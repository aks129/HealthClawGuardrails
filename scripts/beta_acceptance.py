#!/usr/bin/env python3
"""Synthetic-beta acceptance — the supported sample-record journey, measured.

    python scripts/beta_acceptance.py --base http://127.0.0.1:8600 \
        --code-log /tmp/careagents.log            # local stack
    python scripts/beta_acceptance.py --base https://careagents.cloud \
        --email you@example.com                   # the code is typed in

The journey #677 and #703 call "synthetic beta success": a person signs in,
selects the sample source, gets a sourced answer with honest missing-data
handling, and can reopen it. Each step is timed and recorded; each check
either passes with the evidence it saw or fails with what it saw instead. A
step that cannot run here (no run worker, no model key) is recorded as
UNAVAILABLE — never as a pass and never as a failure of something else.

What it needs from the deployment: a signed-in account. The one-time code
is read from a local CareAgents log (`mail.send_code` logs it when
RESEND_API_KEY is unset) or typed by the runner from a real inbox. The
script never learns a Resend key or reads anyone else's mail.

Output: a run log (JSON) to --out, plus a human summary. Synthetic records
only: the sample source is the demo bundle, and nothing here touches a real
connection.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

import requests

QUESTION = ("Summarize the conditions, medications, and allergies in these "
            "records. Say what you could not establish.")
#: Phrases a sourced answer with honest gaps is expected to carry at least
#: one of. The set is loose on purpose: the check is "the answer names its
#: limits", not "the answer uses our words".
MISSING_DATA_WORDS = ("could not", "couldn't", "not establish", "no record",
                      "not in your records", "not found", "unknown", "missing",
                      "not available", "no information", "isn't recorded",
                      "not recorded", "don't have")
CODE_RE = re.compile(r"DEV email — .*? for (?P<email>\S+): (?P<code>\d{4,8})")


class Run:
    def __init__(self, out):
        self.out = out
        self.steps = []
        self.t0 = time.monotonic()

    def step(self, name, status, detail="", **evidence):
        row = {"step": name, "status": status, "detail": detail,
               "at_s": round(time.monotonic() - self.t0, 2), **evidence}
        self.steps.append(row)
        mark = {"PASS": "✓", "FAIL": "✗", "UNAVAILABLE": "–"}[status]
        print(f"{mark} {name}: {status} {detail}".rstrip())
        return status == "PASS"

    def finish(self):
        summary = {"passed": sum(s["status"] == "PASS" for s in self.steps),
                   "failed": sum(s["status"] == "FAIL" for s in self.steps),
                   "unavailable": sum(s["status"] == "UNAVAILABLE"
                                      for s in self.steps),
                   "elapsed_s": round(time.monotonic() - self.t0, 2),
                   "steps": self.steps}
        if self.out:
            with open(self.out, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2)
        print(f"\n{summary['passed']} passed, {summary['failed']} failed, "
              f"{summary['unavailable']} unavailable in {summary['elapsed_s']}s")
        return 0 if summary["failed"] == 0 else 1


def _code_from_log(path, email, since_pos):
    with open(path, encoding="utf-8", errors="replace") as fh:
        fh.seek(since_pos)
        text = fh.read()
    codes = [m.group("code") for m in CODE_RE.finditer(text)
             if m.group("email") == email]
    return codes[-1] if codes else None


def _sse(resp):
    for line in resp.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            try:
                yield json.loads(line[6:])
            except ValueError:
                yield {"type": "unreadable", "raw": line[6:200]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="CareAgents origin")
    ap.add_argument("--email", default="beta-acceptance@example.com")
    ap.add_argument("--code-log", help="local CareAgents log to read the "
                                       "sign-in code from")
    ap.add_argument("--out", default="beta_acceptance_run.json")
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="seconds to wait for the chat turn")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    s = requests.Session()
    run = Run(args.out)

    # 1. Landing and sign-in page reachable.
    try:
        r = s.get(f"{base}/", timeout=15)
        run.step("landing reachable", "PASS" if r.status_code == 200 else "FAIL",
                 f"HTTP {r.status_code}")
    except requests.RequestException as exc:
        run.step("landing reachable", "FAIL", type(exc).__name__)
        return run.finish()

    # 2. Sign in with a one-time code.
    log_pos = 0
    if args.code_log:
        try:
            with open(args.code_log, encoding="utf-8", errors="replace") as fh:
                fh.seek(0, 2)
                log_pos = fh.tell()
        except OSError:
            log_pos = 0
    t = time.monotonic()
    r = s.post(f"{base}/api/auth/email", json={"email": args.email}, timeout=20)
    if r.status_code != 200:
        run.step("sign-in code requested", "FAIL", f"HTTP {r.status_code}")
        return run.finish()
    code = None
    if args.code_log:
        for _ in range(20):
            code = _code_from_log(args.code_log, args.email, log_pos)
            if code:
                break
            time.sleep(0.5)
    if not code:
        code = input(f"One-time code sent to {args.email}: ").strip()
    r = s.post(f"{base}/api/auth/verify", json={"email": args.email, "code": code},
               timeout=20)
    if not run.step("signed in", "PASS" if r.status_code == 200 else "FAIL",
                    f"HTTP {r.status_code}", elapsed_s=round(time.monotonic() - t, 2)):
        return run.finish()

    # 3. The sample source is chosen and visibly synthetic.
    t = time.monotonic()
    r = s.post(f"{base}/api/connections/sample", timeout=60)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    conn = body.get("id")
    ok = r.status_code == 200 and conn and body.get("status") == "active"
    run.step("sample source connected", "PASS" if ok else "FAIL",
             f"HTTP {r.status_code} status={body.get('status')}",
             elapsed_s=round(time.monotonic() - t, 2))
    if not ok:
        return run.finish()
    home = s.get(f"{base}/home", timeout=20).text
    run.step("source shown as synthetic", "PASS" if re.search(r"sample|synthetic", home, re.I) else "FAIL",
             "hub names the sample source" if re.search(r"sample|synthetic", home, re.I)
             else "hub does not say the source is sample data")

    # 4. An agent on that source.
    r = s.post(f"{base}/api/agents", json={"name": "Acceptance", "persona": "calm",
                                           "connection_id": conn}, timeout=20)
    agent = r.json().get("id") if r.status_code == 200 else None
    if not run.step("agent created", "PASS" if agent else "FAIL", f"HTTP {r.status_code}"):
        return run.finish()

    # 5. One real chat turn: sourced answer, honest about gaps.
    t = time.monotonic()
    try:
        r = s.post(f"{base}/api/chat", json={"agent_id": agent, "message": QUESTION},
                   stream=True, timeout=args.timeout)
    except requests.RequestException as exc:
        run.step("chat turn", "FAIL", type(exc).__name__)
        return run.finish()
    if r.status_code == 503:
        run.step("chat turn", "UNAVAILABLE", f"HTTP 503: {r.text[:120]}")
        conversation = None
    elif r.status_code != 200:
        run.step("chat turn", "FAIL", f"HTTP {r.status_code}: {r.text[:120]}")
        conversation = None
    else:
        tools, texts, errors, conversation = [], [], [], None
        for ev in _sse(r):
            kind = ev.get("type")
            if kind == "tool":
                tools.append(ev.get("name"))
            elif kind == "text":
                texts.append(ev.get("text") or "")
            elif kind == "error":
                errors.append(ev.get("text") or "")
            conversation = ev.get("conversation_id") or conversation
        answer = "\n".join(texts)
        elapsed = round(time.monotonic() - t, 2)
        if errors and "more requests than I can answer" in errors[0]:
            # The model provider refused (rate limit / quota). That is the
            # environment, not the product; the sourced-answer checks below
            # are then unavailable, not failed and not passed.
            run.step("chat turn", "UNAVAILABLE",
                     "model provider rate-limited this run", elapsed_s=elapsed)
        elif errors:
            run.step("chat turn", "FAIL", f"agent error: {errors[0][:120]}", elapsed_s=elapsed)
        elif not answer.strip():
            run.step("chat turn", "FAIL", "no text came back", elapsed_s=elapsed, tools=tools)
        else:
            run.step("chat turn", "PASS", f"{len(answer)} chars, tools={tools}",
                     elapsed_s=elapsed, tools=tools)
            run.step("answer is grounded in a records tool",
                     "PASS" if tools else "FAIL",
                     "tool events seen" if tools else "no tool event — the answer was not read from records")
            hit = [w for w in MISSING_DATA_WORDS if w in answer.lower()]
            run.step("answer says what it could not establish",
                     "PASS" if hit else "FAIL",
                     f"phrases: {hit[:3]}" if hit else "no missing-data language in the answer")

    # 6. Reopen: the conversation is still there on a fresh page load.
    page = s.get(f"{base}/chat", params={"agent": agent}, timeout=20)
    reopened = page.status_code == 200 and (QUESTION[:30] in page.text)
    run.step("reopen shows the earlier turn", "PASS" if reopened else
             ("UNAVAILABLE" if conversation is None else "FAIL"),
             "the question is on the page" if reopened else
             ("no turn to reopen" if conversation is None else "the earlier turn is not on the page"))

    # 7. The appointment brief: every field it shows names its source record,
    #    and a missing section says so — never a fabricated negative.
    r = s.get(f"{base}/brief", params={"agent": agent}, timeout=30)
    html = r.text if r.status_code == 200 else ""
    sourced = html.count("brief-source-id")
    missing = html.count("Not available from your connected records")
    unreachable = "could not reach your records" in html
    if r.status_code != 200:
        run.step("appointment brief", "FAIL", f"HTTP {r.status_code}")
    elif unreachable:
        run.step("appointment brief", "UNAVAILABLE", "the brief could not read the records")
    else:
        run.step("appointment brief", "PASS" if sourced else "FAIL",
                 f"{sourced} sourced fields, {missing} sections honestly missing"
                 if sourced else "no field on the brief names a source record",
                 sourced_fields=sourced, missing_sections=missing)

    # 8. Labs: the timeline is read from records with its disclaimer attached.
    r = s.get(f"{base}/api/labs/timeline", params={"agent": agent}, timeout=30)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 502:
        run.step("labs timeline", "UNAVAILABLE", "labs unavailable from the engine")
    elif r.status_code != 200:
        run.step("labs timeline", "FAIL", f"HTTP {r.status_code}")
    else:
        series = body.get("series") or []
        run.step("labs timeline", "PASS" if isinstance(series, list) and body.get("disclaimer") else "FAIL",
                 f"{len(series)} series, disclaimer {'present' if body.get('disclaimer') else 'MISSING'}",
                 series=len(series))

    # 9. Pending approvals: reachable and honest (empty is fine; an outage is not).
    r = s.get(f"{base}/agents/{agent}/approvals", timeout=20)
    run.step("pending approvals page", "PASS" if r.status_code == 200 else "FAIL",
             f"HTTP {r.status_code}" + (" (says nothing is waiting)"
                                         if "Nothing is waiting" in r.text else ""))

    return run.finish()


if __name__ == "__main__":
    sys.exit(main())
