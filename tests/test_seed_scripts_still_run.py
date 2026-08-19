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


# ---------------------------------------------------------------------------
# smoke_medplum: a partial run must not round itself up
# ---------------------------------------------------------------------------
#
# The script reported "7/8 guardrail checks passed" against a HealthClaw with
# no Medplum behind it at all — seven checks describing local SQLite, and the
# single check that would have caught it counted as one lost point. Two of the
# seven were `"000-00-1234" not in blob` evaluated on the body of a 401, which
# is the #499 vacuous assertion: an empty body contains no SSN either.
#
# The guard on this file used to be a grep for the string "Content-Type",
# which is what a procedural script allows. The check logic now lives in a
# Runner that a test can drive, so these are behavioural.

def _runner():
    sys.path.insert(0, str(ROOT / "scripts"))
    import smoke_medplum
    return smoke_medplum


def test_a_failed_gate_stops_the_run_rather_than_scoring_it():
    """MUTATION: make gate() record and return False instead of raising -> red.

    The redaction checks live after the gates on purpose. If a failing gate
    only returned False, they would still execute against an empty body and
    pass.
    """
    m = _runner()
    run = m.Runner()
    run.check("write blocked without step-up (401)", True)
    with pytest.raises(m.StoppedEarly):
        run.gate("read returns 200", False, "status 401")

    assert run.stopped_by == "read returns 200"
    assert run.exit_code() == 1
    assert "STOPPED at the gate" in run.summary()
    assert "did not run" in run.summary()


def test_the_summary_never_reports_a_bare_fraction_for_a_stopped_run():
    """"7/8" is the number that made a run with no Medplum look healthy.

    MUTATION: drop the stopped_by clause from summary() -> red.
    """
    m = _runner()
    run = m.Runner()
    for i in range(7):
        run.check(f"check {i}", True)
    with pytest.raises(m.StoppedEarly):
        run.gate("Medplum-sourced (not local storage)", False, "_source=None")

    summary = run.summary()
    assert not summary.startswith("7/8 guardrail checks passed."), (
        "a stopped run printed a plain fraction")
    assert "Medplum-sourced" in summary, (
        "the summary must name the gate that stopped it, or an operator "
        "cannot tell a partial run from a failing one")


def test_a_clean_run_still_exits_zero():
    """The other side. A guard that can only fail is not a guard.

    MUTATION: make exit_code() always return 1 -> red.
    """
    m = _runner()
    run = m.Runner()
    run.check("a", True)
    run.gate("b", True)
    run.check("c", True)
    assert run.exit_code() == 0
    assert run.summary() == "3/3 guardrail checks passed."


def test_a_failing_check_is_not_a_stopped_run():
    """A check and a gate are different things, and conflating them would
    make any single failure hide every later result."""
    m = _runner()
    run = m.Runner()
    run.check("a", False, "got 500")
    run.check("b", True)
    assert run.stopped_by is None
    assert run.exit_code() == 1
    assert run.summary() == "1/2 guardrail checks passed."


def test_the_redaction_assertions_sit_after_both_gates():
    """Order is the property; the Runner only enforces it if the source uses
    it. Reading the source here is the cheap half, and the behavioural tests
    above are the expensive half.

    MUTATION: move the SSN/phone checks above the `read returns 200` gate
    -> red, and they would evaluate on a refusal again.
    """
    src = (ROOT / "scripts" / "smoke_medplum.py").read_text()
    body = src.split("def _run_checks")[1]
    read_gate = body.index('gate("read returns 200')
    source_gate = body.index('gate("Medplum-sourced')
    for needle in ('check("SSN masked in read"', 'check("phone redacted in read"',
                   'check("name redacted (initial only)"'):
        assert body.index(needle) > read_gate, f"{needle} runs before the read gate"
        assert body.index(needle) > source_gate, f"{needle} runs before the source gate"
