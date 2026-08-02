"""The Railway worker snippet in the runbook is a command an operator pastes.

`docs/runbooks/careagents-durable-worker.md` tells whoever creates the worker
service to enumerate the web service's variables rather than hand-pick them,
because hand-picking is what produced the crash-loop QA found in review (the
worker runs the same unconditional `Config()` the web app does, so production
`_require`s `CARE_SESSION_SECRET` and `RESEND_API_KEY` from a process with no
sessions that sends no email).

The snippet is executable, so test it as code. These run the block extracted
from the runbook against a stub `railway` on PATH, under every shell installed,
and assert on the argv the stub receives: one `--variables` per name, every
value a literal `${{web.NAME}}` reference, no secret value anywhere.

What they do not prove: no Railway API is called and no service is created. The
stub's variable list stands in for the live service, so these check the shape of
what would be sent, not that the live service still carries those names.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUNBOOK = REPO / "docs" / "runbooks" / "careagents-durable-worker.md"

# Stands in for `railway variables list --service careagents --kv`: the names
# the live web service carries, with values shaped like the real secrets so a
# leak would be visible. `add` records its argv.
STUB_RAILWAY = """#!/bin/sh
case "$1" in
  variables|variable)
    cat <<'VARS'
CARE_DATABASE_URL=postgresql://care:S3cr3tPassw0rd@postgres-bfs.railway.internal:5432/railway
CARE_EMAIL_FROM=CareAgents <hello@careagents.cloud>
CARE_ENV=production
CARE_IMESSAGE_HANDLE=+15550100
CARE_MODEL=claude-sonnet-5
CARE_OPENAI_MODEL=gpt-4o-mini
CARE_ORIGIN=https://careagents.cloud
CARE_RP_ID=careagents.cloud
CARE_RP_NAME=CareAgents
CARE_SESSION_SECRET=RtQm9xPLv2eWs7YbKd4NfHj6Uz1AoCg3=
CARE_TELEGRAM_BOT=example_bot
FASTEN_PUBLIC_KEY=public_test_ZmFrZWtleWZvcnFhb25seQ==
HEALTHCLAW_BASE=https://app.healthclaw.io
HEALTHCLAW_MINT_SECRET=mint_9f3a2b1c8d7e6f5a4b3c2d1e0f9a8b7c
OPENAI_API_KEY=sk-proj-QAFAKEKEYQAFAKEKEYQAFAKEKEY
OPENAI_BASE_URL=https://api.openai.com/v1
RESEND_API_KEY=re_QAFAKE_1234567890abcdef
PORT=8600
RAILWAY_PRIVATE_DOMAIN=careagents.railway.internal
RAILWAY_PROJECT_NAME=awake-serenity
RAILWAY_PUBLIC_DOMAIN=careagents-production.up.railway.app
RAILWAY_SERVICE_NAME=careagents
VARS
    ;;
  add)
    : > "$STUB_ADD_ARGV"
    for a in "$@"; do printf '%s\\n' "$a" >> "$STUB_ADD_ARGV"; done
    ;;
  *) echo "stub: unhandled $*" >&2; exit 1 ;;
