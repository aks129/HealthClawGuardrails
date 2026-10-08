#!/usr/bin/env python3
"""End-to-end shakeout of CareAgents + HealthClaw with a synthetic cohort.

    python scripts/synthetic_hospital_cohort.py --data <release> --out <cohort>
    python scripts/cohort_shakeout.py --cohort <cohort> \\
        --care http://localhost:5301 --care-log <careagents stderr log> \\
        [--prompt-log <model request log>] [--healthclaw-db <sqlite path>]

Runs against a LOCAL stack only: sign-in codes are read from the CareAgents
dev mail stub's log (careagents/mail.py), which production never writes.

For each cohort bundle it signs up an account, opens a file connection with
consent, uploads the bundle, creates an assistant and asks it questions.
Then it checks, across accounts:

  V1  No patient canary name, phone or clinician name in any chat answer,
      labs timeline, review page, or (with --prompt-log) any model prompt.
  V2  Every tenant that was read has audit rows (with --healthclaw-db).
  V3  An intake form waits for approval; nothing is sent before Approve.
  V6  Account B is refused account A's agent, connection, run and review.
  S   Upload counts: every entry lands; nothing is dropped silently.
  D   Delete my account purges the tenant; the email can sign up again.

Exit 0 when every check passes, 1 when any fails. Each failure prints the
patient, the check and the evidence.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

QUESTIONS = [
    "What were my most recent lab results?",
    "What medications am I taking?",
    "Do I have any allergies?",
    "Am I due for any screenings or vaccines?",
    "Show me how my hemoglobin has changed over time",
    "Give me a summary of everything in my record",
    "When was my last doctor visit and what was it for?",
    "¿Cuáles son mis alergias? 请告诉我",  # non-English input
    "x" * 1999,  # at the length limit
]

FAILURES: list[str] = []
PASSES: list[str] = []


def check(ok: bool, label: str, evidence: str = "") -> bool:
    (PASSES if ok else FAILURES).append(label + (f" :: {evidence}" if evidence and not ok else ""))
    print(("  PASS " if ok else "  FAIL ") + label + ("" if ok else f" :: {evidence}"))
    return ok


def why(r) -> str:
    """A refusal or failure, stated without the response body: the status and
    the JSON `error` code when there is one. Bodies can carry record content,
    so they are never printed."""
    code = ""
    try:
        body = r.json()
        if isinstance(body, dict):
            code = str(body.get("error") or "")[:60]
    except ValueError:
        pass
    return f"{r.status_code} {code}".strip()


def kinds(leaked: list[str], m: dict) -> str:
    """Which KIND of canary leaked, never the value."""
    out = []
    for c in leaked:
        if c == m["canary_family"]:
            out.append("patient name")
        elif c in m["clinicians"]:
            out.append("clinician name")
        else:
            out.append("patient phone")
    return f"{len(leaked)} leaked: " + ", ".join(sorted(set(out)))


def code_from_log(log: Path, email: str, timeout: float = 15) -> str:
    rx = re.compile(r"for " + re.escape(email) + r": (\d{6,8})")
    end = time.time() + timeout
    while time.time() < end:
        found = rx.findall(log.read_text(errors="replace"))
        if found:
            return found[-1]
        time.sleep(0.3)
    raise RuntimeError(f"no sign-in code for {email} in {log}")


def consent_body(s, base):
    """Consent as the hub card sends it: with the terms version the card was
    rendered with (security review of #904, F4). Read from the page, never
    from a 428, so a terms change mid-run is refused, not accepted."""
    page = s.get(f"{base}/home", timeout=10).text
    m = re.search(r'data-consent-version="([^"]*)"', page)
    return {"consent": True, "consent_version": m.group(1) if m else ""}


def sign_in(base: str, log: Path, email: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{base}/api/auth/email", json={"email": email}, timeout=10)
    r.raise_for_status()
    code = code_from_log(log, email)
    r = s.post(f"{base}/api/auth/verify", json={"email": email, "code": code}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"verify failed {why(r)}")
    return s


def chat(s: requests.Session, base: str, agent_id: str, text: str, timeout: float = 90):
    """Return (answer text, final status, raw events)."""
    r = s.post(f"{base}/api/chat", json={"agent_id": agent_id, "message": text},
               stream=True, timeout=timeout)
    if r.status_code != 200:
        return None, f"http {why(r)}", []
    events, answer, status = [], [], None
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        ev = json.loads(line[6:])
        events.append(ev)
        if ev.get("type") in ("token", "text", "message", "answer", "assistant"):
            answer.append(ev.get("text") or ev.get("content") or "")
        if ev.get("type") == "done":
            status = ev.get("status")
            break
        if ev.get("type") in ("error", "reconnect"):
            status = ev["type"]
            break
    return "".join(answer), status, events


def canaries_in(text: str, canaries: list[str]) -> list[str]:
    low = text.lower()
    return [c for c in canaries if c and c.lower() in low]


def run(args) -> int:
    base, log = args.care.rstrip("/"), Path(args.care_log)
    manifest = json.loads((Path(args.cohort) / "manifest.json").read_text())
    if args.limit:
        manifest = manifest[:args.limit]
    stamp = int(time.time())
    accounts = []
    all_canaries: list[str] = []

    for i, m in enumerate(manifest):
        bundle_path = Path(args.cohort) / m["file"]
        bundle = json.loads(bundle_path.read_text())
        phone = next((t["value"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
                      for t in e["resource"].get("telecom", [])), "")
        canaries = [m["canary_family"], phone] + m["clinicians"]
        all_canaries += canaries
        email = f"sh-{m['patient_id']}@example.test"
        print(f"\n== {m['file']} ({m['reason']}, {sum(m['counts'].values())} entries)")
        try:
            s = sign_in(base, log, email)
        except Exception as exc:  # noqa: BLE001 - report and move on
            check(False, f"{m['file']} sign-in", str(exc))
            continue

        r = s.post(f"{base}/api/connections/direct", json={}, timeout=10)
        check(r.status_code == 428, f"{m['file']} file connection refused without consent",
              why(r))
        r = s.post(f"{base}/api/connections/direct", json=consent_body(s, base), timeout=10)
        if not check(r.status_code == 200 and r.json().get("id"),
                     f"{m['file']} file connection opens with consent", why(r)):
            continue
        conn_id = r.json()["id"]

        raw = bundle_path.read_bytes()
        t0 = time.time()
        r = s.post(f"{base}/api/connections/{conn_id}/upload", data=raw,
                   headers={"Content-Type": "application/fhir+json"}, timeout=120)
        up_secs = time.time() - t0
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        total = len(bundle["entry"])
        landed = body.get("ingested", 0) if isinstance(body, dict) else 0
        check(r.status_code == 200 and landed == total and not body.get("failed"),
              f"{m['file']} upload lands every entry ({total}) in {up_secs:.1f}s",
              f"{why(r)} ingested={landed} failed={body.get('failed')}")
        check("tenant_id" not in json.dumps(body), f"{m['file']} upload answer hides tenant id")
        leaked = canaries_in(json.dumps(body), canaries)
        check(not leaked, f"{m['file']} upload answer has no canary", kinds(leaked, m))

        r = s.post(f"{base}/api/agents", json={"name": f"Shake {i}", "connection_id": conn_id},
                   timeout=10)
        if not check(r.status_code == 200, f"{m['file']} assistant created", why(r)):
            continue
        agent_id = r.json()["id"]

        picks = [QUESTIONS[(i + k) % len(QUESTIONS)] for k in range(args.questions)]
        for q in picks:
            answer, status, events = chat(s, base, agent_id, q)
            label = f"{m['file']} chat '{q[:40]}'"
            if not check(status == "completed" and bool(answer), label + " completes",
                         f"status={status} events={json.dumps(events)[:300]}"):
                continue
            leaked = canaries_in(answer, canaries)
            check(not leaked, label + " answer has no canary", kinds(leaked, m))
            if "lab" in q.lower() and m["lab_lines"]:
                check("no lab" not in answer.lower() and "don't see" not in answer.lower(),
                      label + " finds the stored labs", "answer says no labs")

        r = s.get(f"{base}/api/labs/timeline", params={"agent": agent_id, "topic": "hemoglobin"},
                  timeout=20)
        check(r.status_code == 200, f"{m['file']} labs timeline answers", why(r))
        if r.status_code == 200:
            leaked = canaries_in(r.text, canaries)
            check(not leaked, f"{m['file']} labs timeline has no canary", kinds(leaked, m))

        accounts.append({"m": m, "s": s, "email": email, "conn": conn_id, "agent": agent_id,
                         "canaries": canaries})

    # Real records are invite-only: a stranger cannot open a file connection.
    print("\n== uninvited account")
    try:
        s = sign_in(base, log, f"stranger-{stamp}@example.test")
        r = s.post(f"{base}/api/connections/direct", json=consent_body(s, base), timeout=10)
        check(r.status_code != 200 or r.json().get("soon"), "an uninvited account cannot open a file "
              "connection", why(r))
    except Exception as exc:  # noqa: BLE001
        check(False, "uninvited sign-in", str(exc))

    # V3: an intake form waits for a person.
    if accounts:
        a = accounts[0]
        print(f"\n== approvals ({a['m']['file']})")
        answer, status, events = chat(a["s"], base, a["agent"], "Please start my new patient intake form")
        check(status in ("completed", "waiting_for_human"), "intake request runs", f"status={status}")
        page = a["s"].get(f"{base}/agents/{a['agent']}/approvals", timeout=10)
        check(page.status_code == 200, "approvals page renders", str(page.status_code))
        ids = sorted(set(re.findall(r"/review/" + re.escape(a["agent"]) + r"/([A-Za-z0-9_-]+)", page.text)))
        if check(bool(ids), "a prepared form waits on the approvals page", "no review link"):
            review = a["s"].get(f"{base}/review/{a['agent']}/{ids[0]}", timeout=10)
            check(review.status_code == 200, "review page renders", str(review.status_code))
            # The person's own name and phone are on their own form by design
            # (r6/actions/review.py: demographics read-only). Upstream labels,
            # such as clinician names, are not.
            leaked = canaries_in(review.text, a["m"]["clinicians"])
            check(not leaked, "review page has no clinician name", kinds(leaked, a["m"]))
            st = a["s"].get(f"{base}/api/form/{ids[0]}", params={"agent": a["agent"]}, timeout=10)
            check(st.ok and st.json().get("status") not in ("completed", "executed", "sent"),
                  "nothing is sent before Approve", why(st))
            bare = a["s"].post(f"{base}/review/{a['agent']}/{ids[0]}/submit", data={}, timeout=10)
            st2 = a["s"].get(f"{base}/api/form/{ids[0]}", params={"agent": a["agent"]}, timeout=10)
            check(st2.json().get("status") not in ("completed", "executed", "sent"),
                  "a bare submit without the allergy attestation does not send",
                  f"submit {bare.status_code}; status {st2.json().get('status')}")

    # V6: account B is refused account A's things.
    print("\n== cross-account isolation")
    for a, b in zip(accounts, accounts[1:] + accounts[:1]):
        if a is b:
            break
        tag = f"{b['m']['file']} -> {a['m']['file']}"
        probes = {
            "chat": b["s"].post(f"{base}/api/chat", json={"agent_id": a["agent"], "message": "hi"},
                                timeout=10),
            "labs": b["s"].get(f"{base}/api/labs/timeline", params={"agent": a["agent"]}, timeout=10),
            "upload": b["s"].post(f"{base}/api/connections/{a['conn']}/upload", data=b"{}",
                                  headers={"Content-Type": "application/json"}, timeout=10),
            "disconnect": b["s"].post(f"{base}/api/connections/{a['conn']}/disconnect", timeout=10),
            "rename": b["s"].post(f"{base}/api/agents/{a['agent']}/rename", json={"name": "x"},
                                  timeout=10),
            "approvals": b["s"].get(f"{base}/agents/{a['agent']}/approvals", timeout=10,
                                    allow_redirects=False),
            "chat page": b["s"].get(f"{base}/chat", params={"agent": a["agent"]}, timeout=10,
                                    allow_redirects=False),
        }
        for name, r in probes.items():
            leaked = canaries_in(r.text, a["canaries"])
            check(r.status_code in (302, 303, 400, 403, 404) and not leaked,
                  f"{tag} {name} refused", f"{why(r)}; {kinds(leaked, a['m'])}")

    # V1: what the model was sent.
    if args.prompt_log and Path(args.prompt_log).exists():
        print("\n== model prompts")
        text = Path(args.prompt_log).read_text(errors="replace")
        n = text.count("\n")
        leaked = sorted(set(canaries_in(text, all_canaries)))
        check(n > 0 and not leaked, f"{n} model requests carry no canary",
              f"{len(leaked)} distinct canary values leaked")

    # V2: audit rows per tenant.
    if args.healthclaw_db:
        print("\n== audit")
        from sqlalchemy import create_engine, inspect, text
        url = args.healthclaw_db if "://" in args.healthclaw_db else f"sqlite:///{args.healthclaw_db}"
        eng = create_engine(url)
        table = next(t for t in ("audit_events", "audit_event") if inspect(eng).has_table(t))
        with eng.connect() as con:
            rows = con.execute(text(f"select tenant_id, count(*) from {table} group by tenant_id")).fetchall()
        eng.dispose()
        check(len(rows) >= len(accounts), f"audit rows exist for {len(rows)} tenants "
              f"(accounts: {len(accounts)})", str(rows)[:300])

    # D: delete my account, then sign up again.
    if accounts:
        a = accounts[-1]
        print(f"\n== delete account ({a['m']['file']})")
        r = a["s"].post(f"{base}/api/account/delete", json={}, timeout=10)
        check(r.status_code == 400, "delete without confirm is refused", str(r.status_code))
        r = a["s"].post(f"{base}/api/account/delete", json={"confirm": "DELETE"}, timeout=60)
        check(r.ok and r.json().get("deleted") is True, "delete my account", why(r))
        r = a["s"].post(f"{base}/api/chat", json={"agent_id": a["agent"], "message": "hi"}, timeout=10)
        check(r.status_code in (302, 401, 403, 404), "old session cannot chat after delete",
              str(r.status_code))
        try:
            s2 = sign_in(base, log, a["email"])
            r = s2.post(f"{base}/api/connections/direct", json=consent_body(s2, base), timeout=10)
            check(r.ok, "the same email signs up again and connects", why(r))
        except Exception as exc:  # noqa: BLE001
            check(False, "the same email signs up again", str(exc))

    print(f"\n{len(PASSES)} passed, {len(FAILURES)} failed")
    for f in FAILURES:
        print("  FAIL", f)
    return 1 if FAILURES else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cohort", required=True)
    ap.add_argument("--care", default="http://localhost:5301")
    ap.add_argument("--care-log", required=True)
    ap.add_argument("--prompt-log")
    ap.add_argument("--healthclaw-db")
    ap.add_argument("--questions", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
