#!/usr/bin/env python3
"""Export patient health records from HealthEx MCP to JSON or NDJSON.

PHI is redacted pre-serialization via HealthClaw Guardrails rules. The raw
MCP response is never written to disk.

Usage:
    HEALTHEX_AUTH_TOKEN=<token> python scripts/export_healthex_mcp.py \\
        --tenant-id ev-personal \\
        --output exports/healthex-$(date +%Y-%m-%d).json

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

# Allow importing sibling module when invoked as `python scripts/export_...py`.
sys.path.insert(0, str(Path(__file__).parent))
from healthclaw_redact import redact, redact_via_proxy, RedactionStats  # noqa: E402

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


HEALTHEX_MCP_URL = os.environ.get("HEALTHEX_MCP_URL", "https://api.healthex.io/mcp")

DEFAULT_TOOLS: list[str] = [
    "get_health_summary",
    "get_conditions",
    "get_medications",
    "get_allergies",
    "get_immunizations",
    "get_vitals",
    "get_labs",
    "get_procedures",
    "get_visits",
    "search_clinical_notes",
]


def _unwrap(result) -> object:
    """Pull a Python value out of an MCP CallToolResult."""
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


def _apply_redaction(
    payload: object,
    mode: str,
    tenant_id: str,
    healthclaw_url: str,
) -> tuple[object, RedactionStats]:
    if mode == "none":
        return payload, RedactionStats()
    if mode == "proxy":
        return redact_via_proxy(payload, healthclaw_url, tenant_id)
    return redact(payload)


def _split_for_ndjson(tool: str, data: object) -> list[dict]:
    """Produce one NDJSON line per logical resource when possible."""
    rows: list[dict] = []

    def _emit(item: object) -> None:
        if isinstance(item, dict) and "resourceType" in item:
            rows.append({
                "tool": tool,
                "resource_type": item["resourceType"],
                "data": item,
            })
        else:
            rows.append({"tool": tool, "resource_type": None, "data": item})

    # FHIR Bundle
    if isinstance(data, dict) and data.get("resourceType") == "Bundle":
        for entry in data.get("entry", []) or []:
            resource = entry.get("resource") if isinstance(entry, dict) else None
            if resource:
                _emit(resource)
        if not rows:
            rows.append({"tool": tool, "resource_type": "Bundle", "data": data})
        return rows

    if isinstance(data, list):
        for item in data:
            _emit(item)
        return rows or [{"tool": tool, "resource_type": None, "data": data}]

    _emit(data)
    return rows


async def _run_export(
    tenant_id: str,
    token: str,
    tools: list[str],
    skip_refresh: bool,
    redact_mode: str,
    healthclaw_url: str,
) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-HealthEx-Tenant-Id": tenant_id,
    }

    snapshot: dict = {
        "_meta": {
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tenant_id": tenant_id,
            "source": HEALTHEX_MCP_URL,
            "redaction_mode": redact_mode,
            "tools_attempted": list(tools),
        },
        "records": {},
        "errors": {},
    }
    stats = RedactionStats()

    async with streamablehttp_client(HEALTHEX_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            snapshot["_meta"]["server"] = {
                "name": getattr(init.serverInfo, "name", None),
                "version": getattr(init.serverInfo, "version", None),
                "protocol": init.protocolVersion,
            }

            if not skip_refresh:
                try:
                    await session.call_tool("update_records", {})
                    status = await session.call_tool("check_records_status", {})
                    unwrapped = _unwrap(status)
                    snapshot["_meta"]["refresh_status"] = unwrapped
                except Exception as exc:
                    snapshot["errors"]["refresh"] = repr(exc)

            for tool in tools:
                print(f"-> {tool}", file=sys.stderr, flush=True)
                try:
                    result = await session.call_tool(tool, {})
                    raw = _unwrap(result)
                    redacted, tool_stats = _apply_redaction(
                        raw, redact_mode, tenant_id, healthclaw_url
                    )
                    stats.merge(tool_stats)
                    snapshot["records"][tool] = redacted
                except Exception as exc:
                    snapshot["errors"][tool] = repr(exc)
                    print(f"   error: {exc}", file=sys.stderr, flush=True)

    snapshot["_meta"]["redaction_stats"] = stats.as_dict()
    return snapshot


def _write_single_json(snapshot: dict, output: Path, pretty: bool) -> int:
    indent = 2 if pretty else None
    output.write_text(json.dumps(snapshot, indent=indent, default=str))
    return output.stat().st_size


def _write_ndjson(snapshot: dict, output: Path) -> int:
    """Emit one meta line plus one line per logical resource across all tools.
    Errors block is appended as a final meta line if non-empty."""
    lines: list[str] = []
    meta = {"_meta": snapshot["_meta"]}
    lines.append(json.dumps(meta, default=str))

    for tool, data in snapshot["records"].items():
        for row in _split_for_ndjson(tool, data):
            lines.append(json.dumps(row, default=str))

    if snapshot.get("errors"):
        lines.append(json.dumps({"_errors": snapshot["errors"]}, default=str))

    output.write_text("\n".join(lines) + "\n")
    return output.stat().st_size


def _resolve_output_format(output: Path, ndjson_flag: bool) -> str:
    if ndjson_flag:
        return "ndjson"
    if output.suffix.lower() == ".ndjson":
        return "ndjson"
    return "json"


def _resolve_redact_mode(no_redact: bool, mode: str) -> str:
    if no_redact:
        return "none"
    if mode not in ("local", "proxy"):
        raise SystemExit(f"error: --redact-mode must be local or proxy, got {mode!r}")
    return mode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, help="HealthEx tenant identifier")
    parser.add_argument("--output", required=True, type=Path, help="Output file (.json or .ndjson)")
    parser.add_argument("--tools", nargs="*", default=DEFAULT_TOOLS,
                        help="Override the tool list")
    parser.add_argument("--skip-refresh", action="store_true",
                        help="Skip update_records + check_records_status pre-calls")
    parser.add_argument("--no-redact", action="store_true",
                        help="Disable PHI redaction (only for synthetic tenants)")
    parser.add_argument("--redact-mode", default="local", choices=("local", "proxy"),
                        help="local = bundled rules, proxy = POST to HealthClaw")
    parser.add_argument("--healthclaw-url", default="http://localhost:5000",
                        help="HealthClaw guardrail proxy base URL")
    parser.add_argument("--ndjson", action="store_true",
                        help="Force NDJSON output regardless of extension")
    parser.add_argument("--compact", action="store_true",
                        help="Minified JSON (no effect on NDJSON)")
    args = parser.parse_args()

    token = os.environ.get("HEALTHEX_AUTH_TOKEN")
    if not token:
        print("error: HEALTHEX_AUTH_TOKEN is not set", file=sys.stderr)
        return 2

    redact_mode = _resolve_redact_mode(args.no_redact, args.redact_mode)
    fmt = _resolve_output_format(args.output, args.ndjson)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    try:
        snapshot = asyncio.run(_run_export(
            tenant_id=args.tenant_id,
            token=token,
            tools=list(args.tools),
            skip_refresh=args.skip_refresh,
            redact_mode=redact_mode,
            healthclaw_url=args.healthclaw_url,
        ))
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130

    if fmt == "ndjson":
        size = _write_ndjson(snapshot, args.output)
    else:
        size = _write_single_json(snapshot, args.output, pretty=not args.compact)

    stats = snapshot["_meta"]["redaction_stats"]
    ok = len(snapshot["records"])
    failed = len(snapshot["errors"])
    redacted_total = sum(stats.values())
    print(
        f"wrote {args.output} ({size:,} bytes, {ok} tools ok, {failed} failed, "
        f"{redacted_total} fields redacted, mode={redact_mode}, fmt={fmt})",
        file=sys.stderr,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
