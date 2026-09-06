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
    3  the run did not decide every check this script declares — a defect in
       this script, not a verdict on production

Those are different alarms with different remedies, and a stale build holding
the outage issue open would destroy the meaning of the outage issue. 3 is the
same argument once more: "the harness is broken" and "production is broken"
send different people to different places, and the first used to be reported
as the second's absence — `all N checks passing`, with N derived from whatever
happened to run.

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

The Telegram check (#537) has the same limit one layer down. It reads what an
unauthenticated visitor reads — the landing page, and the home page only if
that ever answers without a session — and fails on any live Telegram link or
call to action there. The "connect →" tile #536 describes lives on /home
behind sign-in, where this script cannot see it; what this guards is the
public promise, and its detail says which pages it actually read. It reads
`/` and `/home` only — a Telegram invitation added to `/auth` would pass —
and `_telegram_advertised` documents the wordings it cannot see.

Which HOST answered is part of every CareAgents reading (#289). `requests`
follows redirects, so a check named for one origin will happily measure
another and report it under the first one's name; the origin's /healthz and
landing therefore fail if the response came from anywhere else, and the
Railway host's /healthz is fetched without following redirects at all. Two
hosts that agree because one is the other are not two readings.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import requests

G, R, Y, D, X = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"

HEALTHCLAW = "https://app.healthclaw.io"
# The origin people actually visit. Until #537 this was the Railway hostname,
# which nobody visits, so a divergence between the two — DNS, routing, a
# custom domain pinned to the wrong target — was invisible by construction
# (#289): a green board said nothing about what a patient saw.
CAREAGENTS = "https://careagents.cloud"
# The Railway-issued hostname behind it. About to 308 everything except
# /healthz to the origin, but /healthz there is what Railway's own health
# check hits, so that one path stays watched under its own name.
CAREAGENTS_RAILWAY = "https://careagents-production.up.railway.app"
# Two MCP deployments by design: the real one is token-locked, the demo one is
# unauthenticated but hard-pinned to a synthetic tenant.
MCP_LOCKED = "https://mcp-server-production-5112.up.railway.app"
MCP_DEMO = "https://mcp-demo-production-ee2c.up.railway.app"
DEMO_TENANT = "desktop-demo"

#: Every Patient the demo tenant is supposed to hold, and no others.
#:
#: One original record plus the three blood-pressure personas seeded for the
#: SMBP demo (a treated case, a white-coat case, and a phoned-in reading with
#: no home series). Adding a persona means adding it here in the same change:
#: the check below reads this as the whole truth about the tenant, so an
#: omission reports real data as an intruder, and an addition nobody made is
#: how the tenant duplicated itself into nineteen patients (#457).
DEMO_PATIENTS = (
    "demo-patient-rivera",
    "demo-marisol",
    "demo-elena",
    "demo-ray",
)
BUILD_CHECK = "careagents: running the current build"
TELEGRAM_CHECK = "careagents: telegram not advertised as live"
# Same shape careagents/_build.py enforces on the way out. A "-dirty" marker
# deliberately fails it: a build stamped from an uncommitted tree has no
# provenance to assert.
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")

# How a page advertises Telegram. A t.me (or tg://) link anywhere is a link,
# full stop. Short of one, a call to action is the surface's name and an
# inviting verb in the SAME piece of copy: "Telegram — connect →" on a tile,
# "Open in Telegram" on a button. The piece of copy is what sits between two
# block-closing tags, so a neighbouring tile's "connect →" cannot vouch for
# or against Telegram, and "coming soon" on the Telegram tile means exactly
# that. Script and style bodies are not copy anyone reads.
#
# A CTA is also SHORT: a tile label or a button, not a sentence. Without that
# bound the verb list matches ordinary prose that merely names the surface —
# "Chat with your agent on the web today. Telegram and iMessage are not
# available in the beta" contains `chat` and `Telegram` and is the opposite of
# a promise. A monitor that reddens on the honest sentence trains its reader
# to ignore it, which is the failure this file warns about two checks down.
# Long copy carrying a real invitation almost always carries the link too, and
# the link rule above has no length bound.
_TG_COPY_MAX = 48
_TG_LINK_RE = re.compile(r"(?i)\bt\.me/|\btg://")
_TG_NAME_RE = re.compile(r"(?i)\btelegram\b")
_TG_CTA_RE = re.compile(r"(?i)\b(?:open|connect|start|chat|join|pair|launch)\b")
_BLOCK_END_RE = re.compile(
    r"(?i)</(?:div|li|p|td|th|tr|a|button|h[1-6]|section|article|label|dt|dd)\s*>")
_TAG_RE = re.compile(r"<[^>]*>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")


def _telegram_advertised(html: str) -> str:
    """How `html` presents Telegram as a live surface, or "" if it does not.

    What it does NOT see, so the check's name is not read as more than it is:
    text a script writes after load (the tile's own state is server-rendered,
    so the one that matters is visible), an invitation worded outside the verb
    list above, and a live call to action sharing one block with the words
    "coming soon". Each of those is a green this returns without proof.
    """
    if _TG_LINK_RE.search(html):
        return "a t.me link"
    # Source newlines are formatting, not boundaries: the real tile puts
    # "Telegram" and "connect →" on different lines of the template. Collapse
    # them first, then let only a block-closing tag end a piece of copy.
    text = " ".join(_SCRIPT_RE.sub(" ", html).split())
    text = _TAG_RE.sub(" ", _BLOCK_END_RE.sub("\n", text))
    for line in unescape(text).splitlines():
        line = " ".join(line.split())
        if not _TG_NAME_RE.search(line) or "coming soon" in line.lower():
            continue
        if len(line) <= _TG_COPY_MAX and _TG_CTA_RE.search(line):
            return f"a live call to action ({line[:60]!r})"
    return ""


def _answered_elsewhere(r, url: str) -> str:
    """Where `r` actually came from, if that is not the origin of `url`.

    `requests` follows redirects, so a check NAMED for one host measures
    whichever host the chain ends at. That is #289's blindness restated one
    layer down: careagents.cloud 308ing to the Railway hostname would keep
    every check here green while nothing verified what a user is served —
    and the build marker, read from this origin's /healthz, would assert the
    other host's build under this host's name. A response from somewhere else
    is not a worse reading of the origin; it is no reading of it.
    """
    landed = str(getattr(r, "url", "") or url)
    return "" if landed.startswith("/".join(url.split("/", 3)[:3])) else landed


results: list[tuple[str, bool, str]] = []
#: Names decided WITHOUT an assertion — see `report`. Kept because a check that
#: said "not asserted, and here is why" ran, and one that said nothing did not.
#: The completeness guard in `run` has to be able to tell those apart.
reported: list[str] = []

# Where the human report goes. `--json` moves it to stderr so stdout carries
# the payload and nothing else — the flag was documented as machine-readable
# while printing JSON after the ANSI-coloured lines on the same stream, so
# piping it into a parser failed (#270). Resolved at print time rather than
# captured at import, so redirecting stdout around a call still works.
_human_to_stderr = False


def _human():
    return sys.stderr if _human_to_stderr else sys.stdout

# The build check's verdict, kept separately from `results`, because an exit
# code is one scalar and the alarms it drives are not. Without this the
# workflow had to infer "is the build stale?" from a code an unrelated outage
# also sets, and closed a live stale-build alarm with "passing again" during an
# outage — the same unverified green #258 exists to eliminate.
# `asserted` is False in informational mode: not-asserted is not passing, and
# only an observed pass may close an alarm.
build_info: dict = {"deployed": None, "built_at": None, "built": None,
                    "asserted": False, "ok": None}

# Whether the run decided everything it declares, kept apart from `results` for
# the same reason build_info is: this is a claim about the RUN, not about
# production, and it drives a third alarm whose remedy is "fix this script".
# False until a run proves otherwise — not-yet-known is not complete.
completeness: dict = {"complete": False, "missing": [], "unreadable": []}


def _declared_checks(source: str | None = None):
    """The check names this file declares, and the call sites it cannot read.

    Every `check(NAME, ...)` here is a declaration that the run will decide
    NAME. Reading them back out of the source is what keeps the expectation in
    the same place as the checks: there is no list beside them to update, so
    there is none to forget, and two branches that each add a check cannot
    disagree about a total. Adding a check adds its expectation.

    A name is readable when it is a string literal or a module-level string
    constant (BUILD_CHECK is one). Anything else — an f-string, a name built at
    run time — comes back as unreadable rather than being skipped: a call site
    this cannot see is a check the guard cannot miss, which is the whole hole.

    `report` sites are NOT declarations. report is how a declared name says
    "not asserted this run", so a name that only ever reports asserts nothing
    and must not be demanded of a run that had no reason to say it.
    """
    if source is None:
        source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    consts: dict[str, str] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value

    names: set[str] = set()
    unreadable: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "check"):
            continue
        arg = node.args[0] if node.args else None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.add(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in consts:
            names.add(consts[arg.id])
        else:
            unreadable.append(
                f"the check on line {node.lineno} does not name itself with a "
                "literal or a module constant, so a run cannot tell whether "
                "it ran")
    return frozenset(names), unreadable


def check(name: str, ok: bool, detail: str = "") -> bool:
    """Assert something, and record the verdict under `name`.

    The name is a promise. Every `check(...)` call site in this file is read
    back out of the source as a declaration that the run will DECIDE that name
    — asserted here, or reported under the same name when there is honestly
    nothing to assert (#272). A run that does neither has quietly checked less
    than this script says it checks, and `run` fails it rather than counting
    what happened to run.

    So a name has to stay statically readable: a literal, or a module constant
    like BUILD_CHECK. `_declared_checks` refuses anything it cannot read.
    """
    results.append((name, ok, detail))
    mark = f"{G} OK {X}" if ok else f"{R}FAIL{X}"
    print(f"{mark} {name}" + (f" {D}— {detail}{X}" if detail else ""),
          file=_human())
    return ok


def report(name: str, detail: str) -> None:
    """Print a fact without asserting anything about it.

    Deliberately NOT recorded as a passing check: this script's whole claim is
    that every line it prints was actually verified. It IS recorded as decided,
    because a check that says "not asserted, and here is why" has run — the
    completeness guard must not report it as vanished, and a silence must not
    borrow its cover.
    """
    reported.append(name)
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


def _run_checks(timeout: float, expect_sha: list[str]) -> bool:
    """Run every check; return whether the build check found a stale build.

    Split out of `run` so the summary and the completeness guard cannot be
    skipped by anything added in here — including an early `return`, which is
    one of the ways a check stops running in the first place.
    """
    # Reset, because this is module state and a stale verdict is worse than no
    # verdict: a second run in informational mode would otherwise inherit the
    # first run's `asserted=True, ok=True` and let the workflow close a live
    # stale-build alarm on an assertion this run never made. Only one run per
    # process happens today; that is a property of main(), not of run().
    build_info.update(deployed=None, built_at=None, built=None,
                      asserted=False, ok=None)
    # Both registers, together. The completeness guard reads them as "what
    # THIS run decided"; a name left behind by an earlier run would answer for
    # a check this one skipped, which is the guard being fooled by exactly the
    # stale module state the reset above exists for.
    results.clear()
    reported.clear()

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

    # --- the demo tenant's shape (#457, docs/defect-catalogue.md §10) --------
    # Deployment truth is not code truth, and nothing was reconciling them.
    # railway.toml runs a pre-deploy seed and its own comment called that seed
    # idempotent; it was not, and the demo tenant reached 19 Patients against
    # a seed set of one. Separately, running the conformance harness against
    # this tenant leaves a probe Patient behind each time. Neither source
    # showed up in any check, because every other check here is satisfied
    # just as well by a tenant with twenty patients in it.
    #
    # A physician advisor found it, on camera, the day before a launch
    # recording: "/conditions shows about a dozen duplicate Type 2 diabetes
    # mellitus entries".
    #
    # This watches the CONSEQUENCE rather than the mechanism. A count is
    # observable from outside; "did the pre-deploy step run" is not, and the
    # count is what a viewer of the demo actually sees.
    # The SET, not the count. A count answers "has it duplicated" and nothing
    # else; it cannot tell a duplicated tenant from one that lost a persona,
    # and losing one is the failure that costs a recording. This check went
    # red for four days after the SMBP demo patients were seeded — correct
    # data, stale expectation — which is its own lesson: a guard that fires on
    # intended work trains everyone to ignore it, and then it is not a guard.
    r = get(f"{HEALTHCLAW}/r6/fhir/Patient?_count=200", timeout,
            headers={"X-Tenant-Id": DEMO_TENANT})
    found = None
    if getattr(r, "status_code", None) == 200:
        try:
            found = {(e.get("resource") or {}).get("id")
                     for e in (r.json().get("entry") or [])}
        except ValueError:
            found = None
    # `is not None` rather than a truthiness test: an EMPTY set is a real
    # failure (an empty demo), and `if found:` would report it as unreadable
    # instead. The read failing and the tenant being empty are different
    # alarms.
    if found is None:
        detail = f"could not read the tenant ({getattr(r, 'status_code', r)})"
    else:
        missing = sorted(set(DEMO_PATIENTS) - found)
        extra = sorted(str(x) for x in found - set(DEMO_PATIENTS))
        parts = [f"{len(found)} Patient(s) in {DEMO_TENANT}"]
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if extra:
            parts.append(f"unexpected {', '.join(extra)}")
        detail = "; ".join(parts)
    check("healthclaw: the demo tenant holds exactly its demo patients",
          found is not None and found == set(DEMO_PATIENTS),
          detail)

    # --- the consumer app ----------------------------------------------------
    r = get(f"{CAREAGENTS}/healthz", timeout)
    # Off-origin is not a reading of this origin, so nothing downstream may
    # speak for it: `healthz_read` stays False and the build check reports
    # that it asserted nothing, exactly as it does for an unreadable /healthz.
    elsewhere = _answered_elsewhere(r, f"{CAREAGENTS}/healthz")
    body = {}
    # Whether the deployment actually told us anything, as opposed to us
    # defaulting on its behalf. The build check below reads its one field from
    # this body and may only speak when this is True — see #272.
    healthz_read = False
    if not elsewhere and getattr(r, "status_code", None) in (200, 503):
        try:
            body = r.json() or {}
            healthz_read = True
        except ValueError:
            pass
    # /healthz round-trips the database, so this catches an app that booted but
    # cannot reach its store — the state a load balancer must not route into.
    check("careagents: ready (db reachable)",
          not elsewhere and getattr(r, "status_code", None) == 200
          and body.get("accounts") is True,
          f"status={getattr(r, 'status_code', r)} accounts={body.get('accounts')}"
          + (f" — answered by {elsewhere}, not the origin" if elsewhere else ""))

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
               "not asserted — /healthz was "
               + (f"answered by {elsewhere}, not the origin" if elsewhere else
                  f"not readable ({getattr(r, 'status_code', r)})")
               + ", so no build marker was read")
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

    # The Railway hostname, readiness only: everything user-facing above and
    # below is asked of the origin, because the origin is what a user gets.
    # Its build marker rides along in the detail — shown, not asserted — so a
    # deployment split between the two hosts is visible in the report rather
    # than assumed away.
    #
    # Not following the redirect is the whole point here. This host is about
    # to 308 every other path to the origin; if /healthz joins them, following
    # it would read the ORIGIN's health and build and print them beside the
    # Railway host's name — the two hosts would agree by construction, which
    # is the one thing this check exists to disprove. A 3xx is therefore a
    # failure: it means this host no longer answers for itself, which is also
    # what Railway's own health check would find.
    r = get(f"{CAREAGENTS_RAILWAY}/healthz", timeout, allow_redirects=False)
    rcode = getattr(r, "status_code", None)
    rbody = {}
    if rcode in (200, 503):
        try:
            rbody = r.json() or {}
        except ValueError:
            pass
    moved = ""
    if isinstance(rcode, int) and 300 <= rcode < 400:
        moved = str((getattr(r, "headers", None) or {}).get("Location") or
                    "an undisclosed target")
    check("careagents (railway host): ready (db reachable)",
          rcode == 200 and rbody.get("accounts") is True,
          f"status={getattr(r, 'status_code', r)} accounts={rbody.get('accounts')}"
          f" build={str(rbody.get('build') or 'unknown').lower()}"
          + (f" — redirects to {moved}, so this host was not read; Railway's "
             "own health check hits this path" if moved else ""))

    r = get(f"{CAREAGENTS}/", timeout)
    html = getattr(r, "text", "") or ""
    landing_status = getattr(r, "status_code", r)
    landing_elsewhere = _answered_elsewhere(r, f"{CAREAGENTS}/")
    landing_read = landing_status == 200 and not landing_elsewhere
    check("careagents: landing renders", landing_read and "/auth" in html,
          str(landing_status)
          + (f" — served by {landing_elsewhere}, not the origin"
             if landing_elsewhere else ""))

    # --- Telegram (#537) ------------------------------------------------------
    # Twelve checks were green while the Telegram surface had been dead since
    # June (#536): every check asked whether a process answers, and none asked
    # whether a promise made to a user could be kept. This one cannot see the
    # bot either — there is no token here, and a getMe would only prove a bot
    # exists, not that anything services it — so it watches the promise
    # instead: no live Telegram link or call to action on any page an
    # unauthenticated visitor can read. A tile marked "coming soon" passes.
    #
    # The landing body is the one already fetched; a landing that was not
    # read is not scanned, and not scanned is a failure, not a pass. /home is
    # fetched without following its redirect: without a session it answers
    # 302 to /auth, and following that would scan the sign-in page under
    # /home's name.
    pages = {"/": html} if landing_read else {}
    r = get(f"{CAREAGENTS}/home", timeout, allow_redirects=False)
    home_code = getattr(r, "status_code", None)
    home_status = getattr(r, "status_code", r)
    if home_code == 200:
        pages["/home"] = getattr(r, "text", "") or ""
    live = ""
    for path, page in pages.items():
        how = _telegram_advertised(page)
        if how:
            live = f"{path} shows {how}"
            break
    # A 200 from the wrong host is not a readable landing, and saying only
    # "(200)" would read as a contradiction on the line under a failing
    # landing check. Name the host that answered instead.
    landing_why = (f"{landing_status} from {landing_elsewhere}"
                   if landing_elsewhere else str(landing_status))
    if not pages:
        detail = f"landing not readable ({landing_why}), nothing was scanned"
    elif live:
        detail = (f"{live} — Telegram pairing is a dead end (#536) until its "
                  "fix deploys; a tile marked coming soon passes")
    else:
        # Both gaps, not just the expected one. A run that read /home but not
        # the landing would otherwise report the pages it managed to read and
        # say nothing about the one a stranger actually opens.
        gaps = ([] if "/" in pages else [f"/ not scanned ({landing_why})"])
        if "/home" not in pages:
            gaps.append(f"/home not scanned ({home_status} without a session)")
        detail = ("no live Telegram link or call to action on "
                  + ", ".join(pages)
                  + ("; " + "; ".join(gaps) if gaps else ""))
    check(TELEGRAM_CHECK, bool(pages) and not live, detail)

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

    return stale


def run(timeout: float, expect_sha: list[str]) -> int:
    stale = _run_checks(timeout, expect_sha)

    # Did this run actually run? `all N checks passing` counted what happened
    # to execute, so a check that stopped running — moved inside a condition
    # that no longer fires, lost in a merge, skipped by an early return — shrank
    # N and the line still read as complete. That is the demo-tenant lesson one
    # level up: a count answers "has something broken" and cannot answer "is
    # this all of it". So compare the SET of names decided against the set this
    # file declares, and name what is absent.
    #
    # The distinction this guard draws, because the script makes both kinds of
    # claim: an assertion about a RESPONSE may be computed from the response —
    # that is what checking is. A claim about how much of the HARNESS ran may
    # not be computed from the harness.
    try:
        declared, unreadable = _declared_checks()
    except (OSError, SyntaxError) as exc:
        # Not knowing what we should have run IS the failure this guard is
        # about, so it takes this guard's exit code. Raising here would exit 1
        # and file the outage issue about a script that never reached
        # production.
        declared, unreadable = frozenset(), [
            f"this script's own source could not be read ({type(exc).__name__}"
            f": {exc}), so nothing knows what this run should have decided"]
    missing = sorted(declared - ({n for n, _, _ in results} | set(reported)))
    completeness.update(complete=not missing and not unreadable,
                        missing=missing, unreadable=unreadable)

    failed = [n for n, ok, _ in results if not ok]
    hard = [n for n in failed if n != BUILD_CHECK]
    print(file=_human())
    if failed:
        print(f"{R}{len(failed)} check(s) failing:{X} " + ", ".join(failed),
              file=_human())
    if missing:
        print(f"{R}{len(missing)} declared check(s) never ran:{X} "
              + ", ".join(missing), file=_human())
    for why in unreadable:
        print(f"{R}this run cannot say what it should have decided:{X} {why}",
              file=_human())
    if missing or unreadable:
        print(f"{D}Each line above is still true; this run as a whole is not a "
              f"verdict on production. The defect is in scripts/prod_watch.py, "
              f"not in the deployment.{X}", file=_human())

    # A stale build is not an outage. Reporting it as one would train the
    # reader to ignore the outage alarm. An incomplete run is neither of those:
    # it says nothing about production at all, so it must not be able to close
    # either alarm, and it outranks the stale one — a build verdict from a run
    # that skipped checks is not worth acting on. An outage still outranks it,
    # because a real outage is the more urgent of the two things to be told.
    if hard:
        return 1
    if missing or unreadable:
        return 3
    if stale:
        return 2
    print(f"{G}all {len(results)} checks passing{X} "
          f"{D}— {len(declared)} declared, all accounted for"
          + (f" ({len(reported)} reported without assertion)"
             if reported else "")
          + f"{X}", file=_human())
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
                         "given. The scheduled run uses this to drive its "
                         "three alarms from the checks themselves rather than "
                         "from one exit code.")
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
               # `and complete`: a run that skipped a check has not observed
               # production, so it must not be able to close the outage issue.
               # `!hardFailing` was already rejected in the workflow for
               # exactly this reason — the absence of a failure is not an
               # observation — and a shrunken denominator is that same absence
               # arriving from inside the script instead of from the runner.
               "hard_ok": code != 1 and completeness["complete"],
               "checks": [{"name": n, "ok": o, "detail": d}
                          for n, o, d in results],
               # What this run DECLARES it decides, against what it decided.
               # The scheduled job raises its third alarm from these and can
               # name the check that vanished; `reported` is what accounts for
               # the gap between `checks` and the declaration on an honest run.
               "complete": completeness["complete"],
               "missing": list(completeness["missing"]),
               "unreadable": list(completeness["unreadable"]),
               "reported": list(reported),
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
