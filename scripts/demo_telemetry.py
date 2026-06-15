#!/usr/bin/env python3
"""
Live guardrail telemetry for stage demos — run this in Claude Code (or any
terminal) while the Telegram agent drives the patient flow. Each guarded
operation the agent performs (FHIR read, redaction, step-up write, real-world
action) lands in the immutable audit trail; this tails that trail and prints a
clean, readable line per event, plus a periodic record-inventory snapshot.

Auth: reads use the tenant header only (no secret) — every line you see is the
same redacted, audited surface an agent gets. Nothing here can mutate data.

Usage:
  python3 scripts/demo_telemetry.py --tenant ev-personal
  python3 scripts/demo_telemetry.py --tenant desktop-demo --interval 2 --base-url https://app.healthclaw.io

Run it via Claude Code's Monitor tool to stream each event into the session
live (one chat line per audit event) — that is the "proof terminal" on stage.
"""

import argparse
import json
import subprocess
import sys
import time

ICON = {
    "read": "👁  read",
    "create": "✏️  create",
    "update": "✎  update",
    "delete": "🗑  delete",
    "validate": "✔  validate",
}


def _get(url, tenant, timeout=8):
    """Fetch via curl — uses the system CA store, so it works on any machine
    regardless of the local Python's SSL/cert configuration (a common macOS
    Python.framework gotcha that would otherwise break this on stage)."""
    out = subprocess.run(
        ["curl", "-s", "--max-time", str(timeout),
         "-H", f"X-Tenant-Id: {tenant}", "-H", "Accept: application/json", url],
        capture_output=True, text=True, timeout=timeout + 4,
    )
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError(f"curl rc={out.returncode}")
    return json.loads(out.stdout)


def _event_line(ev):
    """Format a FHIR AuditEvent into one stage-readable line."""
    etype = (ev.get("type") or {}).get("display") or "?"
    icon = ICON.get(etype, f"•  {etype}")
    when = (ev.get("recorded") or "")[11:19]  # HH:MM:SS
    ent = (ev.get("entity") or [{}])[0]
    what = (ent.get("what") or {}).get("reference") or ""
    agent = ((ev.get("agent") or [{}])[0].get("who") or {}).get("display") or "system"
    outcome = (ev.get("outcome") or {}).get("code", {}).get("display", "")
    flag = "✅" if outcome == "Success" else ("⚠️ " + outcome if outcome else "")
    return f"  {when}  {icon:<12} {what:<28} {agent:<22} {flag}"


def _inventory_line(base, tenant):
    try:
        data = _get(f"{base}/r6/fhir/$inventory", tenant)
        p = {x["name"]: x for x in data.get("parameter", [])}
        total = p.get("total", {}).get("valueInteger", 0)
        by = p.get("byType", {}).get("part", [])
        top = "  ".join(
            f"{x['name']}:{x['valueInteger']}" for x in by[:6]
        )
        return f"📊 {tenant}: {total} resources  |  {top}"
    except Exception as exc:  # noqa: BLE001
        return f"📊 {tenant}: (inventory unavailable: {type(exc).__name__})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--base-url", default="https://app.healthclaw.io")
    ap.add_argument("--interval", type=float, default=2.0, help="poll seconds")
    ap.add_argument("--snapshot-every", type=int, default=15,
                    help="print inventory snapshot every N polls")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    print(f"── HealthClaw live telemetry · tenant={args.tenant} · {base} ──", flush=True)
    print(_inventory_line(base, args.tenant), flush=True)
    print("── watching audit trail (Ctrl-C to stop) ──", flush=True)

    seen = set()
    # Prime: mark existing events as seen so we only show NEW activity.
    try:
        b = _get(f"{base}/r6/fhir/AuditEvent?_count=50&_sort=-_lastUpdated", args.tenant)
        for e in b.get("entry", []):
            rid = (e.get("resource") or {}).get("id")
            if rid:
                seen.add(rid)
    except Exception:  # noqa: BLE001
        pass

    polls = 0
    while True:
        polls += 1
        try:
            b = _get(f"{base}/r6/fhir/AuditEvent?_count=50&_sort=-_lastUpdated", args.tenant)
            fresh = [e.get("resource") or {} for e in b.get("entry", [])]
            fresh = [r for r in fresh if r.get("id") and r["id"] not in seen]
            for r in reversed(fresh):  # oldest-first for readable chronology
                seen.add(r["id"])
                # Suppress the watcher's / control-panel's own meta-telemetry
                # reads ($inventory, $profile-adherence) — they aren't patient
                # operations and would clutter the stage feed with self-noise.
                ent = (r.get("entity") or [{}])[0]
                ref = (ent.get("what") or {}).get("reference") or ""
                if ref in ("Parameters/inventory", "Parameters/profile-adherence"):
                    continue
                print(_event_line(r), flush=True)
        except Exception as exc:  # noqa: BLE001 — never let a blip kill the feed
            print(f"  (poll error: {type(exc).__name__})", flush=True)

        if polls % args.snapshot_every == 0:
            print(_inventory_line(base, args.tenant), flush=True)

        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n── telemetry stopped ──", flush=True)
        sys.exit(0)
