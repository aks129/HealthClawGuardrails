#!/usr/bin/env python3
"""Export verified health records from Health Bank One's MCP server.

Built to be wired LIVE on a call: every endpoint is env-configurable and the
tool catalog is discovered at runtime (`tools/list`), so nothing is hardcoded
against documentation we don't have yet. PHI is redacted in-process via
HealthClaw Guardrails rules before anything touches disk — same pipeline as
export_healthex_mcp.py.

Quickstart the moment HBO hands you a URL + token:

    export HBO_MCP_URL=https://<their-host>/mcp
    export HBO_ACCESS_TOKEN=<token-from-call>      # or run healthbankone_oauth.py first
    python scripts/export_healthbankone_mcp.py --tenant-id ev-personal-hbo --discover

`--discover` lists the server's tools, prints them, and calls every read-safe
one with empty arguments. Once you know the real names, pin them:

    python scripts/export_healthbankone_mcp.py --tenant-id ev-personal-hbo \\
        --tools health.summary health.medications health.conditions

Token resolution order:
    1. HBO_ACCESS_TOKEN env var
    2. ~/.healthclaw/hbo_tokens.json (written by scripts/healthbankone_oauth.py,
       auto-refreshed here if expired and a refresh_token + HBO_TOKEN_ENDPOINT
       are available)

Requires: mcp>=1.2, httpx. Python 3.10+.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from healthclaw_redact import redact, redact_via_proxy, RedactionStats  # noqa: E402

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

TOKEN_CACHE = Path(os.environ.get(
    "HBO_TOKEN_CACHE", str(Path.home() / ".healthclaw" / "hbo_tokens.json")))

# Tool-name fragments that suggest a write/destructive operation. In discovery
# mode we skip these unless --include-writes is passed; a pull should never
# mutate the consumer's HBO account.
_WRITEISH = ("write", "create", "update", "delete", "send", "sign",
             "revoke", "submit", "post", "engage", "notify")


def _resolve_token() -> str | None:
    token = os.environ.get("HBO_ACCESS_TOKEN", "").strip()
    if token:
        return token
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text())
        except json.JSONDecodeError:
            return None
        expires_at = cached.get("expires_at", 0)
        if expires_at and expires_at < datetime.now(timezone.utc).timestamp():
            refreshed = _try_refresh(cached)
            if refreshed:
                return refreshed
            print("warning: cached HBO token expired and refresh failed",
                  file=sys.stderr)
            return None
        return cached.get("access_token")
    return None


def _try_refresh(cached: dict) -> str | None:
    """Refresh via HBO token endpoint using the cached refresh_token.

    client_id is optional — HBO Bootstrap is a public client (no client_id).
    Falls back to cached token_endpoint so no env vars are required.
    """
    refresh_token = cached.get("refresh_token")
    if not refresh_token:
        return None
    token_endpoint = (
        os.environ.get("HBO_TOKEN_ENDPOINT", "").strip()
        or cached.get("token_endpoint", "").strip()
        or "https://oauth.app.healthbankone.com/token"
    )
    client_id = (
        os.environ.get("HBO_CLIENT_ID", "").strip()
        or cached.get("client_id", "").strip()
    )
    import httpx
    data: dict = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if client_id:
        data["client_id"] = client_id
    secret = os.environ.get("HBO_CLIENT_SECRET", "").strip()
    if secret:
        data["client_secret"] = secret
    try:
        resp = httpx.post(token_endpoint, data=data, timeout=15)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"warning: token refresh failed: {exc}", file=sys.stderr)
        return None
    cached.update({
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", refresh_token),
        "expires_at": datetime.now(timezone.utc).timestamp()
        + int(body.get("expires_in", 3600)) - 60,
    })
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps(cached))
    TOKEN_CACHE.chmod(0o600)
    return body["access_token"]


def _unwrap(result) -> object:
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    payload: list = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            payload.append(json.loads(text))
        except json.JSONDecodeError:
            payload.append(text)
    if len(payload) == 1:
        return payload[0]
    return payload


def _apply_redaction(payload: object, mode: str, tenant_id: str,
                     healthclaw_url: str) -> tuple[object, RedactionStats]:
    if mode == "none":
        return payload, RedactionStats()
    if mode == "proxy":
        return redact_via_proxy(payload, healthclaw_url, tenant_id)
    return redact(payload)


def _is_read_safe(tool) -> bool:
    """Heuristic + annotation check for whether a discovered tool is read-only."""
    annotations = getattr(tool, "annotations", None)
    if annotations is not None:
        read_only = getattr(annotations, "readOnlyHint", None)
        if read_only is True:
            return True
        if read_only is False:
            return False
    name = (getattr(tool, "name", "") or "").lower()
    return not any(frag in name for frag in _WRITEISH)


async def _run_export(tenant_id: str, token: str, mcp_url: str,
                      tools: list[str] | None, discover: bool,
                      include_writes: bool, redact_mode: str,
                      healthclaw_url: str, extra_args: dict) -> dict:
    headers = {"Authorization": f"Bearer {token}"}

    snapshot: dict = {
        "_meta": {
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tenant_id": tenant_id,
            "source": mcp_url,
            "redaction_mode": redact_mode,
        },
        "records": {},
        "errors": {},
    }
    stats = RedactionStats()

    async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            snapshot["_meta"]["server"] = {
                "name": getattr(init.serverInfo, "name", None),
                "version": getattr(init.serverInfo, "version", None),
                "protocol": init.protocolVersion,
            }
            # Record any SHARP / PromptOpinion FHIR-context advertisement —
            # tells us whether HealthClaw can run in forwarding mode later.
            caps = getattr(init, "capabilities", None)
            experimental = getattr(caps, "experimental", None) or {}
            if isinstance(experimental, dict) and experimental:
                snapshot["_meta"]["server"]["experimental"] = {
                    k: True for k in experimental
                }

            listing = await session.list_tools()
            catalog = {t.name: t for t in listing.tools}
            snapshot["_meta"]["tool_catalog"] = sorted(catalog)
            print(f"server exposes {len(catalog)} tools:", file=sys.stderr)
            for name in sorted(catalog):
                marker = "" if _is_read_safe(catalog[name]) else "  [write — skipped in discovery]"
                print(f"  - {name}{marker}", file=sys.stderr)

            if tools:
                to_call = list(tools)
                unknown = [t for t in to_call if t not in catalog]
                if unknown:
                    print(f"warning: not in catalog: {unknown}", file=sys.stderr)
            elif discover:
                to_call = [n for n, t in sorted(catalog.items())
                           if include_writes or _is_read_safe(t)]
            else:
                to_call = []
                print("nothing to call — pass --discover or --tools", file=sys.stderr)

            for tool in to_call:
                print(f"-> {tool}", file=sys.stderr, flush=True)
                try:
                    result = await session.call_tool(tool, extra_args or {})
                    raw = _unwrap(result)
                    redacted, tool_stats = _apply_redaction(
                        raw, redact_mode, tenant_id, healthclaw_url)
                    stats.merge(tool_stats)
                    snapshot["records"][tool] = redacted
                except Exception as exc:
                    snapshot["errors"][tool] = repr(exc)
                    print(f"   error: {exc}", file=sys.stderr, flush=True)

    snapshot["_meta"]["redaction_stats"] = stats.as_dict()
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant-id", required=True,
                        help="HealthClaw tenant the export is destined for")
    parser.add_argument("--output", default=None,
                        help="Output path (default exports/hbo-<date>.json)")
    parser.add_argument("--tools", nargs="*", default=None,
                        help="Explicit tool names to call (skips discovery filter)")
    parser.add_argument("--discover", action="store_true",
                        help="List server tools and call every read-safe one")
    parser.add_argument("--include-writes", action="store_true",
                        help="In discovery mode, also call write-looking tools (NOT recommended)")
    parser.add_argument("--tool-args", default=None,
                        help='JSON dict passed as arguments to every tool call, e.g. \'{"patient_id":"..."}\'')
    parser.add_argument("--no-redact", dest="redact_mode", action="store_const",
                        const="none", default="local",
                        help="Skip PHI redaction (raw output — handle with care)")
    parser.add_argument("--redact-mode", dest="redact_mode",
                        choices=["local", "proxy", "none"], default="local")
    parser.add_argument("--healthclaw-url",
                        default=os.environ.get("FHIR_BASE_URL",
                                               "http://localhost:5000/r6/fhir"),
                        help="HealthClaw base URL for --redact-mode proxy")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    mcp_url = os.environ.get(
        "HBO_MCP_URL", "https://mcp.app.healthbankone.com/mcp").strip()

    token = _resolve_token()
    if not token:
        print("error: no HBO token.\n"
              "  Either:  export HBO_ACCESS_TOKEN=<token>\n"
              "  Or run:  python scripts/healthbankone_oauth.py authorize",
              file=sys.stderr)
        return 2

    extra_args = json.loads(args.tool_args) if args.tool_args else {}

    snapshot = asyncio.run(_run_export(
        tenant_id=args.tenant_id, token=token, mcp_url=mcp_url,
        tools=args.tools, discover=args.discover,
        include_writes=args.include_writes, redact_mode=args.redact_mode,
        healthclaw_url=args.healthclaw_url, extra_args=extra_args))

    out = Path(args.output or
               f"exports/hbo-{datetime.now(timezone.utc).date().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2 if args.pretty else None,
                              default=str))
    size = out.stat().st_size
    n_ok = len(snapshot["records"])
    n_err = len(snapshot["errors"])
    print(f"wrote {out} ({size:,} bytes) — {n_ok} tools ok, {n_err} errors")
    if snapshot["_meta"].get("redaction_stats"):
        print(f"redaction: {snapshot['_meta']['redaction_stats']}")
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
