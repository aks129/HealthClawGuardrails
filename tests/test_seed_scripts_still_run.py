"""The operator scripts still import and run.

scripts/seed_smbp_history.py shipped broken. It imported `DEFAULT_ANCHOR`
from r6.smbp.demo_history, that module was rewritten, the constant went
away, and nothing noticed — because the test suite exercises the LIBRARY the
script calls and the e2e harness seeds through the Flask CLI. The script
itself was the one path with no caller in CI.

It broke where it hurts most: the operator reaches for a seed script when
they are setting up a demo, usually against a remote deployment, usually
against the clock. An ImportError at that moment costs the demo, and the
traceback points at the module rather than at the script that failed to keep
up with it.

`--dry-run` exists partly for this. It builds every resource and prints the
summary without needing a server, so a subprocess can prove the whole
import-and-build path works for the price of one process.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Scripts an operator runs by hand, with an argument set that does no I/O.
_DRY_RUNNABLE = [
    ("scripts/seed_smbp_history.py", ["--dry-run"]),
]


@pytest.mark.parametrize("script,args", _DRY_RUNNABLE)
def test_the_script_runs_end_to_end(script, args):
    """MUTATION: import a name from demo_history that does not exist -> red."""
    result = subprocess.run(
        [sys.executable, str(ROOT / script), *args],
        capture_output=True, text=True, cwd=ROOT, timeout=120,
    )
    assert result.returncode == 0, (
        f"{script} exited {result.returncode}. An operator hits this while "
        f"setting up a demo, against the clock.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    assert result.stdout.strip(), f"{script} printed nothing"


def test_the_dry_run_refuses_to_write_without_a_credential():
    """A dry run that silently became a real run would be worse than a
    broken one. Without --dry-run and without a secret it must refuse rather
    than post."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/seed_smbp_history.py"),
         "--base-url", "http://127.0.0.1:1", "--internal-secret", ""],
        capture_output=True, text=True, cwd=ROOT, timeout=120,
    )
    assert result.returncode == 2, (
        "expected a refusal for a missing internal secret, got "
        f"{result.returncode}")
    assert "internal-secret" in result.stderr


def test_the_medplum_gate_check_sends_a_parseable_body():
    """MUTATION: drop Content-Type from the no-step-up request -> red.

    scripts/smoke_medplum.py's first check is named "write blocked without
    step-up (401)" and for its whole life it asserted on a 400. Without a
    Content-Type the body never parses, and the depth-bounded parse that
    deliberately runs AHEAD of the auth gate (#312) refuses the request as
    malformed — so the gate the check is named after was never reached.

    It failed rather than passed, which is the only reason it was ever
    noticed; written as "not 201" it would have passed forever while testing
    nothing. Pinned here because CI cannot run the script itself: it needs a
    live Medplum behind a live HealthClaw.
    """
    src = (ROOT / "scripts" / "smoke_medplum.py").read_text()
    block = src.split("# 1. Write is gated")[1].split("check(")[0]
    assert "Content-Type" in block, (
        "the no-step-up write must send a parseable body, or it is refused "
        "as malformed before the credential is ever considered")
