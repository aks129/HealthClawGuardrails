#!/usr/bin/env python3
"""
scripts/bot_commands.py

Shared command helper invoked by each OpenClaw agent (Sally, Mary, Dom,
Shervin, Ronny, Joe, Kristy) when the user sends a slash command in
Telegram. Each agent's AGENTS.md tells the LLM which commands to
handle and to exec this script for the mechanics.

Deployed to the Mac mini at ~/.healthclaw/commands.py by
scripts/bot_commands_install.sh. Reads secrets from ~/.healthclaw/env
(preferred) or ~/.kristy/env (fallback for co-install).

Design goals:
  - Zero side effects on bad input (prints an error line, exits non-zero).
  - Structured stdout (one fact per line) so the LLM can paraphrase it.
  - No external Python deps beyond `requests` (already installed for kristy).
  - Same STEP_UP_SECRET as Railway → tokens are accepted by the Flask API.

Usage:
  python3 ~/.healthclaw/commands.py <command> [--agent <id>] [--tenant <id>]

Commands:
  dashboard   — mint a 24h signed dashboard URL
  health      — probe Railway Flask + OpenClaw Gateway + Redis
  tasks       — list pending tasks for the tenant
  week        — run the Kristy watcher (kristy only)
  conflicts   — list family-conflict:* pending tasks (kristy only)
  token       — emit a fresh step-up token (5-min TTL)  [dev/debug]
  help        — print command list
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path

import requests


# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------

_ENV_CANDIDATES = (
    Path.home() / ".healthclaw" / "env",
    Path.home() / ".kristy" / "env",  # co-install fallback
)


def _load_env() -> None:
    for path in _ENV_CANDIDATES:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)


def _api_base() -> str:
    return os.environ.get(
        "COMMAND_CENTER_API",
        "https://app.healthclaw.io/command-center/api",
    ).rstrip("/")


def _dashboard_base() -> str:
    return os.environ.get(
        "DASHBOARD_BASE_URL",
        "https://app.healthclaw.io",
    ).rstrip("/")


def _tenant_default() -> str:
    return os.environ.get("DEFAULT_TENANT", "ev-personal")


# ---------------------------------------------------------------------------
# Secrets + tokens
# ---------------------------------------------------------------------------

def _stepup_secret() -> str:
    s = os.environ.get("STEP_UP_SECRET", "").strip()
    if not s:
        print("error: STEP_UP_SECRET not set (~/.healthclaw/env)", file=sys.stderr)
        sys.exit(2)
    return s


def mint_step_up_token(tenant: str, agent: str = "bot") -> str:
    """Same format as r6.stepup.generate_step_up_token: base64url(json).hmac_hex"""
    secret = _stepup_secret()
    payload = {
        "exp": int(time.time()) + 300,
        "tid": tenant,
        "sub": agent,
        "nonce": secrets.token_hex(16),
    }
    p = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    sig = hmac.new(secret.encode(), p.encode(), hashlib.sha256).hexdigest()
    return f"{p}.{sig}"


def mint_dashboard_token(tenant: str, agent: str = "bot") -> str:
    """
    24-hour signed URL access token (itsdangerous format) — accepted by
    Flask's access.verify_access_token. Replicates the server-side
    signing scheme so we can generate links offline.
    """
    # itsdangerous URLSafeTimedSerializer with salt "command-center-access-v1"
    secret = _stepup_secret()  # server's SESSION_SECRET fallback is STEP_UP_SECRET
    import itsdangerous
    s = itsdangerous.URLSafeTimedSerializer(
        secret, salt="command-center-access-v1"
    )
    payload = {"tenant_id": tenant}
    if agent:
        payload["agent_id"] = agent
    return s.dumps(payload)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_dashboard(args) -> int:
    tenant = args.tenant or _tenant_default()
    token = mint_dashboard_token(tenant, args.agent or "bot")
    url = f"{_dashboard_base()}/command-center?tenant={tenant}&t={token}"
    print(url)
    print("valid: 24h")
    return 0


def cmd_health(args) -> int:
    base = _dashboard_base()
    rows = []
    # Flask
    try:
        r = requests.get(f"{base}/r6/fhir/health", timeout=8)
        rows.append(f"flask: HTTP {r.status_code} ({r.elapsed.total_seconds()*1000:.0f}ms)")
    except Exception as exc:
        rows.append(f"flask: unreachable — {exc}")
    # Command center API — need a session, use step-up
    tenant = args.tenant or _tenant_default()
    token = mint_step_up_token(tenant, args.agent or "bot")
    try:
        r = requests.get(
            f"{_api_base()}/system",
            headers={"X-Step-Up-Token": token, "X-Tenant-Id": tenant},
            timeout=8,
        )
        if r.status_code == 200:
            d = r.json()
            gw = d.get("openclaw_gateway", {})
            rows.append(
                f"openclaw gateway: "
                f"{'reachable' if gw.get('reachable') else 'unreachable'}"
                f" ({gw.get('status_code','?')}, {gw.get('latency_ms','?')}ms)"
            )
            mcp = d.get("mcp_server", {})
            rows.append(f"mcp server: {'up' if mcp.get('up') else 'down'}")
            redis = d.get("redis", {})
            rows.append(
                "redis: "
                + ("up" if redis.get("up") else f"down (configured={redis.get('configured')})")
            )
        else:
            rows.append(f"system api: HTTP {r.status_code}")
    except Exception as exc:
        rows.append(f"system api: error — {exc}")
    for row in rows:
        print(row)
    return 0


def cmd_tasks(args) -> int:
    tenant = args.tenant or _tenant_default()
    token = mint_step_up_token(tenant, args.agent or "bot")
    try:
        r = requests.get(
            f"{_api_base()}/tasks",
            params={"tenant": tenant, "limit": 20},
            headers={"X-Step-Up-Token": token, "X-Tenant-Id": tenant},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as exc:
        print(f"error fetching tasks: {exc}", file=sys.stderr)
        return 1
    tasks = r.json()
    if not tasks:
        print("No pending tasks.")
        return 0
    print(f"Pending tasks ({len(tasks)}):")
    for t in tasks:
        print(f"- [{t.get('priority','?'):8s}] {t.get('title','?')}")
        if t.get("agent_emoji") or t.get("agent_name"):
            print(f"    for: {t.get('agent_emoji','')} {t.get('agent_name','?')}")
        if t.get("source"):
            print(f"    source: {t['source']}")
    return 0


def cmd_conflicts(args) -> int:
    """List current family-conflict:* pending tasks (Kristy-specific filter)."""
    tenant = args.tenant or _tenant_default()
    token = mint_step_up_token(tenant, args.agent or "bot")
    try:
        r = requests.get(
            f"{_api_base()}/tasks",
            params={"tenant": tenant, "limit": 50},
            headers={"X-Step-Up-Token": token, "X-Tenant-Id": tenant},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    rows = [
        t for t in r.json()
        if (t.get("resource_ref") or "").startswith("family-conflict:")
    ]
    if not rows:
        print("No family schedule conflicts pending.")
        return 0
    print(f"Family conflicts pending ({len(rows)}):")
    for t in rows:
        print(f"- [{t.get('priority','?'):8s}] {t.get('title','?')}")
        desc = (t.get("description") or "").split("\n", 1)[0]
        if desc:
            print(f"    {desc}")
    return 0


def cmd_week(args) -> int:
    """Run the Kristy watcher (Kristy persona only)."""
    watcher = Path.home() / ".kristy" / "watcher.py"
    if not watcher.exists():
        print(f"error: watcher not installed at {watcher}", file=sys.stderr)
        return 1
    os.execv("/usr/bin/python3", ["python3", str(watcher)])
    return 0  # unreachable


def cmd_token(args) -> int:
    """Emit a fresh step-up token — useful for curl debugging."""
    tenant = args.tenant or _tenant_default()
    token = mint_step_up_token(tenant, args.agent or "bot")
    print(token)
    print(f"tenant: {tenant}")
    print(f"ttl: 300s")
    return 0


def cmd_help(args) -> int:
    print(
        "HealthClaw bot commands:\n"
        "  /dashboard   — fresh 24h signed command-center link\n"
        "  /health      — stack health (Flask, MCP, gateway, Redis)\n"
        "  /tasks       — pending tasks for your tenant\n"
        "  /token       — step-up token (5min) for dev/debug\n"
        "  /help        — this list\n"
        "\nPersona-specific commands:\n"
        "  /week        — run Kristy's schedule scan (Kristy only)\n"
        "  /conflicts   — family schedule conflicts (Kristy only)"
    )
    return 0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_COMMANDS = {
    "dashboard": cmd_dashboard,
    "health":    cmd_health,
    "tasks":     cmd_tasks,
    "conflicts": cmd_conflicts,
    "week":      cmd_week,
    "token":     cmd_token,
    "help":      cmd_help,
}


def main() -> int:
    _load_env()

    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("command", choices=list(_COMMANDS.keys()))
    p.add_argument("--agent", default=None, help="agent id (sally, mary, dom, ...)")
    p.add_argument("--tenant", default=None, help="tenant id (default: $DEFAULT_TENANT)")
    args = p.parse_args()

    return _COMMANDS[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
