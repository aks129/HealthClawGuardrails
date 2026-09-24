#!/usr/bin/env python3
"""Synthetic-beta acceptance — the supported sample-record journey, measured.

    python scripts/beta_acceptance.py --code-log /tmp/careagents.log
                                                  # local stack (the default)
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

Rows 7–12 of the #677 checklist follow the first six, in its order:

  7  SMBP triage     — UNAVAILABLE by construction: the SMBP rail is served
                       by the engine behind a step-up credential, and the
                       consumer app has no surface for it. The issue says to
                       record that, not to substitute another interface.
  8  care gaps       — two surfaces. The brief's "Preventive care due"
                       section, read without the model: sourced items, or
                       its own "unavailable", never reassurance from a
                       review that did not run (today the brief resolves no
                       patient, so it says unavailable — r6/brief/routes.py).
                       Then a chat turn: the answer must come from the
                       care-gaps tool and name what is due.
  9  intake form     — asked for in chat; the review page lists each
                       medication and allergy for a decision, leaves "No
                       known allergies" unticked, and the server refuses a
                       submit that attests nothing about allergies.
  10 approve + PDF   — every item is kept as the record has it and the
                       review is submitted; the signed PDF is downloaded and
                       must carry the approved medications and the review
                       line. "No known allergies" is never ticked by this
                       script: with no allergy on file it asks the person at
                       the terminal, and records UNAVAILABLE without one.
  11 restart/retry   — a fresh session signs in again and finds the turn,
                       the form's outcome unchanged, and a second submit of
                       the same review refused. Reconnect is asked for and
                       recorded as what the sample source answers: nothing
                       to re-pull.
  12 delete          — the sample connection is deleted, the response states
                       its scope, and the hub, labs timeline and form status
                       then show it gone; a second delete finds nothing.

Output: a run log (JSON) to --out, plus a human summary. Synthetic records
only: the sample source is the demo bundle, and nothing here touches a real
connection. The run log carries counts and statuses, never record content.
"""
from __future__ import annotations

import argparse
import base64
import html as _html
import json
import re
import sys
import time
import zlib

import requests

QUESTION = ("Summarize the conditions, medications, and allergies in these "
            "records. Say what you could not establish.")
CARE_GAPS_QUESTION = ("Which preventive screenings or vaccines am I due for? "
                      "Say which ones you could not check.")
FORM_REQUEST = ("Please start my new-patient intake form from my records so "
                "I can review it.")
#: Phrases a sourced answer with honest gaps is expected to carry at least
#: one of. The set is loose on purpose: the check is "the answer names its
#: limits", not "the answer uses our words".
MISSING_DATA_WORDS = ("could not", "couldn't", "not establish", "no record",
                      "not in your records", "not found", "unknown", "missing",
                      "not available", "no information", "isn't recorded",
                      "not recorded", "don't have")
CODE_RE = re.compile(r"DEV email — .*? for (?P<email>\S+): (?P<code>\d{4,8})")
DEFAULT_BASE = "http://127.0.0.1:8600"

#: Row 7 has no consumer surface to drive. Said once, here, so the run log
#: and the tests agree on why.
SMBP_UNAVAILABLE = ("no CareAgents surface: the SMBP rail is served by the "
                    "engine at /r6/smbp behind a step-up credential this "
                    "script never holds")

_MED_NAME_RE = re.compile(
    r'class="med-row[^"]*"[^>]*>\s*<div class="me-3">\s*'
    r'<div class="fw-bold">([^<]*)</div>')
_MED_ROW_RE = re.compile(r'name="med-(\d+)"')
_ALLERGY_ROW_RE = re.compile(r'name="allergy-(\d+)"')
_NKA_INPUT_RE = re.compile(r'<input[^>]*name="nka"[^>]*>')
_BRIEF_GAPS_RE = re.compile(
    r"<h2>Preventive care due</h2>(?P<body>.*?)(?:<h2>|\Z)", re.S)


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


