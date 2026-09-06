"""`deploy/careagents/deploy.sh` deploys to a host that is no longer production.

The script rsyncs CareAgents to the careagents.cloud VPS (187.77.4.50) and
points that host's nginx at the app. Users stopped landing there: careagents.cloud
resolves to Railway's edge, it and `careagents-production.up.railway.app` report
the same build, and `scripts/prod_watch.py` watches only the Railway host (#289).
#264 asked for exactly that in August — one origin, VPS path retired — and the
script was never told.

Running it now would ship to an unwatched second origin against the same account
store, which is #258's shape with a passkey failure on top. So it refuses, and
these tests are what keep it refusing. They run the real script with `ssh`,
`rsync` and `scp` stubbed on PATH and assert no stub is ever called — a refusal
that happens after the first `ssh` is not a refusal — and that
`careagents/BUILD_SHA` is untouched, because stamping is a write into the
checkout that happens before the first remote call.

What they do not prove: nothing here contacts the VPS, so these say what the
script does, not what the host is running.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "careagents" / "deploy.sh"
MARKER = REPO / "careagents" / "BUILD_SHA"

# Every remote-side command the script reaches for. Each records its argv and
# succeeds, so a script that got past the refusal would run to completion here
# rather than dying on a missing binary and looking like a refusal.
STUB = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$STUB_LOG"
exit 0
"""


@pytest.fixture
def stubs(tmp_path):
    """`ssh`/`rsync`/`scp` on PATH that log instead of reaching a host."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("ssh", "rsync", "scp"):
        stub = bindir / name
        stub.write_text(STUB)
        stub.chmod(0o755)
    return bindir, tmp_path / "stub.log"


def _run(stubs, *args):
    bindir, log = stubs
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["STUB_LOG"] = str(log)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
    )


def test_the_vps_deploy_refuses_and_reaches_no_host(stubs):
    _, log = stubs
    before = MARKER.read_bytes() if MARKER.exists() else None

    r = _run(stubs)

    assert r.returncode != 0, "the retired VPS deploy must not succeed"
    assert not log.exists(), f"it contacted the host: {log.read_text()}"
    after = MARKER.read_bytes() if MARKER.exists() else None
    assert after == before, "it stamped BUILD_SHA into the checkout"


def test_a_host_argument_does_not_re_enable_it(stubs):
    # The script takes `[user@host]`, so a different target is the obvious way
    # someone would try to keep using it. It is still the retired path.
    _, log = stubs
    r = _run(stubs, "root@example.invalid")
    assert r.returncode != 0
    assert not log.exists(), f"it contacted the host: {log.read_text()}"


def test_the_refusal_names_the_live_path(stubs):
    # A refusal that does not say what to run instead gets worked around.
    r = _run(stubs)
    said = r.stdout + r.stderr
    assert "docs/runbooks/careagents-durable-worker.md" in said
    assert "railway up" in said
    assert "careagents.cloud" in said