esac
"""

# Every name the snippet must forward: the stub's list minus RAILWAY_*, PORT
# and CARE_ROLE. These are exactly the 17 the live web service carries, and
# they include the two whose absence crash-loops the worker.
EXPECTED_NAMES = [
    "CARE_DATABASE_URL", "CARE_EMAIL_FROM", "CARE_ENV", "CARE_IMESSAGE_HANDLE",
    "CARE_MODEL", "CARE_OPENAI_MODEL", "CARE_ORIGIN", "CARE_RP_ID",
    "CARE_RP_NAME", "CARE_SESSION_SECRET", "CARE_TELEGRAM_BOT",
    "FASTEN_PUBLIC_KEY", "HEALTHCLAW_BASE", "HEALTHCLAW_MINT_SECRET",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "RESEND_API_KEY",
]

# Fragments of the stub's values. None may appear in the argv, in stdout, or in
# stderr: a reference is the whole point, and an argv is world-readable in the
# process table and lands in shell history.
SECRET_FRAGMENTS = [
    "S3cr3tPassw0rd", "RtQm9xPLv2eWs7YbKd4NfHj6Uz1AoCg3",
    "mint_9f3a2b1c8d7e6f5a4b3c2d1e0f9a8b7c",
    "sk-proj-QAFAKEKEYQAFAKEKEYQAFAKEKEY", "re_QAFAKE_1234567890abcdef",
    "ZmFrZWtleWZvcnFhb25seQ",
]

# The operator's shell is whatever their terminal runs. On this project's
# machines that is zsh; CI is Linux bash. Both must produce the same service.
SHELLS = [name for name in ("bash", "zsh", "sh") if shutil.which(name)]


def _snippet() -> str:
    """The fenced block in the runbook that creates the worker service."""
    blocks = re.findall(r"```bash\n(.*?)```", RUNBOOK.read_text(), re.S)
    matching = [b for b in blocks if "railway add" in b]
    assert len(matching) == 1, (
        f"expected exactly one worker-creation snippet, found {len(matching)}")
    return matching[0]


@pytest.fixture(scope="module")
def stub(tmp_path_factory):
    path = tmp_path_factory.mktemp("railway-stub")
    railway = path / "railway"
    railway.write_text(STUB_RAILWAY)
    railway.chmod(0o755)
    return path


def _run(stub, shell):
    argv_file = stub / f"argv-{shell}"
    if argv_file.exists():
        argv_file.unlink()
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}",
           "STUB_ADD_ARGV": str(argv_file)}
    result = subprocess.run([shell, "-c", _snippet()], env=env,
                            capture_output=True, text=True)
    argv = argv_file.read_text().splitlines() if argv_file.exists() else []
    return result, argv


def _pairs(argv):
    """The KEY=VALUE arguments that followed each --variables flag."""
    return [argv[i + 1] for i, a in enumerate(argv)
            if a == "--variables" and i + 1 < len(argv)]


@pytest.mark.parametrize("shell", SHELLS)
def test_the_snippet_forwards_every_variable_as_its_own_flag(stub, shell):
    # The failure this pins is not a crash. zsh does not word-split unquoted
    # parameter expansions, so `for name in $NAMES` iterates once over the whole
    # newline-joined list and the loop emits ONE malformed --variables holding
    # every name at once. `railway add` then creates a worker with CARE_ROLE and
    # one garbage variable: CARE_ENV is unset, so Config() takes the development
    # path, raises nothing, and the container runs green while claiming nothing.
    # The runbook's own troubleshooting step — look for a ConfigError — finds
    # none.
    result, argv = _run(stub, shell)
    assert result.returncode == 0, result.stderr
    pairs = _pairs(argv)
    assert "CARE_ROLE=worker" in pairs
    names = [p.split("=", 1)[0] for p in pairs if p != "CARE_ROLE=worker"]
    assert names == EXPECTED_NAMES, (
        f"under {shell} the snippet sent {len(names)} variable(s) instead of "
        f"{len(EXPECTED_NAMES)}")


@pytest.mark.parametrize("shell", SHELLS)
def test_the_snippet_sends_references_not_values(stub, shell):
    result, argv = _run(stub, shell)
    for pair in _pairs(argv):
        if pair == "CARE_ROLE=worker":
            continue
        assert "=" in pair, (
            f"under {shell} the snippet sent a --variables argument that is "
            f"not a KEY=VALUE pair: {pair!r}")
        name, value = pair.split("=", 1)
        assert value == "${{careagents.%s}}" % name, (
            f"{name} was not sent as a Railway reference: {value!r}")


@pytest.mark.parametrize("shell", SHELLS)
def test_no_secret_value_reaches_the_argv_or_the_terminal(stub, shell):
    # `railway variables list --kv` prints raw values by design. They must stay
    # inside the pipe: an argv is readable from the process table and is written
    # to shell history, and stdout/stderr land in a terminal scrollback or a CI
    # log.
    result, argv = _run(stub, shell)
    haystack = "\n".join(argv) + result.stdout + result.stderr
    for fragment in SECRET_FRAGMENTS:
        assert fragment not in haystack, (
            f"a secret value escaped under {shell}: {fragment!r}")


@pytest.mark.parametrize("shell", SHELLS)
def test_railway_managed_and_role_variables_are_not_mirrored(stub, shell):
    # RAILWAY_* are injected per service and PORT is assigned per service, so
    # mirroring either points the worker at the web service's identity.
    # CARE_ROLE must come from the explicit `CARE_ROLE=worker`, never the web
    # service's `web`.
    _, argv = _run(stub, shell)
    names = [p.split("=", 1)[0] for p in _pairs(argv)]
    assert not [n for n in names if n.startswith("RAILWAY_")]
    assert "PORT" not in names
    assert names.count("CARE_ROLE") == 1
    assert "CARE_ROLE=worker" in _pairs(argv)


def test_the_enumerated_set_satisfies_the_production_config_requirements():
    # The defect that motivated the snippet: careagents/worker.py builds the
    # same Config() the web app does, so production refuses to boot without
    # these. Whatever the snippet forwards has to cover them.
    required = {"CARE_SESSION_SECRET", "HEALTHCLAW_MINT_SECRET",
                "RESEND_API_KEY"}
    assert required <= set(EXPECTED_NAMES)
    assert {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"} & set(EXPECTED_NAMES), (
        "the worker needs an LLM credential or every run fails at inference")
    config = (REPO / "careagents" / "config.py").read_text()
    for name in required:
        assert f'_require("{name}"' in config, (
            f"{name} is no longer required — re-check what the worker needs")


def test_the_runbook_does_not_send_the_operator_back_to_a_repo_build():
    # A repo-connected build picks up the repo-root railway.toml, hence the
    # repo-root Dockerfile, which is the HealthClaw Flask app.
    railway_section = RUNBOOK.read_text().split("## Railway", 1)[1]
    assert "Do not create the service from the GitHub repo" in railway_section
    assert "railway.toml" in railway_section
    assert "--repo" not in railway_section
