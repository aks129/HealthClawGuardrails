"""CareAgents automatic deploy and drift alarm (scripts/careagents_release.py,
.github/workflows/careagents-deploy.yml, careagents-drift.yml)."""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "careagents_release", ROOT / "scripts" / "careagents_release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

SHA = "4f2a91cbeef1" + "0" * 28
LATER = "8abc80c96290" + "1" * 28
NOW = 1_800_000_000


def _workflow(name):
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


# ---- same_build / landed -------------------------------------------------

@pytest.mark.parametrize("deployed,sha,expected", [
    ("4f2a91cbeef1", SHA, True),             # short marker, full sha
    ("4f2a91cbeef1", "4f2a91cbeef1", True),
    ("4f2a91cbeef2", SHA, False),
    ("4f2a91cbeef1-dirty", SHA, False),      # uncommitted code never matches
    ("unknown", SHA, False),
    ("", SHA, False),
    ("4f2a91cbeef1", "", False),
])
def test_same_build(deployed, sha, expected):
    assert release.same_build(deployed, sha) is expected


def test_landed_needs_the_build_and_a_draining_worker():
    assert release.landed({"build": "4f2a91cbeef1", "run_workers": True}, SHA)
    assert not release.landed({"build": "4f2a91cbeef1", "run_workers": False}, SHA)
    assert not release.landed({"build": "4f2a91cbeef1"}, SHA)
    assert not release.landed({"build": "000000000000", "run_workers": True}, SHA)


# ---- wait ------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_wait_succeeds_once_the_new_build_reports_ready():
    clock = _Clock()
    answers = iter([
        RuntimeError("connection reset"),                     # mid-rollout blip
        {"build": "000000000000", "run_workers": True},       # old build
        {"build": "4f2a91cbeef1", "run_workers": False},      # worker not yet
        {"build": "4f2a91cbeef1", "run_workers": True},
    ])

    def fetch():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert release.wait(SHA, 900, 15, fetch=fetch, clock=clock, sleep=clock.sleep) == 0
    assert clock.t == 45


def test_wait_fails_when_production_never_reports_the_build():
    clock = _Clock()
    code = release.wait(SHA, 60, 15, clock=clock, sleep=clock.sleep,
                        fetch=lambda: {"build": "000000000000", "run_workers": True})
    assert code == 1
    assert clock.t >= 60


# ---- drift_verdict -----------------------------------------------------------

def _never(*_):
    raise AssertionError("ancestry must not be consulted")


def test_drift_current_when_deployed_is_the_newest_careagents_commit():
    code, _ = release.drift_verdict("4f2a91cbeef1", SHA, NOW - 99_999, NOW, 7200, _never)
    assert code == release.CURRENT


def test_drift_current_when_deployed_is_a_later_main_commit():
    # A deploy from a main tip that did not itself touch CareAgents.
    code, _ = release.drift_verdict("8abc80c96290", SHA, NOW - 99_999, NOW, 7200,
                                    lambda a, d: True)
    assert code == release.CURRENT


def test_drift_behind_past_the_grace():
    code, detail = release.drift_verdict("000000000000", SHA, NOW - 7201, NOW, 7200,
                                         lambda a, d: False)
    assert code == release.BEHIND
    assert "4f2a91cbeef1" in detail


def test_drift_within_grace_does_not_alarm():
    code, _ = release.drift_verdict("000000000000", SHA, NOW - 600, NOW, 7200,
                                    lambda a, d: False)
    assert code == release.WITHIN_GRACE


@pytest.mark.parametrize("deployed", ["unknown", "4f2a91cbeef1-dirty", ""])
def test_drift_an_unshipped_marker_counts_as_behind(deployed):
    code, _ = release.drift_verdict(deployed, SHA, NOW - 7201, NOW, 7200, _never)
    assert code == release.BEHIND


def test_drift_a_build_git_cannot_resolve_counts_as_behind():
    code, detail = release.drift_verdict("abcdefabcdef", SHA, NOW - 7201, NOW, 7200,
                                         lambda a, d: None)
    assert code == release.BEHIND
    assert "not a commit on main" in detail


def test_is_ancestor_reads_git_exit_codes():
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout.strip()
    assert release.is_ancestor(head, head) is True
    assert release.is_ancestor("f" * 40, head) is None


def test_drift_reports_unknown_when_healthz_is_unreachable(capsys):
    def boom():
        raise OSError("unreachable")
    assert release.drift(7200, "HEAD", fetch=boom) == release.UNKNOWN


# ---- workflow shape ------------------------------------------------------------

def test_deploy_workflow_ships_only_main_and_never_overlaps():
    wf = _workflow("careagents-deploy.yml")
    triggers = wf.get(True) or wf["on"]
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    assert set(triggers["push"]["paths"]) == {
        "careagents/**", "deploy/careagents/**", "pyproject.toml", "uv.lock"}
    assert wf["permissions"] == {"contents": "read"}
    assert wf["concurrency"]["cancel-in-progress"] is False
    job = wf["jobs"]["deploy"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert re.fullmatch(r"\d+\.\d+\.\d+", job["env"]["RAILWAY_CLI_VERSION"])


def test_deploy_workflow_follows_the_runbook_stage_and_upload():
    steps = _workflow("careagents-deploy.yml")["jobs"]["deploy"]["steps"]
    stage = next(s["run"] for s in steps if s.get("name") == "Stage the build")
    lines = [ln.strip() for ln in stage.splitlines() if ln.strip()]
    order = [i for i, ln in enumerate(lines) if ln.startswith(("cp ", "./deploy/"))]
    assert [lines[i] for i in order] == [
        'cp pyproject.toml uv.lock "$STAGE/"',
        'cp -R careagents "$STAGE/"',
        'cp deploy/careagents/Dockerfile "$STAGE/Dockerfile"',
        './deploy/careagents/stamp_build.sh "$STAGE"',   # stamp LAST
    ]
    upload = next(s["run"] for s in steps if s.get("name", "").startswith("Upload"))
    assert upload.lstrip().startswith('cd "$STAGE"')
    ups = [ln.strip() for ln in upload.splitlines() if ln.strip().startswith("railway up")]
    assert len(ups) == 2
    assert "--service careagents " in ups[0] and "--service careagents-worker " in ups[1]
    for up in ups:
        assert "--detach" in up and " -y" not in up and "--new" not in up
    # Every step after the gate is skipped when the secret is absent.
    for step in steps[1:]:
        assert step.get("if") == "steps.gate.outputs.skip == 'false'", step
    assert "careagents_release.py wait" in steps[-1]["run"]


def test_drift_workflow_has_history_and_alarms_only_past_the_grace():
    wf = _workflow("careagents-drift.yml")
    assert wf["permissions"] == {"contents": "read", "issues": "write"}
    steps = wf["jobs"]["drift"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    assert "--grace 7200" in steps[1]["run"]
