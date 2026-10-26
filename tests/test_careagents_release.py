"""CareAgents automatic deploy and drift alarm (scripts/careagents_release.py,
.github/workflows/careagents-deploy.yml, careagents-drift.yml)."""

import importlib.util
import json
import os
import re
import subprocess
import sys
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
    code, _ = release.drift_verdict("4f2a91cbeef1", SHA, NOW - 99_999, NOW, 7200, _never, _never)
    assert code == release.CURRENT


def test_drift_current_when_deployed_is_a_later_main_commit():
    # A deploy from a main tip that did not itself touch CareAgents.
    code, _ = release.drift_verdict("8abc80c96290", SHA, NOW - 99_999, NOW, 7200,
                                    lambda a, d: True, lambda d: True)
    assert code == release.CURRENT


def test_drift_behind_past_the_grace():
    code, detail = release.drift_verdict("000000000000", SHA, NOW - 7201, NOW, 7200,
                                         lambda a, d: False, _never)
    assert code == release.BEHIND
    assert "4f2a91cbeef1" in detail


def test_drift_within_grace_does_not_alarm():
    code, _ = release.drift_verdict("000000000000", SHA, NOW - 600, NOW, 7200,
                                    lambda a, d: False, _never)
    assert code == release.WITHIN_GRACE


@pytest.mark.parametrize("deployed", ["unknown", "4f2a91cbeef1-dirty", ""])
def test_drift_an_unshipped_marker_counts_as_behind(deployed):
    code, _ = release.drift_verdict(deployed, SHA, NOW - 7201, NOW, 7200, _never, _never)
    assert code == release.BEHIND


def test_drift_a_build_git_cannot_resolve_counts_as_behind():
    code, detail = release.drift_verdict("abcdefabcdef", SHA, NOW - 7201, NOW, 7200,
                                         lambda a, d: None, _never)
    assert code == release.BEHIND
    assert "not a commit on main" in detail


def test_drift_a_descendant_that_is_not_on_main_counts_as_behind():
    # Built from an unmerged branch: it contains main, but main never had it.
    code, detail = release.drift_verdict("abcdefabcdef", SHA, NOW - 7201, NOW, 7200,
                                         lambda a, d: True, lambda d: False)
    assert code == release.BEHIND
    assert "not a commit on main" in detail


@pytest.mark.parametrize("value,expected", [
    ("4f2a91cbeef1", "4f2a91cbeef1"),
    ("4f2a91cbeef1-dirty", "4f2a91cbeef1-dirty"),
    ("unknown", "unknown"),
    ("```\n@x -->", "unknown"),
    ("4f2a91cbeef1\ndeployed=x", "unknown"),
    (None, "unknown"),
    (12345, "unknown"),
])
def test_clean_build_passes_only_deploy_markers(value, expected):
    assert release.clean_build(value) == expected


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
    # actions: read is for the CI gate; nothing writes.
    assert wf["permissions"] == {"contents": "read", "actions": "read"}
    assert wf["concurrency"]["cancel-in-progress"] is False
    job = wf["jobs"]["deploy"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert re.fullmatch(r"\d+\.\d+\.\d+", job["env"]["RAILWAY_CLI_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", job["env"]["RAILWAY_CLI_SHA256"])
    assert job["environment"] == "careagents-production"
    assert job["env"]["RAILWAY_PROJECT_ID"] == "835e92df-ebaa-4aa1-bfb5-cb1b346d385e"


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


@pytest.mark.parametrize("project_id,ok", [
    ("835e92df-ebaa-4aa1-bfb5-cb1b346d385e", True),
    ("835e92df-ebaa-4aa1-bfb5-cb1b346d385f", False),
    (None, False),
])
def test_the_project_check_admits_only_the_exact_project_id(tmp_path, project_id, ok):
    steps = _workflow("careagents-deploy.yml")["jobs"]["deploy"]["steps"]
    upload = next(s["run"] for s in steps if s.get("name", "").startswith("Upload"))
    code = re.search(r"python3 -c '\n(.*?)\n' ", upload, re.S).group(1)
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"id": project_id, "name": "awake-serenity",
                                  "services": {"edges": ["secret-ish"]}}))
    result = subprocess.run(
        [sys.executable, "-c", code, str(status)], capture_output=True, text=True,
        env={**os.environ, "RAILWAY_PROJECT_ID": "835e92df-ebaa-4aa1-bfb5-cb1b346d385e"})
    assert (result.returncode == 0) is ok, result.stderr
    assert "secret-ish" not in result.stdout + result.stderr
    if ok:
        assert result.stdout.strip() == "awake-serenity"


