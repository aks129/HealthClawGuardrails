"""What one CareAgents image will actually start, per CARE_ROLE.

The image has to carry both process roles, because `railway add` has no
start-command option: on Railway the role is reachable through the environment
alone. Before #273 the Dockerfile *said* it supported two roles while `CMD` was
hardcoded to gunicorn, so the deployment ran web twice — `/healthz` answered
503 with `run_workers: false` and chat answered `run_workers_unavailable`,
because nothing ever claimed a queued run.

These tests extract the `CMD` from the Dockerfile and run that dispatch under
`/bin/sh` with stub `gunicorn` and `python` executables on PATH, so the branch
each role takes is observed rather than pattern-matched, and the web argv is
compared element by element rather than by substring.

The shell is covered: the dispatch runs under the host's `/bin/sh` and, where
present, under `/bin/dash` — which is `/bin/sh` on Debian, so it is the image's
own shell and the one Linux CI uses.

What they do not prove: no image is built and no container is run. Nothing here
shows that gunicorn or careagents.worker exist in the image, that PATH resolves
the same way inside it, that `exec` really makes the role process PID 1, or
that the Railway services carry the environment the worker needs. One property
is pinned — which command each CARE_ROLE reaches, and that an unrecognised role
reaches none. (Reviewing #273, QA closed the container-level gaps against a
real build: the stored CMD matches, `CARE_ROLE=worker` puts the worker at PID
1, and `docker stop` returns in 0.2s with `exec` versus 30s and SIGKILL
without.)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "deploy" / "careagents" / "Dockerfile"

# The command the web role runs, split as the shell splits it. Any edit to a
# flag, a default, or the access-log format has to be made here too — which is
# the point: the durable-run split must not quietly re-tune the web process
# while it is adding a second role. `--threads` moved 4 -> 8 in #219; the test
# below says why, so the number is never re-tuned from an estimate.
WEB_ARGV = [
    "gunicorn", "careagents.wsgi:app",
    "--bind", "0.0.0.0:8600",
    "--workers", "2",
    "--threads", "8",
    "--timeout", "180",
    "--access-logfile", "-",
    "--error-logfile", "-",
    "--access-logformat", '%(h)s "%(r)s" %(s)s %(M)sms',
]

# /bin/dash is /bin/sh on Debian, so it is both the image's shell and CI's.
SHELLS = [path for path in ("/bin/sh", "/bin/dash") if os.path.exists(path)]

STUB = """#!/bin/sh
: > "$RECORD"
for arg in "$0" "$@"; do printf '%s\\n' "$arg" >> "$RECORD"; done
"""


def _cmd_script() -> str:
    """The shell program in the Dockerfile's `CMD ["sh", "-c", ...]`."""
    text = DOCKERFILE.read_text()
    # Docker joins continued lines before parsing the JSON form.
    text = text.replace("\\\n", "")
    match = re.search(r"^CMD (\[.*\])\s*$", text, re.M)
    assert match, "CMD is no longer a single JSON exec form"
    argv = json.loads(match.group(1))
    assert argv[:2] == ["sh", "-c"], argv[:2]
    return argv[2]


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    """A PATH holding recording stand-ins for gunicorn and python."""
    path = tmp_path_factory.mktemp("bin")
    for name in ("gunicorn", "python"):
        stub = path / name
        stub.write_text(STUB)
        stub.chmod(0o755)
    return path


def _start(stubs, role, shell="/bin/sh", **env):
    """Run the real dispatch; return (exit code, argv it launched, stderr)."""
    record = stubs / "record"
    if record.exists():
        record.unlink()
    environ = {"PATH": str(stubs), "RECORD": str(record), **env}
    if role is not None:
        environ["CARE_ROLE"] = role
    result = subprocess.run([shell, "-c", _cmd_script()], env=environ,
                            capture_output=True, text=True)
    argv = None
    if record.exists():
        argv = record.read_text().splitlines()
        argv[0] = os.path.basename(argv[0])
    return result.returncode, argv, result.stderr


# --- the two roles the image claims to support -------------------------------

@pytest.mark.parametrize("role", [None, "web"])
def test_the_default_role_still_starts_the_web_server_unchanged(stubs, role):
    code, argv, stderr = _start(stubs, role)
    assert code == 0, stderr
    assert argv == WEB_ARGV


