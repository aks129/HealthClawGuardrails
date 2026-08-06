"""The zombie reaper runs when a container starts, and never blocks serving.

`r6/fasten/reaper.py` was written to run at boot — its docstring says
"Called from main.py right after schema_sync", and its 5-minute
`ZOMBIE_MIN_AGE` exists specifically so "a job started seconds before a
rolling deploy's second worker boots is not double-triggered". That sentence
described an intent that no longer matched the code: the only callers were
`run_legacy_boot_tasks` and the `recover-zombies` CLI command, and
`railway.toml`'s preDeployCommand runs neither.

The consequence is patient-visible. Fasten NDJSON ingest runs in a daemon
thread inside the web process (`r6/fasten/routes.py:_launch_ingest`), so
every deploy kills whatever imports are in flight, and `main` auto-deploys.
A patient whose records were importing when someone merged got a job wedged
in a non-terminal state with no automatic recovery — the documented fallback
being a human hitting the retry endpoint.

It is wired into the container start command rather than into `create_app`,
because `tests/test_app_factory.py` pins the factory as side-effect-free:
constructing an app must not touch the database, start a thread, or call an
external service. That pin is worth more than the convenience, and the CLI
command already exists for exactly this.

These tests run the Dockerfile's real `CMD` under `/bin/sh` with recording
stubs on PATH, following `tests/test_careagents_container_roles.py`, so the
dispatch is observed rather than pattern-matched.

What they do not prove: no image is built and no container is run. They show
what the start command does, not that Railway runs it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"

# /bin/dash is /bin/sh on Debian, so it is both the image's shell and CI's.
SHELLS = [path for path in ("/bin/sh", "/bin/dash") if os.path.exists(path)]

#: Records its own argv, then exits with $STUB_EXIT (default 0) so a test can
#: make the reaper fail on demand.
STUB = """#!/bin/sh
for arg in "$0" "$@"; do printf '%s\\n' "$arg" >> "$RECORD"; done
exit ${STUB_EXIT:-0}
"""


def _cmd_script() -> str:
    """The shell program the image starts."""
    text = DOCKERFILE.read_text().replace("\\\n", "")
    match = re.search(r"^CMD (.+)$", text, re.M)
    assert match, "the Dockerfile no longer has a CMD"
    script = match.group(1).strip()
    assert not script.startswith("["), (
        "CMD became exec form; these tests run it as a shell program")
    return script


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("bin")
    for name in ("gunicorn", "flask"):
        stub = path / name
        stub.write_text(STUB)
        stub.chmod(0o755)
    return path


def _start(stubs, shell="/bin/sh", **env):
    """Run the real start command; return every argv it launched."""
    record = stubs / "record"
    if record.exists():
        record.unlink()
    environ = {"PATH": str(stubs), "RECORD": str(record), **env}
    result = subprocess.run([shell, "-c", _cmd_script()], env=environ,
                            capture_output=True, text=True)
    lines = record.read_text().splitlines() if record.exists() else []
    # $0 is the resolved stub path; the command name is what the assertions
    # are about, so name the executables and leave their arguments alone.
    launched = [os.path.basename(line) if line.startswith(str(stubs)) else line
                for line in lines]
    return result, launched


@pytest.mark.parametrize("shell", SHELLS)
def test_container_start_reaps_stranded_jobs_before_serving(stubs, shell):
    """MUTATION: drop the recover-zombies step from CMD -> red.

    Ran it, saw red.
    """
    _, launched = _start(stubs, shell=shell)
    assert "recover-zombies" in launched, (
        f"the start command never reaps stranded Fasten jobs: {launched}")
    assert "gunicorn" in launched, launched
    assert launched.index("recover-zombies") < launched.index("gunicorn"), (
        "recovery has to run before the app starts taking traffic")


@pytest.mark.parametrize("shell", SHELLS)
def test_a_failing_reaper_never_stops_the_app_from_serving(stubs, shell):
    """The reaper is best-effort. Serving traffic is not.

    `recover_zombie_jobs` already swallows its own exceptions, but the CLI
    process can still exit non-zero for reasons it never sees — an
    unreachable database at boot, a bad entrypoint. If that took gunicorn
    down with it, a recovery convenience would have become an outage, and
    the restart policy would retry it three times and give up.

    MUTATION: chain the two commands with `&&` instead -> red, gunicorn
    never starts. Ran it, saw red.
    """
    result, launched = _start(stubs, shell=shell, STUB_EXIT="1")
    assert "gunicorn" in launched, (
        f"a failed reaper stopped the app from starting: {launched} "
        f"(exit {result.returncode})")