# ---- CI gate -------------------------------------------------------------------

REQUIRED = ["python-tests", "postgres-tests", "node-tests", "lint",
            "secret-scan", "dependency-audit", "compliance-gates"]


def _jobs(**overrides):
    """Every ci.yml job completed successfully, with per-job overrides as
    (status, conclusion) or None to drop the job."""
    names = REQUIRED + ["playwright-tests", "compose-smoke"]
    jobs = []
    for name in names:
        status, conclusion = overrides.get(name.replace("-", "_"), ("completed", "success")) or (None, None)
        if status is not None:
            jobs.append({"name": name, "status": status, "conclusion": conclusion})
    return jobs


RUN_DONE = {"id": 39890567588, "status": "completed", "head_sha": SHA, "event": "push"}
RUN_LIVE = {"id": 39890567588, "status": "in_progress", "head_sha": SHA, "event": "push"}


def test_ci_gate_requires_exactly_the_agreed_jobs():
    # Mutation: dropping any name from REQUIRED_CI_JOBS, or adding
    # playwright-tests, turns this red.
    assert list(release.REQUIRED_CI_JOBS) == REQUIRED
    assert "playwright-tests" not in release.REQUIRED_CI_JOBS


def test_every_required_ci_job_exists_in_ci_yml_under_that_check_name():
    # The gate matches by job name. A `name:` override or a matrix would
    # rename the job in the API, and the gate would then fail every deploy
    # (missing), so pin that the ids are still the names.
    ci = _workflow("ci.yml")
    assert ci["name"] == "CI - FHIR MCP Guardrails"
    for name in release.REQUIRED_CI_JOBS:
        job = ci["jobs"][name]
        assert "name" not in job and "matrix" not in job.get("strategy", {}), name
    triggers = ci.get(True) or ci["on"]
    assert "main" in triggers["push"]["branches"] and "paths" not in triggers["push"]


def test_ci_verdict_passes_when_every_required_job_succeeded():
    assert release.ci_verdict(RUN_DONE, _jobs())[0] == release.PASS


def test_ci_verdict_passes_while_playwright_fails_or_runs():
    # Not required: a red or slow playwright lane does not hold the deploy.
    assert release.ci_verdict(RUN_DONE, _jobs(playwright_tests=("completed", "failure")))[0] == release.PASS
    assert release.ci_verdict(RUN_LIVE, _jobs(playwright_tests=("in_progress", None)))[0] == release.PASS


@pytest.mark.parametrize("name", REQUIRED)
@pytest.mark.parametrize("conclusion", ["failure", "skipped", "cancelled", "timed_out",
                                        "neutral", "action_required", "stale", None])
def test_ci_verdict_fails_on_any_required_job_that_did_not_succeed(name, conclusion):
    # Mutation: `!= "failure"` in place of `== "success"` admits skipped,
    # which is what compliance-gates reports when compose-smoke fails.
    state, detail = release.ci_verdict(
        RUN_LIVE, _jobs(**{name.replace("-", "_"): ("completed", conclusion)}))
    assert state == release.FAIL, detail
    assert name in detail


def test_ci_verdict_fails_when_a_finished_run_lacks_a_required_job():
    # A renamed or deleted job must not pass the gate by its absence.
    state, detail = release.ci_verdict(RUN_DONE, _jobs(lint=None))
    assert state == release.FAIL
    assert "lint (missing)" in detail


