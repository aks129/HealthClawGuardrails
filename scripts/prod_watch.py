#!/usr/bin/env python3
"""Production watch — is the live product actually working right now?

    python scripts/prod_watch.py                  # all deployments
    python scripts/prod_watch.py --json           # machine-readable

Exits non-zero if any check fails, so a scheduled job can open an issue.

Why this exists
---------------
There was no scheduled verification of production at all. The sign-in code
input silently truncated 8-digit codes to 6 (#181) — UI login was broken for
every user, in production, and it was found by hand rather than by any alarm.
The MCP server fail-closed on a missing token and stayed down until someone
noticed.

Scope, stated honestly
----------------------
Every check here is UNAUTHENTICATED on purpose, so this can run from CI with no
credentials and no synthetic account to keep alive. That buys the catastrophic
cases — deploy broke, database unreachable, guardrails regressed, labels
regressed, MCP down — and it does NOT cover the signed-in journey (signup →
email code → connect → chat → intake form).

Covering that needs an inbox the runner can read, which is a real piece of work
and a real decision; it is tracked separately rather than faked here. A monitor
that quietly checks less than it appears to is worse than one with a narrow,
stated scope.
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

G, R, Y, D, X = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"

HEALTHCLAW = "https://app.healthclaw.io"
CAREAGENTS = "https://careagents-production.up.railway.app"
# Two MCP deployments by design: the real one is token-locked, the demo one is
# unauthenticated but hard-pinned to a synthetic tenant.
MCP_LOCKED = "https://mcp-server-production-5112.up.railway.app"
MCP_DEMO = "https://mcp-demo-production-ee2c.up.railway.app"
DEMO_TENANT = "desktop-demo"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    mark = f"{G} OK {X}" if ok else f"{R}FAIL{X}"
    print(f"{mark} {name}" + (f" {D}— {detail}{X}" if detail else ""))
    return ok


def get(url: str, timeout: float, **kw):
    try:
        return requests.get(url, timeout=timeout, **kw)
    except requests.RequestException as exc:
        return type(exc).__name__


def run(timeout: float) -> int:
    # --- the guardrail engine ------------------------------------------------
    r = get(f"{HEALTHCLAW}/r6/fhir/health", timeout)
    check("healthclaw: alive", getattr(r, "status_code", None) == 200,
          str(getattr(r, "status_code", r)))

    r = get(f"{HEALTHCLAW}/r6/fhir/$conformance", timeout)
    grade = None
    if getattr(r, "status_code", None) == 200:
        try:
            grade = (r.json() or {}).get("grade")
        except ValueError:
            pass
    # The public claim is Grade A. If a deployment stops earning it we should
    # hear it from a machine, not from someone reading the site.
    check("healthclaw: guardrail grade A", grade == "A", f"grade={grade}")

    # --- terminology labels (#207) -------------------------------------------
    # Redaction strips every upstream display, so if server-derived labelling
    # regresses, records silently become "unlabeled record, code X" again and
    # the agent loses the ability to name anything it reads.
    r = get(f"{HEALTHCLAW}/r6/fhir/Condition?_count=5", timeout,
            headers={"X-Tenant-Id": DEMO_TENANT})
    labelled = 0
    total = 0
    if getattr(r, "status_code", None) == 200:
        try:
            for entry in (r.json().get("entry") or []):
                res = entry.get("resource") or {}
                if res.get("resourceType") != "Condition":
                    continue
                total += 1
                cc = res.get("code") or {}
                if cc.get("text") or any(c.get("display")
                                         for c in (cc.get("coding") or [])):
                    labelled += 1
        except ValueError:
            pass
    check("healthclaw: records are readable", total > 0 and labelled == total,
          f"{labelled}/{total} labelled")

    # --- the consumer app ----------------------------------------------------
    r = get(f"{CAREAGENTS}/healthz", timeout)
    body = {}
    if getattr(r, "status_code", None) in (200, 503):
        try:
            body = r.json() or {}
        except ValueError:
            pass
    # /healthz round-trips the database, so this catches an app that booted but
    # cannot reach its store — the state a load balancer must not route into.
    check("careagents: ready (db reachable)",
          getattr(r, "status_code", None) == 200 and body.get("accounts") is True,
          f"status={getattr(r, 'status_code', r)} accounts={body.get('accounts')}")

    r = get(f"{CAREAGENTS}/", timeout)
    html = getattr(r, "text", "") or ""
    check("careagents: landing renders",
          getattr(r, "status_code", None) == 200 and "/auth" in html,
          str(getattr(r, "status_code", r)))

    # The sign-in page is the front door; #181 broke exactly this and nothing
    # noticed. Codes are 8 digits — an input that cannot hold 8 is a dead door.
    r = get(f"{CAREAGENTS}/auth", timeout)
    html = getattr(r, "text", "") or ""
    ok_auth = getattr(r, "status_code", None) == 200 and 'maxlength="8"' in html
    check("careagents: sign-in accepts an 8-digit code", ok_auth,
          "maxlength=8 present" if ok_auth else "code input missing or too short")

    # --- MCP servers ---------------------------------------------------------
    r = get(f"{MCP_LOCKED}/health", timeout)
    check("mcp (locked): alive", getattr(r, "status_code", None) == 200,
          str(getattr(r, "status_code", r)))

    # The one non-negotiable on this server: an unauthenticated caller must be
    # REFUSED. 401/403 is the pass. Anything that looks like service — a 200,
    # or a 405 meaning "wrong method, but you're welcome" — is a failure,
    # because it would mean the real tool surface had been left open.
    r = get(f"{MCP_LOCKED}/mcp", timeout)
    code = getattr(r, "status_code", None)
    check("mcp (locked): refuses unauthenticated callers", code in (401, 403),
          f"unauthenticated /mcp -> {code}")

    # The public demo is meant to answer without a token; it is pinned to a
    # synthetic tenant server-side. Alive is all we assert here.
    r = get(f"{MCP_DEMO}/health", timeout)
    check("mcp (public demo): alive", getattr(r, "status_code", None) == 200,
          str(getattr(r, "status_code", r)))

    failed = [n for n, ok, _ in results if not ok]
    print()
    if failed:
        print(f"{R}{len(failed)} check(s) failing:{X} " + ", ".join(failed))
        return 1
    print(f"{G}all {len(results)} checks passing{X}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable results")
    args = ap.parse_args()

    code = run(args.timeout)
    if args.json:
        print(json.dumps({"ok": code == 0,
                          "checks": [{"name": n, "ok": o, "detail": d}
                                     for n, o, d in results]}, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
