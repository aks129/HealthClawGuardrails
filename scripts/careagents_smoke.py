#!/usr/bin/env python3
"""CareAgents live smoke — the product works, end to end, or this fails loud.

    python scripts/careagents_smoke.py                        # careagents.cloud
    python scripts/careagents_smoke.py --base http://127.0.0.1:8600

Drives: landing → create agent (fresh tenant, seeded) → one real chat turn
(labs, tool-grounded) → start intake form → review relay renders → the
allergy-attestation gate rejects a bare submit through the relay → honest
submit → confirm/execute → signed PDF downloads. One LLM call is involved
(the chat turn), so run against a configured deployment.
"""
from __future__ import annotations

import argparse
import json

import sys

import requests

G, R, D, X = "\033[92m", "\033[91m", "\033[2m", "\033[0m"


def fail(msg):
    print(f"{R}FAIL{X} {msg}")
    sys.exit(1)


def ok(msg):
    print(f"{G} OK {X} {msg}")


def sse_events(resp):
    for chunk in resp.iter_lines(decode_unicode=True):
        if chunk and chunk.startswith("data: "):
            yield json.loads(chunk[6:])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="https://careagents.cloud")
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    s = requests.Session()
    print(f"{D}CareAgents smoke → {base}{X}\n")

    r = s.get(f"{base}/", timeout=args.timeout)
    if r.status_code != 200 or "Create your agent" not in r.text:
        fail(f"landing {r.status_code}")
    ok("landing renders")

    r = s.get(f"{base}/healthz", timeout=args.timeout)
    provider = (r.json() or {}).get("provider") if r.ok else None
    if not provider:
        fail(f"healthz {r.status_code}")
    ok(f"healthy (LLM provider: {provider})")

    r = s.post(f"{base}/start", data={"agent_name": "SmokeTest",
                                      "persona": "direct"},
               timeout=args.timeout, allow_redirects=True)
    if r.status_code != 200 or "SmokeTest" not in r.text:
        fail(f"agent setup {r.status_code}")
    ok("agent created (fresh tenant seeded)")

    r = s.post(f"{base}/api/chat", json={"message": "What do my labs say?"},
               stream=True, timeout=args.timeout)
    if r.status_code != 200:
        fail(f"chat turn {r.status_code}")
    saw_tool = saw_text = False
    for ev in sse_events(r):
        saw_tool = saw_tool or ev.get("type") == "tool"
        saw_text = saw_text or (ev.get("type") == "text"
                                and len(ev.get("text") or "") > 20)
        if ev.get("type") == "error":
            fail(f"chat error event: {ev.get('text')}")
    if not saw_text:
        fail("chat produced no answer")
    ok(f"chat turn answered ({'tool-grounded' if saw_tool else 'no tool?!'})")
    if not saw_tool:
        fail("answer was not grounded in a tool call")

    r = s.post(f"{base}/api/chat",
               json={"message": "Fill out my intake form for a new doctor."},
               stream=True, timeout=args.timeout)
    action_id = None
    for ev in sse_events(r):
        if ev.get("type") == "card" and ev.get("kind") == "review":
            action_id = ev["action_id"]
    if not action_id:
        fail("agent did not start the intake form")
    ok(f"intake form proposed ({action_id})")

    r = s.get(f"{base}/review/{action_id}", timeout=args.timeout)
    if r.status_code != 200 or f"/review/{action_id}/submit" not in r.text:
        fail(f"review relay {r.status_code}")
    ok("review page relayed with same-origin submit")

    bare = {f"med-{i}": "yes" for i in range(10)}
    bare.update({f"allergy-{i}": "remove" for i in range(10)})
    r = s.post(f"{base}/review/{action_id}/submit", json=bare,
               timeout=args.timeout)
    if r.status_code != 422:
        fail(f"attestation gate should 422 through the relay, got {r.status_code}")
    ok("allergy-attestation gate holds through the relay (422)")

    honest = {"nka": "true"}
    honest.update({f"med-{i}": "yes" for i in range(10)})
    honest.update({f"allergy-{i}": "confirm" for i in range(10)})
    r = s.post(f"{base}/review/{action_id}/submit", json=honest,
               timeout=args.timeout)
    if r.status_code != 200:
        fail(f"honest review {r.status_code}: {r.text[:120]}")
    ok("honest review accepted → out-of-band confirm ran")

    r = s.get(f"{base}/api/form/{action_id}", timeout=args.timeout)
    d = r.json() if r.ok else {}
    link = d.get("delivery_link")
    if d.get("status") != "completed" or not link:
        fail(f"form not completed: {d}")
    ok("action completed with signed delivery link")

    pdf = requests.get(link, timeout=args.timeout)  # clean client, no cookies
    if pdf.status_code != 200 or not pdf.content.startswith(b"%PDF"):
        fail(f"signed PDF fetch {pdf.status_code}")
    ok(f"signed PDF downloads ({len(pdf.content)} bytes) — no auth headers")

    print(f"\n{G}CareAgents is alive end to end.{X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