def test_ci_verdict_waits_on_unfinished_or_unstarted_jobs():
    assert release.ci_verdict(RUN_LIVE, _jobs(postgres_tests=("in_progress", None)))[0] == release.PENDING
    assert release.ci_verdict(RUN_LIVE, _jobs(compliance_gates=None))[0] == release.PENDING
    assert release.ci_verdict(RUN_LIVE, [])[0] == release.PENDING
    assert release.ci_verdict(None, [])[0] == release.PENDING


def test_ci_verdict_fails_fast_even_while_other_jobs_run():
    jobs = _jobs(lint=("completed", "failure"), postgres_tests=("queued", None))
    assert release.ci_verdict(RUN_LIVE, jobs)[0] == release.FAIL


def test_ci_verdict_a_duplicate_job_entry_must_pass_too():
    jobs = _jobs() + [{"name": "lint", "status": "completed", "conclusion": "failure"}]
    assert release.ci_verdict(RUN_DONE, jobs)[0] == release.FAIL


def _feed(*answers):
    answers = iter(answers)

    def fetch():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer
    return fetch


def test_ci_gate_waits_through_blips_and_queued_runs_then_passes():
    clock = _Clock()
    fetch = _feed(
        (None, []),                                         # run not created yet
        OSError("api blip"),
        (RUN_LIVE, _jobs(python_tests=("queued", None))),
        (RUN_DONE, _jobs()),
    )
    assert release.ci_gate(SHA, 2400, 30, fetch, clock=clock, sleep=clock.sleep) == 0
    assert clock.t == 90


def test_ci_gate_fails_without_waiting_out_the_clock_when_ci_fails(capsys):
    clock = _Clock()
    fetch = _feed((RUN_DONE, _jobs(postgres_tests=("completed", "failure"))))
    assert release.ci_gate(SHA, 2400, 30, fetch, clock=clock, sleep=clock.sleep) == 1
    assert clock.t == 0
    err = capsys.readouterr().err
    assert "not deploying" in err and "postgres-tests (failure)" in err


def test_ci_gate_fails_when_ci_never_finishes(capsys):
    clock = _Clock()

    def sleep(seconds):
        # Mutation (deadline check removed) loops forever on a fake clock;
        # fail instead of hanging the suite.
        assert clock.t < 10_000, "ci_gate ignored its deadline"
        clock.sleep(seconds)
    code = release.ci_gate(SHA, 2400, 30, lambda: (RUN_LIVE, _jobs(lint=("in_progress", None))),
                           clock=clock, sleep=sleep)
    assert code == 1
    assert 2400 <= clock.t < 2400 + 30
    assert "did not finish within 2400s" in capsys.readouterr().err


def test_ci_gate_never_passes_on_api_errors_alone():
    clock = _Clock()

    def boom():
        raise OSError("403 forbidden")
    assert release.ci_gate(SHA, 120, 30, boom, clock=clock, sleep=clock.sleep) == 1


@pytest.mark.parametrize("sha", ["4f2a91cbeef1", "", SHA.upper(), SHA + "0"])
def test_ci_gate_needs_a_full_sha(sha):
    # The runs filter matches only a full sha; a short one finds no run and
    # would wait 40 minutes for nothing.
    assert release.ci_gate(sha, 2400, 30, lambda: (RUN_DONE, _jobs())) == 1


def test_fetch_ci_reads_only_ci_yml_push_runs_for_this_sha():
    calls = []

    def get(path, token):
        calls.append(path)
        if "/runs?" in path:
            return {"workflow_runs": [
                {"id": 5, "head_sha": SHA, "event": "push", "head_branch": "main"},
                {"id": 9, "head_sha": SHA, "event": "pull_request", "head_branch": "main"},
                {"id": 8, "head_sha": LATER, "event": "push", "head_branch": "main"},
                {"id": 7, "head_sha": SHA, "event": "push", "head_branch": "main"},
            ]}
        return {"jobs": [{"name": "lint"}]}

    run, jobs = release.fetch_ci("o/r", SHA, "tok", get=get)
    assert run["id"] == 7 and jobs == [{"name": "lint"}]
    assert calls[0] == ("/repos/o/r/actions/workflows/ci.yml/runs"
                        f"?head_sha={SHA}&event=push&branch=main&per_page=20")
    assert calls[1] == "/repos/o/r/actions/runs/7/jobs?filter=latest&per_page=100"