def _json(r):
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            return r.json()
        except ValueError:
            return {}
    return {}


def sign_in(s, base, email, code_log, run, name="signed in"):
    """Request a one-time code and verify it. True when signed in.

    Records `name` on `run`; with `run` None it only answers.
    """
    def record(status, detail, **evidence):
        if run is not None:
            run.step(name, status, detail, **evidence)
        return status == "PASS"

    log_pos = 0
    if code_log:
        try:
            with open(code_log, encoding="utf-8", errors="replace") as fh:
                fh.seek(0, 2)
                log_pos = fh.tell()
        except OSError:
            log_pos = 0
    t = time.monotonic()
    r = s.post(f"{base}/api/auth/email", json={"email": email}, timeout=20)
    if r.status_code != 200:
        return record("FAIL", f"code request HTTP {r.status_code}")
    code = None
    if code_log:
        for _ in range(20):
            code = _code_from_log(code_log, email, log_pos)
            if code:
                break
            time.sleep(0.5)
    if not code:
        code = input(f"One-time code sent to {email}: ").strip()
    r = s.post(f"{base}/api/auth/verify", json={"email": email, "code": code},
               timeout=20)
    return record("PASS" if r.status_code == 200 else "FAIL",
                  f"HTTP {r.status_code}",
                  elapsed_s=round(time.monotonic() - t, 2))


def chat_turn(s, base, agent, message, timeout):
    """One model turn, read to the end of its event stream.

    Returns a dict: `status` (PASS / FAIL / UNAVAILABLE) and `detail` for the
    turn itself, plus what came back — `tools`, `answer`, `cards`,
    `conversation`, `elapsed_s`. The model provider refusing (429 upstream)
    and this deployment's own per-account limits are the environment, not
    the product: both are UNAVAILABLE, and the checks that needed the turn
    do not run.
    """
    t = time.monotonic()
    out = {"tools": [], "answer": "", "cards": [], "conversation": None}
    try:
        r = s.post(f"{base}/api/chat", json={"agent_id": agent, "message": message},
                   stream=True, timeout=timeout)
    except requests.RequestException as exc:
        return {**out, "status": "FAIL", "detail": type(exc).__name__,
                "elapsed_s": round(time.monotonic() - t, 2)}
    if r.status_code in (429, 503):
        return {**out, "status": "UNAVAILABLE",
                "detail": f"HTTP {r.status_code}: {r.text[:120]}",
                "elapsed_s": round(time.monotonic() - t, 2)}
    if r.status_code != 200:
        return {**out, "status": "FAIL",
                "detail": f"HTTP {r.status_code}: {r.text[:120]}",
                "elapsed_s": round(time.monotonic() - t, 2)}
    texts, errors = [], []
    for ev in _sse(r):
        kind = ev.get("type")
        if kind == "tool":
            out["tools"].append(ev.get("name"))
        elif kind == "text":
            texts.append(ev.get("text") or "")
        elif kind == "error":
            errors.append(ev.get("text") or "")
        elif kind == "card":
            out["cards"].append(ev)
        out["conversation"] = ev.get("conversation_id") or out["conversation"]
    out["answer"] = "\n".join(texts)
    out["elapsed_s"] = round(time.monotonic() - t, 2)
    if errors and "more requests than I can answer" in errors[0]:
        # The model provider refused (rate limit / quota). That is the
        # environment, not the product; the checks that needed this turn
        # are then unavailable, not failed and not passed.
        return {**out, "status": "UNAVAILABLE",
                "detail": "model provider rate-limited this run"}
    if errors:
        return {**out, "status": "FAIL", "detail": f"agent error: {errors[0][:120]}"}
    if not out["answer"].strip():
        return {**out, "status": "FAIL", "detail": "no text came back"}
    return {**out, "status": "PASS",
            "detail": f"{len(out['answer'])} chars, tools={out['tools']}"}


# --- rows 7–12 ---------------------------------------------------------------

