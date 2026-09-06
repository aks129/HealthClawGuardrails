"""QA regressions for the CareAgents CARE_ROLE dispatch (#273).

Complements ``tests/test_careagents_container_roles.py``. That file proves the
three intended values reach the three intended outcomes. These tests attack the
same dispatch from the operator's and the attacker's side instead:

- ``CARE_ROLE`` arrives from a Railway service variable, i.e. a dashboard text
  field. Treat its value as hostile: whitespace, shell metacharacters, command
  substitution, globs, unicode look-alikes, and a 60 kB value must all reach the
  refusal branch and launch nothing. Starting *anything* for a value that is not
  exactly ``web`` or ``worker`` is the failure this dispatch exists to prevent.
- The parser in the sibling file models Docker's line-continuation join as
  ``replace("\\\\\\n", "")``. That model was checked once against a real
  ``docker build`` (Docker 29.6.2 keeps the continuation line's leading indent,
  so the model is exact). Docker is not available in CI, so instead of pinning
  Docker's behaviour these tests pin the property that makes the model safe:
  the dispatch behaves identically under every plausible join rule, because
  every continued line ends in a space before its backslash.

What these do NOT prove, same as the sibling file: no image is built and no
container is run here. Nothing below shows that ``exec`` makes the role process
PID 1, that ``gunicorn`` and ``careagents.worker`` resolve inside the image, or
that a Railway worker service carries the environment ``careagents.config``
requires. Those were checked by hand against a locally built image; only the
shell-level properties are pinned as regressions.
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

WEB_ARGV = [
    "gunicorn", "careagents.wsgi:app",
    "--bind", "0.0.0.0:8600",
    "--workers", "2",
    # 4 -> 8 in #219, the one deliberate retune of this command since the role
    # split. tests/test_careagents_container_roles.py carries the measurement.
    "--threads", "8",
    "--timeout", "180",
    "--access-logfile", "-",
    "--error-logfile", "-",
    "--access-logformat", '%(h)s "%(r)s" %(s)s %(M)sms',
]
WORKER_ARGV = ["python", "-m", "careagents.worker"]

# Records argv, and would create $CANARY if a value ever escaped the case
# subject into an evaluated command position.
STUB = """#!/bin/sh
: > "$RECORD"
for arg in "$0" "$@"; do printf '%s\\n' "$arg" >> "$RECORD"; done
"""

SHELLS = [s for s in ("/bin/sh", "/bin/dash") if os.path.exists(s)]


def _cmd_block() -> str:
    """The raw, still-continued CMD instruction."""
    lines = DOCKERFILE.read_text().splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith("CMD [")]
    assert starts, "no CMD instruction in the Dockerfile"
    return "".join(lines[starts[-1]:])


def _script(join=lambda block: block.replace("\\\n", "")) -> str:
    joined = join(_cmd_block())
    match = re.search(r"^CMD (\[.*\])\s*$", joined, re.M)
    assert match, "CMD is no longer a single JSON exec form"
    argv = json.loads(match.group(1))
    assert argv[:2] == ["sh", "-c"], argv[:2]
    return argv[2]


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    path = tmp_path_factory.mktemp("care-role-qa")
    for name in ("gunicorn", "python"):
        stub = path / name
        stub.write_text(STUB)
        stub.chmod(0o755)
    return path


def _start(sandbox, role, shell="/bin/sh", script=None, **env):
    """Run the dispatch. Returns (rc, argv launched or None, stderr, canary)."""
    record = sandbox / "record"
    canary = sandbox / "canary"
    for leftover in (record, canary):
        if leftover.exists():
            leftover.unlink()
    environ = {"PATH": str(sandbox), "RECORD": str(record),
               "CANARY": str(canary), **env}
    if role is not None:
        environ["CARE_ROLE"] = role
    result = subprocess.run([shell, "-c", script or _script()], env=environ,
                            capture_output=True, text=True)
    argv = None
    if record.exists():
        argv = record.read_text().splitlines()
        argv[0] = os.path.basename(argv[0])
    return result.returncode, argv, result.stderr, canary.exists()


# --- a hostile CARE_ROLE must never start a process ---------------------------

# Every one of these is something a Railway variable field can hold. None of
# them is `web` or `worker`, so every one must exit non-zero having launched
# nothing. A value that starts a web server here is a worker service that
# silently serves HTTP, passes its health check, and drains no runs — the exact
# shape of #273, reintroduced through a typo instead of a missing branch.
HOSTILE = [
    pytest.param("", id="empty-a-reference-that-resolved-to-nothing"),
    pytest.param(" ", id="one-space"),
    pytest.param("   ", id="only-spaces"),
    pytest.param("\t", id="tab"),
    pytest.param("web ", id="web-trailing-space"),
    pytest.param(" web", id="web-leading-space"),
    pytest.param("web\t", id="web-trailing-tab"),
    pytest.param("worker ", id="worker-trailing-space-pasted"),
    pytest.param("web\n", id="web-trailing-newline"),
    pytest.param("\nweb", id="web-leading-newline"),
    pytest.param("web\r", id="web-trailing-cr-from-windows-paste"),
    pytest.param("web\nworker", id="both-on-two-lines"),
    pytest.param("web worker", id="both-on-one-line"),
    pytest.param("Worker", id="capitalised"),
    pytest.param("WEB", id="upper-case"),
    pytest.param("wokrer", id="transposed-typo"),
    pytest.param("*", id="glob-star-must-not-match-a-pattern"),
    pytest.param("w*", id="glob-prefix"),
    pytest.param("we?", id="glob-question"),
    pytest.param("[w]eb", id="glob-class"),
    pytest.param("wеb", id="cyrillic-ie-look-alike"),
    pytest.param("ｗeb", id="fullwidth-w"),
    pytest.param("web ", id="non-breaking-space"),
    pytest.param("web; touch $CANARY", id="semicolon-injection"),
    pytest.param("x && touch $CANARY", id="andand-injection"),
    pytest.param("$(touch $CANARY)", id="command-substitution-injection"),
    pytest.param("`touch $CANARY`", id="backtick-injection"),
    pytest.param("web | touch $CANARY", id="pipe-injection"),
    pytest.param("web' ; touch $CANARY ; '", id="single-quote-break-out"),
    pytest.param('web" ; touch $CANARY ; "', id="double-quote-break-out"),
    pytest.param("$CARE_ROLE", id="self-reference-must-not-re-expand"),
    pytest.param("w" * 60000, id="60kb-value"),
]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("role", HOSTILE)
def test_a_hostile_care_role_launches_nothing_and_exits(sandbox, shell, role):
    code, argv, stderr, canary = _start(sandbox, role, shell=shell)
    assert argv is None, f"{role!r} started {argv}"
    assert code != 0, f"{role!r} exited 0"
    assert not canary, f"{role!r} executed an injected command"
    assert "CARE_ROLE" in stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_the_two_real_roles_still_reach_their_own_command(sandbox, shell):
    # The other half of the same guarantee: refusing everything would also pass
    # the test above.
    assert _start(sandbox, None, shell=shell)[1] == WEB_ARGV
    assert _start(sandbox, "web", shell=shell)[1] == WEB_ARGV
    assert _start(sandbox, "worker", shell=shell)[1] == WORKER_ARGV


# --- the dispatch must not depend on how Docker joins continuations ----------

JOINS = {
    # What Docker 29.6.2 actually does, verified against a built image: the
    # continuation line keeps its leading indent.
    "keep-indent": lambda b: b.replace("\\\n", ""),
    # What a stricter parser might do instead.
    "strip-indent": lambda b: re.sub(r"\\\n[ \t]*", "", b),
    "collapse-to-one-space": lambda b: re.sub(r"[ \t]*\\\n[ \t]*", " ", b),
}


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("join", list(JOINS))
def test_the_dispatch_is_insensitive_to_the_continuation_join_rule(sandbox,
                                                                   shell, join):
    # Only true while every continued line ends in whitespace before its
    # backslash. Drop that space and `--timeout 180--access-logfile` becomes one
    # token under a stripping parser — a break no host-shell test would catch,
    # because the test does its own joining.
    script = _script(JOINS[join])
    assert _start(sandbox, "web", shell=shell, script=script)[1] == WEB_ARGV
    assert _start(sandbox, "worker", shell=shell,
                  script=script)[1] == WORKER_ARGV
    assert _start(sandbox, "wokrer", shell=shell, script=script)[1] is None


def test_every_continued_line_ends_in_whitespace_before_its_backslash():
    # States the precondition of the test above directly, so a future edit that
    # removes a space fails with a message that says why.
    for number, line in enumerate(_cmd_block().splitlines(), 1):
        if line.endswith("\\"):
            assert line[:-1].endswith((" ", "\t")), (
                f"CMD continuation line {number} joins without whitespace: "
                f"{line!r}")


# --- environment-driven flags reach the web command --------------------------

@pytest.mark.parametrize("shell", SHELLS)
def test_port_and_web_worker_overrides_reach_gunicorn(sandbox, shell):
    _, argv, _, _ = _start(sandbox, "web", shell=shell, PORT="9999",
                           CARE_WEB_WORKERS="7")
    assert argv[argv.index("--bind") + 1] == "0.0.0.0:9999"
    assert argv[argv.index("--workers") + 1] == "7"


def test_the_web_command_changed_only_where_a_measurement_changed_it():
    # Byte-for-byte, not token-for-token: the durable-run split must not retune
    # the web process while it adds a second role. The doubled internal spaces
    # come from the line continuations and are part of the comparison.
    #
    # One difference is authorised, and it is written out here rather than
    # loosened away: #219 measured the thread pool starving `/healthz` and
    # raised it 4 -> 8. Anything else still fails, including a second edit to
    # the same flag — this comparison would then be against 8, not against 4.
    RETUNES = [("--threads 4", "--threads 8", "#219, measured")]

    previous = subprocess.run(
        ["git", "-C", str(REPO), "show",
         "f71d46e:deploy/careagents/Dockerfile"],
        capture_output=True, text=True)
    if previous.returncode != 0:
        pytest.skip("f71d46e not present in this checkout")
    before = json.loads(re.search(
        r"^CMD (\[.*\])\s*$",
        previous.stdout.replace("\\\n", ""), re.M).group(1))[2]
    for old, new, why in RETUNES:
        assert old in before, (
            f"the authorised retune ({why}) no longer applies: {old!r} is not "
            "in the pre-split command, so this test is comparing to nothing")
        before = before.replace(old, new)
    script = _script()
    opening = 'case "${CARE_ROLE-web}" in   web) exec '
    assert script.startswith(opening)
    assert script[len(opening):script.index(" ;;   worker)")] == before


# --- source-level guarantees a stub run cannot observe -----------------------

def test_both_branches_exec_so_the_role_process_becomes_pid_one():
    # Verified against a real container: with `exec`, `docker stop` on the
    # worker returns in 0.2s with exit 0; with `sh` left in front of it, the
    # same stop takes the full 30s grace period and ends in exit 137 (SIGKILL)
    # with run leases still held. careagents/worker.py installs SIGTERM/SIGINT
    # handlers and joins non-daemon threads, and PID 1 receives no default
    # signal disposition, so a shell in front of it never forwards the signal.
    script = _script()
    assert "exec gunicorn " in script
    assert "exec python -m careagents.worker" in script
    for branch in ("web)", "worker)"):
        body = script.split(branch, 1)[1].split(";;", 1)[0]
        assert body.lstrip().startswith("exec "), (
            f"the {branch} branch does not exec: {body!r}")


def test_the_refusal_is_the_default_branch_not_a_fallthrough_to_web():
    script = _script()
    catch_all = script.split("*)", 1)[1]
    assert re.search(r"\bexit [1-9]", catch_all), (
        "the catch-all branch must exit non-zero")
    assert "gunicorn" not in catch_all, (
        "the catch-all branch must not reach the web command")
    assert script.index("*)") > script.index("worker)"), (
        "the catch-all must come after the real roles")