def test_the_web_thread_pool_is_the_size_the_probe_measured(stubs):
    """2 workers x 8 threads, because that is what was measured (#219).

    Every open chat turn holds one gunicorn thread for the whole life of its
    run — the SSE replay loop, not inference, which has run outside this
    process since #257. The pool is therefore the concurrency ceiling for
    every route, `/healthz` included. Measured 2026-09-03 against this exact
    invocation, 20s turns:

        2 x 4 :  N=8 -> /healthz waited 18.19s;  N=16 -> 38.32s
        2 x 8 :  N=8 -> /healthz waited  0.00s;  N=16 -> 18.23s

    (`docs/evidence/2026-09-03-probe-219-thread-saturation.md` §7, PR #573.)

    One doubling is all that evidence supports. It moves the failure from 8
    concurrent turns to 16; it does not remove it, and each thread also polls
    HealthClaw ~4x/s on a browser's behalf, so the next doubling doubles that
    load on the engine that also serves clinicians. This pin exists so the
    next edit is a decision with a measurement behind it rather than drift.
    """
    _, argv, _ = _start(stubs, "web")
    assert argv[argv.index("--threads") + 1] == "8", (
        "the web thread count is pinned to the 2x8 the #219 probe measured; "
        "changing it needs a new measurement, not an estimate")
    assert argv[argv.index("--workers") + 1] == "2", (
        "the probe measured 2 workers x 8 threads; CARE_WEB_WORKERS can "
        "override the worker half on the platform, which multiplies a thread "
        "count nobody measured")


def test_the_web_role_still_honours_its_port_and_worker_overrides(stubs):
    _, argv, _ = _start(stubs, "web", PORT="9999", CARE_WEB_WORKERS="7")
    assert argv[argv.index("--bind") + 1] == "0.0.0.0:9999"
    assert argv[argv.index("--workers") + 1] == "7"


def test_the_worker_role_starts_the_durable_worker(stubs):
    # The defect in #273: this branch did not exist, so a service configured as
    # the worker started a web process and readiness stayed 503 forever.
    code, argv, stderr = _start(stubs, "worker")
    assert code == 0, stderr
    assert argv == ["python", "-m", "careagents.worker"]


# --- and the one it must refuse ----------------------------------------------

@pytest.mark.parametrize("role", [
    "wokrer",       # a plain typo on the service that matters
    "Worker",       # case is not a synonym
    "",             # a variable reference that resolved to nothing
    "web worker",   # both, which is not a role
    "worker ",      # a trailing space pasted from a dashboard field
])
def test_an_unknown_role_exits_instead_of_starting_a_second_web_server(stubs,
                                                                      role):
    # Falling through to web is the dangerous outcome, not the loud one: the
    # container would pass its health check and look correct in the dashboard
    # while no process ever claimed a run.
    code, argv, stderr = _start(stubs, role)
    assert code != 0
    assert argv is None, f"started {argv} for CARE_ROLE={role!r}"
    assert "CARE_ROLE" in stderr


def test_the_refusal_names_the_roles_the_image_can_run(stubs):
    _, _, stderr = _start(stubs, "wokrer")
    assert "web" in stderr and "worker" in stderr


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("role", [
    r"a\cb",       # dash's echo expands \c, which ends output mid-message
    r"a\nb",
    "%s-%d",       # and a value must never be read as a printf format
])
def test_a_hostile_role_value_cannot_truncate_or_reformat_the_refusal(
        stubs, role, shell):
    # The operator reads this line to find out what they typed wrong. It has to
    # survive whatever the dashboard field held, on the image's shell.
    _, argv, stderr = _start(stubs, role, shell=shell)
    assert argv is None
    assert stderr.strip() == (
        f"careagents: unknown CARE_ROLE '{role}' (expected web or worker)")


@pytest.mark.skipif(not os.path.exists("/bin/dash"), reason="dash not present")
@pytest.mark.parametrize("role,expected", [
    ("web", WEB_ARGV),
    ("worker", ["python", "-m", "careagents.worker"]),
    ("wokrer", None),
])
def test_the_dispatch_behaves_the_same_under_the_images_shell(stubs, role,
                                                              expected):
    # python:3.11-slim is Debian, where /bin/sh is dash. The tests above run
    # whatever the host provides, which on macOS is bash — a `case` that
    # depended on a bashism would pass there and fail in the image.
    _, argv, _ = _start(stubs, role, shell="/bin/dash")
    assert argv == expected


# --- source-level, for the parts a stub run cannot observe -------------------

def test_the_worker_branch_execs_so_it_receives_the_platform_sigterm():
    # careagents.worker installs SIGTERM/SIGINT handlers and joins non-daemon
    # threads. A shell left in front of it as PID 1 would swallow the signal,
    # and every redeploy would end in SIGKILL with leases still held.
    script = _cmd_script()
    assert "exec python -m careagents.worker" in script
    assert "exec gunicorn" in script


def test_the_dockerfile_documents_the_role_it_dispatches_on():
    # The header claimed two roles for two releases while CMD supported one.
    header = DOCKERFILE.read_text().split("FROM ")[0]
    assert "CARE_ROLE" in header