def row_smbp(run):
    """Row 7. Nothing to drive from a signed-in session; say so."""
    return run.step("SMBP triage (normal, abnormal, urgent)", "UNAVAILABLE",
                    SMBP_UNAVAILABLE)


def row_care_gaps(s, base, agent, run):
    """Row 8. The brief's preventive-care section, which is read without the
    model. Three honest states and one dishonest one: sourced items (PASS),
    "review unavailable" (UNAVAILABLE — the page said so), "none found"
    (PASS: the page only says it after a review that ran), and anything
    else, including items with no source, FAIL."""
    name = "care gaps on the brief"
    r = s.get(f"{base}/brief", params={"agent": agent}, timeout=30)
    if r.status_code != 200:
        return run.step(name, "FAIL", f"HTTP {r.status_code}")
    m = _BRIEF_GAPS_RE.search(r.text)
    if not m:
        return run.step(name, "FAIL", "the brief has no preventive-care section")
    body = m.group("body")
    if "Screening review unavailable" in body:
        return run.step(name, "UNAVAILABLE",
                        "the page says the screening review did not run")
    items = body.count('class="brief-field"')
    sourced = body.count("brief-source-id")
    if items:
        ok = sourced == items
        return run.step(name, "PASS" if ok else "FAIL",
                        f"{items} due item(s), {sourced} naming a source record",
                        due_items=items, sourced_items=sourced)
    if "We found no preventive care items" in body:
        return run.step(name, "PASS",
                        "review ran and found nothing due", due_items=0)
    return run.step(name, "FAIL", "the section shows neither items nor a state")


def row_care_gaps_chat(s, base, agent, run, timeout):
    """Row 8, the surface that evaluates today: a chat turn. The answer must
    be read from the care-gaps tool and say what is due. The words are the
    model's, so the check is loose: grounded, and it uses the word."""
    name = "care gaps in chat"
    turn = chat_turn(s, base, agent, CARE_GAPS_QUESTION, timeout)
    if turn["status"] != "PASS":
        return run.step(name, turn["status"], turn["detail"],
                        elapsed_s=turn["elapsed_s"])
    grounded = "get_care_gaps" in turn["tools"]
    names_due = re.search(r"\bdue\b", turn["answer"], re.I) is not None
    return run.step(name, "PASS" if grounded and names_due else "FAIL",
                    ("read from the care-gaps tool" if grounded
                     else f"not read from the care-gaps tool; tools={turn['tools']}")
                    + ("; names what is due" if names_due
                       else "; does not say what is due"),
                    elapsed_s=turn["elapsed_s"], tools=turn["tools"])


def _review_page(s, base, agent, action_id):
    r = s.get(f"{base}/review/{agent}/{action_id}", timeout=30)
    if r.status_code != 200:
        return r.status_code, None
    page = r.text
    nka = _NKA_INPUT_RE.search(page)
    return 200, {
        "meds": len(set(_MED_ROW_RE.findall(page))),
        "allergies": len(set(_ALLERGY_ROW_RE.findall(page))),
        "med_names": [_html.unescape(n).strip()
                      for n in _MED_NAME_RE.findall(page)],
        "nka_present": nka is not None,
        "nka_prechecked": bool(nka and re.search(r"\schecked\b", nka.group(0))),
        "readable": "We could not read your records" not in page,
    }


