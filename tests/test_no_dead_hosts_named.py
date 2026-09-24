"""Guard: we do not name a host that is unregistered and claimable (#626).

`sharponmcp.com` was where the SHARP-on-MCP specification lived. By
2026-09-04 it was not merely unreachable but unregistered (`whois`: no match),
while both MCP servers handed `spec: "https://sharponmcp.com"` to every client
in `initialize` and `/health`. Anyone could register the name and serve what
they chose under a URL our servers called the spec. #629 de-linked the site;
this removes the rest and keeps it removed.

The test is a text search, not a resolution check: the suite has no network,
and a probe would pass the day a squatter puts up a page.

Allowed to keep the name: the evidence inventory that recorded it, the
inventory script that probes it, and tests that explain why it is gone.
MUTATION: put `spec: "https://sharponmcp.com"` back into
services/agent-orchestrator/src/index.ts -> red naming that file.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Hosts we must not name, and why.
DEAD = {
    "sharponmcp.com": "unregistered and claimable (whois: no match, 2026-09-04)",
}

SCANNED = (
    "app.py", "main.py", "README.md", "server.json",
    "r6", "careagents", "templates", "static", "skills", "scripts", "docs",
    "services/agent-orchestrator/src", "adapters", "hermes", "openclaw",
)
SUFFIXES = {".py", ".ts", ".js", ".json", ".md", ".html", ".txt", ".yml", ".yaml"}
ALLOWED = ("docs/evidence/", "scripts/surface_inventory.py")


def _files():
    for entry in SCANNED:
        base = ROOT / entry
        if base.is_file():
            yield base
        elif base.is_dir():
            for path in sorted(base.rglob("*")):
                if "node_modules" in path.parts or "dist" in path.parts:
                    continue
                if path.is_file() and path.suffix in SUFFIXES:
                    yield path


def test_no_dead_host_is_named():
    offenders = []
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(ALLOWED):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        offenders += [f"{rel}: {host}" for host in DEAD if host in text]
    assert not offenders, (
        f"{offenders} name a host in DEAD. It is claimable: drop the name, "
        "or point at a location that answers and is held by the party we mean."
    )


def test_the_scan_reaches_the_mcp_server_source():
    # The site-served copy is gone; the machine-served one was in index.ts.
    # If the scan stops reaching that file, the guard above passes vacuously.
    scanned = {p.relative_to(ROOT).as_posix() for p in _files()}
    assert "services/agent-orchestrator/src/index.ts" in scanned
