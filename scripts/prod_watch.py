#!/usr/bin/env python3
"""Production watch — is the live product actually working right now?

    python scripts/prod_watch.py                  # all deployments
    python scripts/prod_watch.py --json           # machine-readable on stdout
    python scripts/prod_watch.py --expect-sha a1b2c3d,4f5e6d7   # pin the build

Under `--json` the human report moves to stderr and stdout carries the payload
alone, so `--json | jq` works (#270). Without it, the human report keeps stdout
to itself.

Exit codes, so a scheduled job can open the right issue:

    0  everything passing
    1  hard failure — a deployment is down, degraded, or a guardrail regressed
    2  the deployment is healthy but is running a build we did not expect

Those are different alarms with different remedies, and a stale build holding
the outage issue open would destroy the meaning of the outage issue.

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

The build check (#258) closes a different blind spot with the same honesty
limit: every other check here is equally satisfied by a months-old build, and
this one proves WHICH ARTIFACT is deployed. It does not prove the code in that
artifact works — a broken build carrying the right sha still passes it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

import requests

G, R, Y, D, X = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"

HEALTHCLAW = "https://app.healthclaw.io"
CAREAGENTS = "https://careagents-production.up.railway.app"
# Two MCP deployments by design: the real one is token-locked, the demo one is
# unauthenticated but hard-pinned to a synthetic tenant.
MCP_LOCKED = "https://mcp-server-production-5112.up.railway.app"
MCP_DEMO = "https://mcp-demo-production-ee2c.up.railway.app"
DEMO_TENANT = "desktop-demo"
BUILD_CHECK = "careagents: running the current build"
# Same shape careagents/_build.py enforces on the way out. A "-dirty" marker
# deliberately fails it: a build stamped from an uncommitted tree has no
# provenance to assert.
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")

results: list[tuple[str, bool, str]] = []

# Where the human report goes. `--json` moves it to stderr so stdout carries
# the payload and nothing else — the flag was documented as machine-readable
# while printing JSON after the ANSI-coloured lines on the same stream, so
# piping it into a parser failed (#270). Resolved at print time rather than
# captured at import, so redirecting stdout around a call still works.
_human_to_stderr = False


def _human():
    return sys.stderr if _human_to_stderr else sys.stdout

# The build check's verdict, kept separately from `results`, because an exit
# code is one scalar and the two alarms it drives are not. Without this the
# workflow had to infer "is the build stale?" from a code an unrelated outage
# also sets, and closed a live stale-build alarm with "passing again" during an
# outage — the same unverified green #258 exists to eliminate.
# `asserted` is False in informational mode: not-asserted is not passing, and
# only an observed pass may close an alarm.
build_info: dict = {"deployed": None, "built_at": None, "built": None,
                    "asserted": False, "ok": None}


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    mark = f"{G} OK {X}" if ok else f"{R}FAIL{X}"
    print(f"{mark} {name}" + (f" {D}— {detail}{X}" if detail else ""),
          file=_human())
    return ok


def report(name: str, detail: str) -> None:
    """Print a fact without asserting anything about it.

    Deliberately NOT recorded as a passing check: this script's whole claim is
    that every line it prints was actually verified.
    """
    print(f"{Y}INFO{X} {name} {D}— {detail}{X}", file=_human())


def _stamp(ts) -> str:
    # 0 is what an unstamped build reports, not a build time. Rendering it as
    # 1970-01-01 states a moment that never happened, on the very line a human
    # reads at 03:00 on the alarm this check exists to raise. Negatives are the
    # same defect one value over: _build passes int() through unbounded.
    # bool is an int, so True would otherwise survive as 1970-01-01. Nothing we
    # serve can produce it, which is exactly the argument a monitor is not
    # allowed to make about the field it is auditing.
    if isinstance(ts, bool):
        return ""
    try:
        if not ts or int(ts) <= 0:
            return ""
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime(
            "%Y-%m-%dT%H:%MZ")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def get(url: str, timeout: float, **kw):
    try:
        return requests.get(url, timeout=timeout, **kw)
    except requests.RequestException as exc:
        return type(exc).__name__


def post(url: str, timeout: float, **kw):
    try:
        return requests.post(url, timeout=timeout, **kw)
    except requests.RequestException as exc:
        return type(exc).__name__


def run(timeout: float, expect_sha: list[str]) -> int:
    # Reset, because this is module state and a stale verdict is worse than no
    # verdict: a second run in informational mode would otherwise inherit the
    # first run's `asserted=True, ok=True` and let the workflow close a live
    # stale-build alarm on an assertion this run never made. Only one run per
    # process happens today; that is a property of main(), not of run().
    build_info.update(deployed=None, built_at=None, built=None,
                      asserted=False, ok=None)

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
    # Whether the deployment actually told us anything, as opposed to us
    # defaulting on its behalf. The build check below reads its one field from
    # this body and may only speak when this is True — see #272.
    healthz_read = False
    if getattr(r, "status_code", None) in (200, 503):
        try:
            body = r.json() or {}
            healthz_read = True
        except ValueError:
            pass
    # /healthz round-trips the database, so this catches an app that booted but
    # cannot reach its store — the state a load balancer must not route into.
    check("careagents: ready (db reachable)",
          getattr(r, "status_code", None) == 200 and body.get("accounts") is True,
          f"status={getattr(r, 'status_code', r)} accounts={body.get('accounts')}")

    # Same body, no second request. Every other check on this deployment is
    # satisfied just as well by a months-old build — in #258 both CareAgents
    # deployments were running code older than PR #241 while this script
    # reported 9/9 green. This asks the one question the others cannot.
    stale = False
    if not healthz_read:
        # Nothing was read, so there is no marker to have a verdict about, and
        # `deployed` below would be this script's own "unknown" default —
        # indistinguishable from a genuinely unmarked build. Asserting on it
        # told whoever read the alarm at 03:00 to redeploy when the deployment
        # was simply DOWN (#272): a verdict about a field the run never read,
        # which is the exact thing #258 exists to prevent. The readiness check
        # above already reports the outage, and its remedy is the right one.
        report(BUILD_CHECK,
               f"not asserted — /healthz was not readable "
               f"({getattr(r, 'status_code', r)}), so no build marker was read")
    else:
        deployed = str(body.get("build") or "unknown").lower()
        built = _stamp(body.get("built_at"))
        marker = deployed + (f" built {built}" if built else "")
        build_info.update(deployed=deployed, built_at=body.get("built_at"),
                          built=built or None)
        if not expect_sha:
            # No expected set means no honest assertion to make, so make none.
            report(BUILD_CHECK,
                   f"{marker} (informational — no --expect-sha given)")
        else:
            # Deployed sha is short; the expected set is full sha. A build
            # still rolling out matches an hours-old commit and passes;
            # "unknown" and anything "-dirty" match nothing, which is the
            # intent.
            #
            # Shape-check first. `startswith` alone means a build reporting "4"
            # prefix-matches every expected sha and passes. _build.py gates the
            # marker on its way out; this gates it on the way in, because the
            # one thing a monitor must not do is accept the field it is
            # auditing.
            ok = (bool(_SHA_RE.fullmatch(deployed))
                  and any(full.startswith(deployed) for full in expect_sha))
            stale = not check(
                BUILD_CHECK, ok,
                marker if ok else
                f"deployed build {deployed}"
                + (f" (built {built})" if built else "")
                + f" is not one of the {len(expect_sha)} commit(s) this run "
                f"accepts (tip {expect_sha[0][:7]}). CareAgents does not "
                "auto-deploy — redeploy per RELEASING.md §4.")
            build_info.update(asserted=True, ok=ok)

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
    # synthetic tenant server-side.
    r = get(f"{MCP_DEMO}/health", timeout)
    check("mcp (public demo): alive", getattr(r, "status_code", None) == 200,
          str(getattr(r, "status_code", r)))

    # ...but "alive" was all this asserted for a while, and that is the gap a
    # design partner found for us: every quickstart, the Gemini extension, the
    # registry entry and the homepage now send strangers here, and what they
    # actually do is an unauthenticated MCP handshake — not a /health GET. A
    # token accidentally set on this deployment would flip it to 401 and leave
    # this script fully green, because /health stays public on both servers.
    # So assert the thing users depend on: a keyless `initialize` succeeds.
    r = post(
        f"{MCP_DEMO}/mcp", timeout,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "prod-watch",
                                        "version": "1"}}},
    )
    code = getattr(r, "status_code", None)
    served = False
    if code == 200:
        try:
            served = "result" in (r.json() or {})
        except ValueError:
            served = False
    check("mcp (public demo): serves an unauthenticated handshake", served,
          f"keyless initialize -> {code}"
          + ("" if served else " (no JSON-RPC result)"))

    failed = [n for n, ok, _ in results if not ok]
    hard = [n for n in failed if n != BUILD_CHECK]
    print(file=_human())
    if failed:
        print(f"{R}{len(failed)} check(s) failing:{X} " + ", ".join(failed),
              file=_human())
    # A stale build is not an outage. Reporting it as one would train the
    # reader to ignore the outage alarm.
    if hard:
        return 1
    if stale:
        return 2
    print(f"{G}all {len(results)} checks passing{X}", file=_human())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable results on stdout, and send "
                         "the human report to stderr so stdout can be piped "
                         "into a parser")
    ap.add_argument("--json-out", metavar="PATH",
                    help="write the same machine-readable results to PATH. "
                         "stdout keeps the human report unless --json is also "
                         "given. The scheduled run uses this to drive its two "
                         "alarms from the checks themselves rather than from "
                         "one exit code.")
    ap.add_argument("--expect-sha", action="append", default=[], metavar="SHA",
                    help="full commit sha the CareAgents build may have been "
                         "built from; repeatable or comma-separated. The "
                         "scheduled run passes every commit merged to main in "
                         "the last 24h plus the tip, so a deploy in flight is "
                         "still accepted. Omit it and the deployed build is "
                         "reported but not asserted.")
    args = ap.parse_args()

    # Assigned unconditionally, not only under --json: this is module state,
    # and a second main() in one process must not inherit the first one's
    # stream.
    global _human_to_stderr
    _human_to_stderr = args.json

    # Order matters: the first sha is reported as the tip. De-duplicated so a
    # tip that also appears in the last-24h list is not counted twice.
    expect = list(dict.fromkeys(s.strip().lower() for arg in args.expect_sha
                                for s in arg.split(",") if s.strip()))
    # `--expect-sha "$SHA"` with SHA unset parses to nothing. Falling back to
    # informational mode there would tell a caller who explicitly asked to pin
    # the build that everything passed, while silently dropping the assertion —
    # the unset-variable footgun, reported as green.
    if args.expect_sha and not expect:
        print(f"{R}--expect-sha was given but contains no sha{X}", file=sys.stderr)
        return 1
    code = run(args.timeout, expect)
    payload = {"ok": code == 0,
               # "nothing is wrong" and "nothing an outage alarm speaks for is
               # wrong" are different questions, and the outage alarm needs the
               # second. A healthy deployment running a stale build exits 2, so
               # `ok` is False with zero hard failures — closing the outage
               # alarm on `ok` would leave it open, saying production is
               # failing, for the entire window between any merge to main and a
               # manual CareAgents redeploy. That is the normal state of this
               # repo, not an edge case, and it is the exact meaning-destruction
               # the two-alarm split exists to prevent.
               "hard_ok": code != 1,
               "checks": [{"name": n, "ok": o, "detail": d}
                          for n, o, d in results],
               # Separate from `checks` on purpose: in informational mode the
               # build is reported without being counted, so folding it in
               # would inflate the count. Omitting it entirely left --json as
               # blind to provenance as it was before #258.
               "build": dict(build_info)}
    if args.json:
        print(json.dumps(payload, indent=2))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(payload, fh, indent=2)
    return code


if __name__ == "__main__":
    sys.exit(main())
