"""Guard: a keyless claim must never point at the token-locked MCP endpoint.

There are two hosted MCP deployments and they differ only in a URL:

  * the **demo** server  — `MCP_PUBLIC_DEMO=true`, no credential, hard-pinned
    to a synthetic tenant;
  * the **production** server — `MCP_AUTH_TOKEN` set, 401 without a bearer.

#194 locked the production endpoint and moved the keyless pitch to the demo
server, but only `README.md` was updated. Every per-client quickstart kept
telling readers to paste the *production* URL and promising "no login screen
appears". The result was a connector that adds fine and then fails at sign-in
with "Couldn't register", because a 401 makes MCP clients start an OAuth flow
against a server that publishes no OAuth metadata.

Nothing caught it: the URL was reachable, the tests were green, and the claim
and the URL sat on adjacent lines in a file no test read. It took a design
partner four days later to report it.

So this asserts the invariant that was violated, rather than pinning the
current URLs: **wherever a doc promises keyless access, the endpoint nearest
that promise must be the keyless one.** Swapping either host string keeps the
test meaningful; deleting the pairing is what it exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The two deployments, identified by host. Kept as bare hosts so the check
# fires for any path or scheme (/mcp, /mcp/rpc, /health, curl lines, JSON).
DEMO_HOST = "mcp-demo-production-ee2c.up.railway.app"
LOCKED_HOST = "mcp-server-production-5112.up.railway.app"

# Phrases that promise a reader they need no credential. Deliberately narrow:
# a bare "none" in a comparison table is not a promise, "Authentication: none"
# in a setup step is.
KEYLESS_CLAIM = re.compile(
    r"""
      no\s+api\s+key
    | no\s+key\s+required
    | without\s+credentials
    | no\s+credentials\s+needed
    | no\s+login\s+screen
    | anonymous\s+access
    | authentication:\W*\s*(?:no\s+authentication|none)\b
    | works\s+without\s+credentials
    """,
    re.IGNORECASE | re.VERBOSE,
)

# `templates` and `adapters` are in scope because they were broken too: the
# public homepage handed visitors the locked URL, and all three adapter
# examples used it as their `--mcp-base` default.
SEARCH_DIRS = ("docs", "skills", "hermes", "templates", "adapters")
ROOT_FILES = ("README.md", "server.json", "glama.json", ".mcp.json",
              "gemini-extension.json")
SCAN_SUFFIXES = {".md", ".json", ".yaml", ".yml", ".html", ".py"}
SKIP_PARTS = {"node_modules", ".git", "dist", "__pycache__"}


def _candidate_files() -> list[Path]:
    found: list[Path] = []
    for directory in SEARCH_DIRS:
        base = REPO_ROOT / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix.lower() not in SCAN_SUFFIXES:
                continue
            if SKIP_PARTS & set(path.parts):
                continue
            found.append(path)
    found.extend(REPO_ROOT / name for name in ROOT_FILES
                 if (REPO_ROOT / name).is_file())
    return sorted(found)


def _nearest_host(lines: list[str], index: int) -> str | None:
    """Which deployment is this line talking about? None if neither is near.

    Ties resolve to LOCKED: an ambiguous keyless claim is exactly the kind of
    thing a human should disambiguate, not something to wave through.
    """
    best: tuple[int, str] | None = None
    for offset, line in enumerate(lines):
        for host in (DEMO_HOST, LOCKED_HOST):
            if host in line:
                distance = abs(offset - index)
                if best is None or distance < best[0]:
                    best = (distance, host)
                elif distance == best[0] and host == LOCKED_HOST:
                    best = (distance, LOCKED_HOST)
    return None if best is None else best[1]


def test_repo_still_references_both_deployments():
    """Sanity: the corpus this guard scans has not moved out from under it."""
    blob = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                     for p in _candidate_files())
    assert DEMO_HOST in blob, "no demo endpoint in docs — did a host change?"
    assert LOCKED_HOST in blob, "no locked endpoint in docs — host change?"


def test_no_keyless_claim_points_at_the_token_locked_endpoint():
    violations: list[str] = []

    for path in _candidate_files():
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if LOCKED_HOST not in "\n".join(lines):
            continue
        for index, line in enumerate(lines):
            if not KEYLESS_CLAIM.search(line):
                continue
            if _nearest_host(lines, index) == LOCKED_HOST:
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"{rel}:{index + 1}: {line.strip()[:100]}")

    assert not violations, (
        "These lines promise keyless access next to the token-locked endpoint "
        f"({LOCKED_HOST}), which answers 401 and sends MCP clients into an "
        "OAuth flow that cannot complete. Point them at the demo endpoint "
        f"({DEMO_HOST}) or state the bearer requirement:\n  "
        + "\n  ".join(violations)
    )


@pytest.mark.parametrize("path", [
    "docs/quickstarts/claude.md",
    "docs/quickstarts/chatgpt.md",
    "docs/quickstarts/perplexity.md",
    "docs/quickstarts/mcp-generic.md",
    "docs/quickstarts/README.md",
])
def test_consumer_quickstarts_lead_with_the_keyless_endpoint(path):
    """The URL a non-developer pastes into a hosted connector must be keyless.

    Hosted connectors (claude.ai, ChatGPT, Perplexity) cannot attach a static
    bearer header at all, so the locked endpoint is not merely inconvenient
    there — it is unusable. These guides must not be the place it appears
    first.
    """
    lines = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
    first = {}
    for index, line in enumerate(lines):
        for host in (DEMO_HOST, LOCKED_HOST):
            if host in line and host not in first:
                first[host] = index

    assert DEMO_HOST in first, f"{path} never names the keyless endpoint"
    if LOCKED_HOST in first:
        assert first[DEMO_HOST] < first[LOCKED_HOST], (
            f"{path} names the token-locked endpoint (line "
            f"{first[LOCKED_HOST] + 1}) before the keyless one (line "
            f"{first[DEMO_HOST] + 1}); a reader following along top-to-bottom "
            "will paste the one that 401s."
        )


def test_server_json_keyless_remote_is_the_demo_host():
    """Catalog clients surface `remotes` — a remote with no required secret
    header reads as 'try me, no signup', so it had better be the demo."""
    import json

    remotes = json.loads((REPO_ROOT / "server.json").read_text())["remotes"]
    assert remotes, "server.json advertises no remotes"

    for remote in remotes:
        url = remote["url"]
        needs_secret = any(
            header.get("isRequired") and header.get("isSecret")
            for header in remote.get("headers") or []
        )
        if LOCKED_HOST in url:
            assert needs_secret, (
                "server.json advertises the token-locked endpoint without "
                "marking a required secret header; catalogs will present it "
                "as keyless and every visitor gets a 401."
            )
        elif DEMO_HOST in url:
            assert not needs_secret, (
                "server.json demands a secret for the demo endpoint, which "
                "accepts none — visitors will hunt for a credential that "
                "does not exist."
            )