def test_fetch_ci_counts_only_mains_run_when_another_branch_has_the_same_sha():
    # ci.yml also runs on pushes to master. A newer green push run of the same
    # sha on master must not stand in for main's failed one. The client-side
    # head_branch check matters even with branch=main in the query, so the
    # fake ignores the query. Mutation: dropping that check picks run 2 and
    # the verdict becomes PASS.
    failed = _jobs(lint=("completed", "failure"))

    def get(path, token):
        if "/runs?" in path:
            return {"workflow_runs": [
                {"id": 1, "head_sha": SHA, "event": "push", "head_branch": "main", "status": "completed"},
                {"id": 2, "head_sha": SHA, "event": "push", "head_branch": "master", "status": "completed"},
            ]}
        return {"jobs": failed if "/runs/1/" in path else _jobs()}

    run, jobs = release.fetch_ci("o/r", SHA, "t", get=get)
    assert run["id"] == 1
    assert release.ci_verdict(run, jobs)[0] == release.FAIL


def test_fetch_ci_with_only_another_branchs_run_is_none():
    runs = {"workflow_runs": [{"id": 2, "head_sha": SHA, "event": "push", "head_branch": "master"}]}
    assert release.fetch_ci("o/r", SHA, "t", get=lambda p, t: runs) == (None, [])


@pytest.mark.parametrize("code", [401, 403])
def test_ci_gate_names_a_permission_problem_instead_of_waiting_quietly(code, capsys):
    import urllib.error
    clock = _Clock()

    def denied():
        raise urllib.error.HTTPError("https://api.github.com/x", code, "Forbidden", {}, None)
    assert release.ci_gate(SHA, 60, 30, denied, clock=clock, sleep=clock.sleep) == 1
    out = capsys.readouterr()
    assert f"could not read CI: permission denied (HTTP {code}; check actions: read)" in out.out
    assert "permission denied" in out.err   # the final ::error:: carries it too


def test_fetch_ci_with_no_run_is_none():
    assert release.fetch_ci("o/r", SHA, "t", get=lambda p, t: {"workflow_runs": []}) == (None, [])


def test_the_ci_gate_runs_before_any_railway_step_and_holds_the_deploy():
    job = _workflow("careagents-deploy.yml")["jobs"]["deploy"]
    steps = job["steps"]
    names = [s.get("name") or s.get("uses") for s in steps]
    gate = names.index("Wait for CI to pass on this commit")
    # Every step that installs or runs the Railway CLI, or holds the token.
    # The token check (id: gate) only evaluates a boolean and names Railway
    # in its notice, so it is the one step allowed ahead of the CI gate.
    assert steps[0]["id"] == "gate"
    railway = [i for i, s in enumerate(steps) if s.get("id") != "gate" and (
        "railway" in (s.get("name", "") + s.get("run", "")).lower()
        or "RAILWAY_TOKEN" in str(s.get("env")))]
    assert len(railway) >= 2 and gate < min(railway), names
    step = steps[gate]
    assert step["run"].strip() == (
        'python3 scripts/careagents_release.py ci-gate --sha "$GITHUB_SHA" --timeout 3600')
    # A failing gate must stop the job: no continue-on-error on it, and no
    # later step that runs regardless of earlier failures.
    assert not step.get("continue-on-error")
    for later in steps[gate + 1:]:
        assert not any(w in str(later.get("if", "")) for w in ("always()", "failure()", "!cancelled()"))
    # Room for 60 minutes of CI, the uploads and 15 minutes of /healthz.
    assert job["timeout-minutes"] >= 60 + 15 + 5
    # No later step needs git auth, so the checkout keeps no token on disk.
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False


def test_drift_workflow_has_history_and_alarms_only_past_the_grace():
    wf = _workflow("careagents-drift.yml")
    assert wf["permissions"] == {"contents": "read", "issues": "write"}
    steps = wf["jobs"]["drift"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    assert "--grace 7200" in steps[1]["run"]
