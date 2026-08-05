"""Compliance Gate 2 must fail when it audits the wrong interpreter.

The gate's own history is the reason this file exists. CI ran

    uvx --from pip-audit==2.9.0 pip-audit --strict

and `uvx` builds an ephemeral environment containing pip-audit and its
dependencies — not this project's. The gate examined 27 packages, all of
them pip-audit's own, reported "No known vulnerabilities found", and exited
0 on every PR. Two real advisories against the locked set sat on the default
branch underneath a passing SOC2 S3 gate.

The fixture below is the REAL output of that command, trimmed. It is the
regression: `check_audit_coverage.py` must reject it. Note that `requests`
and `urllib3` appear in it — pip-audit depends on them — so a coverage check
keyed on a single common package would have passed against an empty
interpreter. That is why EXPECTED spans four distinct areas of this
service's dependency set.

MUTATION: drop `cryptography`, `flask` or `sqlalchemy` from EXPECTED in
scripts/check_audit_coverage.py, or lower MIN_AUDITED to 20, and
test_the_old_uvx_output_is_rejected goes green-when-it-should-be-red.
Verified 2026-08-05 with PYTHONDONTWRITEBYTECODE=1.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_audit_coverage.py"

# Verbatim package list from the pre-fix CI command, captured 2026-08-05.
UVX_ONLY_PACKAGES = [
    "boolean-py", "cachecontrol", "certifi", "charset-normalizer",
    "cyclonedx-python-lib", "defusedxml", "filelock", "idna",
    "license-expression", "markdown-it-py", "mdurl", "msgpack",
    "packageurl-python", "packaging", "pip", "pip-api", "pip-audit",
    "pip-requirements-parser", "platformdirs", "py-serializable", "pygments",
    "pyparsing", "requests", "rich", "sortedcontainers", "toml", "urllib3",
]

# A stand-in for the locked set: the four EXPECTED names plus enough filler
# to clear MIN_AUDITED. The real environment audits ~99.
PROJECT_PACKAGES = [
    "flask", "sqlalchemy", "requests", "cryptography",
] + [f"filler-{i}" for i in range(50)]


def _report(names):
    return {"dependencies": [{"name": n, "version": "1.0", "vulns": []}
                             for n in names],
            "fixes": []}


def _run(tmp_path, payload=None, write=True):
    path = tmp_path / "audit.json"
    if write:
        path.write_text(payload if isinstance(payload, str)
                        else json.dumps(payload))
    return subprocess.run([sys.executable, str(SCRIPT), str(path)],
                          capture_output=True, text=True)


def test_the_old_uvx_output_is_rejected(tmp_path):
    """The exact artifact the green gate produced must now be a failure."""
    result = _run(tmp_path, _report(UVX_ONLY_PACKAGES))
    assert result.returncode != 0, (
        "the gate accepted pip-audit's own dependency list as evidence that "
        "this project's dependencies were audited")
    assert "wrong interpreter" in result.stderr


def test_requests_alone_does_not_satisfy_the_gate(tmp_path):
    """pip-audit's own env contains `requests`; one common name is not proof."""
    result = _run(tmp_path, _report(["requests"] + [f"x{i}" for i in range(60)]))
    assert result.returncode != 0
    assert "cryptography" in result.stderr and "flask" in result.stderr


def test_a_real_project_audit_passes(tmp_path):
    result = _run(tmp_path, _report(PROJECT_PACKAGES))
    assert result.returncode == 0, result.stderr
    assert "examined 54 packages" in result.stdout


def test_a_partial_environment_is_rejected(tmp_path):
    """All four names present, but far too few packages to be the locked set."""
    result = _run(tmp_path, _report(["flask", "sqlalchemy", "requests",
                                     "cryptography"]))
    assert result.returncode != 0
    assert "partial environment" in result.stderr


def test_a_missing_report_is_not_a_pass(tmp_path):
    """No file means no evidence. Absence of a report is not a clean report."""
    result = _run(tmp_path, write=False)
    assert result.returncode != 0
    assert "no evidence" in result.stderr


def test_an_unreadable_report_is_not_a_pass(tmp_path):
    result = _run(tmp_path, "<html>502 Bad Gateway</html>")
    assert result.returncode != 0
    assert "not JSON" in result.stderr