def row_form_review(s, base, agent, action_id, run):
    """Row 9. The form waits for the person: listed, each item a decision,
    NKA unticked, and a submit that says nothing about allergies refused.

    Returns the parsed review page (for row 10) or None.
    """
    name = "intake form: each item up for review"
    listed = s.get(f"{base}/agents/{agent}/approvals", timeout=20)
    if listed.status_code != 200 or f"/review/{agent}/{action_id}" not in listed.text:
        run.step(name, "FAIL",
                 f"approvals HTTP {listed.status_code}; the form is not listed")
        return None
    status, page = _review_page(s, base, agent, action_id)
    if status == 503:
        run.step(name, "UNAVAILABLE", "the review page could not be checked")
        return None
    if page is None:
        run.step(name, "FAIL", f"review page HTTP {status}")
        return None
    if not page["readable"]:
        run.step(name, "FAIL", "the review page could not read the records")
        return None
    if not page["nka_present"] or page["nka_prechecked"]:
        run.step(name, "FAIL", "'No known allergies' is missing or pre-ticked")
        return None
    # The gate, probed: every medication answered, every allergy removed,
    # nothing attested. The server must refuse — silence about allergies is
    # never read as "none".
    probe = {f"med-{i}": "yes" for i in range(page["meds"])}
    probe.update({f"allergy-{i}": "remove" for i in range(page["allergies"])})
    r = s.post(f"{base}/review/{agent}/{action_id}/submit", json=probe, timeout=30)
    if r.status_code != 422:
        run.step(name, "FAIL",
                 f"a submit attesting nothing about allergies answered "
                 f"HTTP {r.status_code}, not 422")
        return None
    run.step(name, "PASS",
             f"{page['meds']} medication(s) and {page['allergies']} allergy(ies) "
             "each need a decision; 'No known allergies' unticked; a submit "
             "without an allergy answer is refused (422)",
             medications=page["meds"], allergies=page["allergies"])
    return page


def _pdf_text(data):
    """Best-effort text of a reportlab PDF: every string shown on a page,
    joined by spaces. Enough to find a label; not a PDF parser."""
    parts = []
    for m in re.finditer(rb"<<(.*?)>>\s*stream\r?\n(.*?)endstream", data, re.S):
        head, raw = m.group(1), m.group(2).strip()
        try:
            if b"ASCII85Decode" in head:
                if raw.startswith(b"<~"):
                    raw = raw[2:]
                raw = base64.a85decode(raw.removesuffix(b"~>"))
            if b"FlateDecode" in head:
                raw = zlib.decompress(raw)
        except (ValueError, zlib.error):
            continue
        for s in re.findall(rb"\(((?:\\.|[^\\)])*)\)", raw):
            parts.append(re.sub(rb"\\(.)", rb"\1", s).decode("latin-1"))
    return re.sub(r"\s+", " ", " ".join(parts))


def row_form_approve(s, base, agent, action_id, page, run, *,
                     attest_nka=None, fetch=None):
    """Row 10. Keep every item as the record has it, submit, and read the
    signed PDF that comes back.

    `attest_nka` is asked only when the record lists no allergy; it must be
    a person answering. Without one the row is UNAVAILABLE and nothing is
    submitted. Returns the decisions submitted, or None.
    """
    name = "intake form approved and PDF delivered"
    fetch = fetch or requests.get
    decisions = {f"med-{i}": "yes" for i in range(page["meds"])}
    decisions.update({f"allergy-{i}": "confirm" for i in range(page["allergies"])})
    if not page["allergies"]:
        if attest_nka is None or not attest_nka():
            run.step(name, "UNAVAILABLE",
                     "the record lists no allergy and no person attested 'No "
                     "known allergies'; this script never ticks it")
            return None
        decisions["nka"] = "true"
    r = s.post(f"{base}/review/{agent}/{action_id}/submit", json=decisions, timeout=60)
    body = _json(r)
    if r.status_code != 200 or body.get("confirmed") is not True:
        run.step(name, "FAIL", f"submit HTTP {r.status_code}, "
                               f"confirmed={body.get('confirmed')!r}")
        return decisions
    st = s.get(f"{base}/api/form/{action_id}", params={"agent": agent}, timeout=20)
    status = _json(st)
    link = status.get("delivery_link")
    if status.get("status") != "completed" or not link:
        run.step(name, "FAIL", f"form status {status.get('status')!r}, "
                               f"link {'present' if link else 'missing'}")
        return decisions
    pdf = fetch(link, timeout=30)
    data = pdf.content or b""
    if pdf.status_code != 200 or data[:4] != b"%PDF":
        run.step(name, "FAIL", f"PDF link HTTP {pdf.status_code}, "
                               f"{'a PDF' if data[:4] == b'%PDF' else 'not a PDF'}")
        return decisions
    text = _pdf_text(data)
    names = page["med_names"]
    shown = sum(1 for n in names if n and n in text)
    reviewed = "Reviewed by patient on" in text
    ok = reviewed and shown == len(names)
    run.step(name, "PASS" if ok else "FAIL",
             f"{len(data)} bytes; {shown}/{len(names)} approved medication(s) "
             f"in the PDF; review line {'present' if reviewed else 'MISSING'}",
             pdf_bytes=len(data), medications_in_pdf=shown)
    return decisions


