#!/usr/bin/env python3
"""
Live guardrail telemetry for stage demos — run this in a terminal next to the
Telegram agent. It streams, per tenant:

  • each guarded OPERATION as it happens (FHIR read, redaction, step-up write,
    real-world action) with its audit DETAIL — the output/context, not just
    "a read happened"
  • which HealthClaw SKILLS are active (recent activity)
  • a periodic record-inventory snapshot

It reads the immutable audit trail + the command-center projection — the same
surfaces an auditor or the dashboard sees. Nothing here can mutate data. It
shells out to `curl` (system CA store) so it works on any machine's SSL config,
and mints/refreshes a short-lived step-up token via the server (no local secret).

Usage:
  python3 scripts/demo_telemetry.py --tenant ev-personal
  python3 scripts/demo_telemetry.py --tenant desktop-demo --base-url https://app.healthclaw.io
"""

import argparse
import json
import subprocess
import time

ICON = {
    "read": "👁  read",
    "create": "✏️  create",
    "update": "✎  update",
    "delete": "🗑  delete",
    "validate": "✔  validate",
}

# Self/meta telemetry — the watcher's own polls + the FHIR control panel.
# Suppress so the stage feed shows only real patient-data operations.
_META_REFS = ("inventory", "profile-adherence")
_META_DETAIL = ("$inventory", "$profile-adherence", "sources-summary")


def _curl(args, timeout=8):
    out = subprocess.run(["curl", "-s", "--max-time", str(timeout)] + args,
                         capture_output=True, text=True, timeout=timeout + 4)
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError(f"curl rc={out.returncode}")
    return json.loads(out.stdout)


def _get(base, path, tenant, token=None, timeout=8):
    h = ["-H", f"X-Tenant-Id: {tenant}", "-H", "Accept: application/json"]
    if token:
        h += ["-H", f"X-Step-Up-Token: {token}"]
    return _curl(h + [f"{base}{path}"], timeout)


def _mint_token(base, tenant):
    try:
        d = _curl(["-X", "POST", f"{base}/r6/fhir/internal/step-up-token",
                   "-H", f"X-Tenant-Id: {tenant}", "-H", "Content-Type: application/json",
                   "-d", json.dumps({"tenant_id": tenant})])
        return d.get("token")
    except Exception:  # noqa: BLE001
        return None


def _is_meta(ev):
    if (ev.get("resource_id") or "") in _META_REFS:
        return True
    detail = ev.get("detail") or ""
    return any(detail.startswith(m) for m in _META_DETAIL)


def _op_line(ev):
    icon = ICON.get(ev.get("event_type", ""), f"•  {ev.get('event_type','?')}")
    when = (ev.get("recorded") or "")[11:19]
    rt = ev.get("resource_type") or ""
    rid = ev.get("resource_id") or ""
    what = f"{rt}/{rid}" if rt and rid else rt
    agent = ev.get("agent_name") or ev.get("agent_id") or "system"
    detail = (ev.get("detail") or "").replace("\n", " ")
    if len(detail) > 64:
        detail = detail[:61] + "…"
    ok = "✅" if ev.get("outcome") == "success" else "⚠️ " + (ev.get("outcome") or "")
    out = f"  {when}  {icon:<12} {what:<26} {agent:<20} {ok}"
    if detail:
        out += f"\n             ↳ {detail}"
    return out


def _skills_line(base, tenant, token):
    try:
        skills = _get(base, f"/command-center/api/skills?tenant={tenant}", tenant, token)
        active = [s for s in skills if (s.get("recent_activity_count") or 0) > 0]
        if not active:
            return None
        parts = " · ".join(f"{s['name']}({s['recent_activity_count']})" for s in active[:8])
        return f"🛠  skills active: {parts}"
    except Exception:  # noqa: BLE001
        return None


def _inventory_line(base, tenant):
    try:
        data = _get(base, "/r6/fhir/$inventory", tenant)
        p = {x["name"]: x for x in data.get("parameter", [])}
        total = p.get("total", {}).get("valueInteger", 0)
        by = p.get("byType", {}).get("part", [])
        top = "  ".join(f"{x['name']}:{x['valueInteger']}" for x in by[:6])
        return f"📊 {tenant}: {total} resources  |  {top}"
    except Exception as exc:  # noqa: BLE001
        return f"📊 {tenant}: (inventory unavailable: {type(exc).__name__})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--base-url", default="https://app.healthclaw.io")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--snapshot-every", type=int, default=30)
    ap.add_argument("--skills-every", type=int, default=8)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    print(f"── HealthClaw live telemetry · tenant={args.tenant} · {base} ──", flush=True)
    print(_inventory_line(base, args.tenant), flush=True)

    token = _mint_token(base, args.tenant)
    token_at = time.monotonic()
    src = "command-center (skills + output)" if token else "audit trail (tenant-read)"
    print(f"── watching {src} — Ctrl-C to stop ──", flush=True)
    sl = _skills_line(base, args.tenant, token)
    if sl:
        print(sl, flush=True)

    seen = set()
    # Prime: mark existing events seen so only NEW activity shows.
    try:
        for ev in _get(base, f"/command-center/api/actions?tenant={args.tenant}&limit=100",
                       args.tenant, token):
            if ev.get("id"):
                seen.add(ev["id"])
    except Exception:  # noqa: BLE001
        pass

    polls = 0
    while True:
        polls += 1
        # Refresh the step-up token before its 5-min TTL lapses.
        if token and time.monotonic() - token_at > 240:
            new = _mint_token(base, args.tenant)
            if new:
                token, token_at = new, time.monotonic()

        try:
            events = _get(base, f"/command-center/api/actions?tenant={args.tenant}&limit=50",
                          args.tenant, token)
            fresh = [e for e in events if e.get("id") and e["id"] not in seen]
            for ev in reversed(fresh):  # oldest-first
                seen.add(ev["id"])
                if _is_meta(ev):
                    continue
                print(_op_line(ev), flush=True)
        except Exception as exc:  # noqa: BLE001 — never let a blip kill the feed
            print(f"  (poll error: {type(exc).__name__})", flush=True)

        if polls % args.skills_every == 0:
            sl = _skills_line(base, args.tenant, token)
            if sl:
                print(sl, flush=True)
        if polls % args.snapshot_every == 0:
            print(_inventory_line(base, args.tenant), flush=True)

        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n── telemetry stopped ──", flush=True)