def row_restart(new_session, base, conn, agent, action_id, decisions,
                question, run):
    """Row 11. A fresh session — nothing carried over but the account — finds
    what the first one left: the conversation, the form's outcome unchanged,
    and a repeat of the same review refused rather than sent twice.

    Returns the new session, or None when it could not sign in.
    """
    s = new_session()
    if s is None:
        run.step("restart: signed in again", "FAIL", "could not sign in")
        return None
    run.step("restart: signed in again", "PASS", "a new session, same account")
    # Reconnect. The sample source is generated, not fetched, so the app
    # answers that there is nothing to re-pull; recorded as that, not as a
    # reconnect that happened.
    r = s.post(f"{base}/api/connections/{conn}/refresh", json={}, timeout=30)
    body = _json(r)
    if r.status_code == 200 and body.get("unsupported"):
        run.step("reconnect", "UNAVAILABLE",
                 "the sample source has nothing to re-pull (refresh answers "
                 "unsupported)")
    else:
        run.step("reconnect", "PASS" if r.status_code == 200 else "FAIL",
                 f"refresh HTTP {r.status_code} {body.get('status') or ''}".rstrip())
    if question:
        page = s.get(f"{base}/chat", params={"agent": agent}, timeout=20)
        there = page.status_code == 200 and question[:30] in page.text
        run.step("restart: earlier turn still there", "PASS" if there else "FAIL",
                 "the question is on the page" if there
                 else "the earlier turn is not on the page")
    else:
        run.step("restart: earlier turn still there", "UNAVAILABLE",
                 "no turn to look for")
    if not action_id:
        run.step("retry: form not duplicated", "UNAVAILABLE", "no form was started")
        return s
    st_r = s.get(f"{base}/api/form/{action_id}", params={"agent": agent},
                 timeout=20)
    st = _json(st_r)
    if st_r.status_code == 503:
        run.step("retry: form not duplicated", "UNAVAILABLE",
                 "the form's status could not be checked")
        return s
    if decisions is None:
        # Never submitted: it must still be waiting, not quietly complete.
        waiting = st.get("status") == "awaiting_confirmation"
        run.step("retry: form not duplicated", "PASS" if waiting else "FAIL",
                 f"unsubmitted form reads {st.get('status')!r}")
        return s
    r = s.post(f"{base}/review/{agent}/{action_id}/submit", json=decisions,
               timeout=30)
    listed = s.get(f"{base}/agents/{agent}/approvals", timeout=20)
    still_listed = f"/review/{agent}/{action_id}" in listed.text
    ok = (r.status_code in (404, 409) and st.get("status") == "completed"
          and not still_listed)
    run.step("retry: form not duplicated", "PASS" if ok else "FAIL",
             f"second submit HTTP {r.status_code}; status {st.get('status')!r}; "
             f"{'still' if still_listed else 'no longer'} listed as waiting")
    return s


def row_delete(s, base, conn, agent, action_id, run):
    """Row 12. Delete the sample connection, read the scope the answer
    states, then look for it from other places. The hub is read first: a
    connection it never showed would read as gone afterwards."""
    before = s.get(f"{base}/home", timeout=20)
    listed_before = before.status_code == 200 and conn in before.text
    r = s.delete(f"{base}/api/connections/{conn}", timeout=60)
    body = _json(r)
    if r.status_code == 502:
        return run.step("delete: records and connection", "UNAVAILABLE",
                        body.get("message") or "the engine did not confirm")
    scope_ok = (r.status_code == 200 and body.get("deleted") is True
                and body.get("unlinked") is True
                and body.get("audit_retained") is True
                and int(body.get("rows_deleted") or 0) > 0)
    run.step("delete: records and connection", "PASS" if scope_ok else "FAIL",
             f"HTTP {r.status_code}; {body.get('rows_deleted', 0)} record(s) "
             f"deleted; unlinked={body.get('unlinked')!r}; "
             f"audit kept={body.get('audit_retained')!r}",
             rows_deleted=body.get("rows_deleted"))
    home = s.get(f"{base}/home", timeout=20)
    labs = s.get(f"{base}/api/labs/timeline", params={"agent": agent}, timeout=20)
    again = s.delete(f"{base}/api/connections/{conn}", timeout=60)
    seen = {"hub": (listed_before and home.status_code == 200
                    and conn not in home.text),
            "labs": labs.status_code == 404,
            "second delete": again.status_code == 404}
    if action_id:
        form = s.get(f"{base}/api/form/{action_id}", params={"agent": agent},
                     timeout=20)
        seen["form"] = form.status_code == 404
    gone = all(seen.values())
    return run.step("delete: observed gone", "PASS" if gone else "FAIL",
                    ", ".join(f"{k} {'gone' if v else 'STILL THERE'}"
                              for k, v in seen.items()))


def _ask_nka():
    """A person at the terminal, or nobody. Never a default."""
    if not sys.stdin.isatty():
        return False
    answer = input("The synthetic record lists no allergy. As the reviewing "
                   "person, type NKA to attest 'No known allergies' "
                   "(anything else skips): ").strip()
    return answer == "NKA"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"CareAgents origin (default {DEFAULT_BASE})")
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
    if not sign_in(s, base, args.email, args.code_log, run):
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
    turn = chat_turn(s, base, agent, QUESTION, args.timeout)
    conversation = turn["conversation"]
    run.step("chat turn", turn["status"], turn["detail"],
             elapsed_s=turn["elapsed_s"],
             **({"tools": turn["tools"]} if turn["status"] == "PASS" else {}))
    if turn["status"] == "PASS":
        tools, answer = turn["tools"], turn["answer"]
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

    # Checklist rows 7–12.
    row_smbp(run)
    row_care_gaps(s, base, agent, run)
    row_care_gaps_chat(s, base, agent, run, args.timeout)

    turn = chat_turn(s, base, agent, FORM_REQUEST, args.timeout)
    action_id = next((c.get("action_id") for c in turn["cards"]
                      if c.get("kind") == "review"), None)
    if turn["status"] != "PASS":
        run.step("intake form requested", turn["status"], turn["detail"],
                 elapsed_s=turn["elapsed_s"])
    else:
        run.step("intake form requested", "PASS" if action_id else "FAIL",
                 "a review card came back" if action_id
                 else f"no review card; tools={turn['tools']}",
                 elapsed_s=turn["elapsed_s"])
    decisions = None
    if action_id:
        page = row_form_review(s, base, agent, action_id, run)
        if page:
            decisions = row_form_approve(s, base, agent, action_id, page, run,
                                         attest_nka=_ask_nka)
        else:
            run.step("intake form approved and PDF delivered", "UNAVAILABLE",
                     "the review step did not pass")
    else:
        run.step("intake form: each item up for review", "UNAVAILABLE",
                 "no form was started")
        run.step("intake form approved and PDF delivered", "UNAVAILABLE",
                 "no form was started")

    def fresh():
        s2 = requests.Session()
        return s2 if sign_in(s2, base, args.email, args.code_log, None) else None
    s = row_restart(fresh, base, conn, agent, action_id, decisions,
                    QUESTION if reopened else None, run) or s

    row_delete(s, base, conn, agent, action_id, run)
    return run.finish()


if __name__ == "__main__":
    sys.exit(main())
