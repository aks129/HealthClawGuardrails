#!/usr/bin/env python3
"""Live-data shakeout scorecard — is the agent actually using the record?

    railway ssh --service HealthClawGuardrails \
        "python scripts/shakeout_live.py --tenant <tenant-id>"

or locally against any database:

    SQLALCHEMY_DATABASE_URI=... python scripts/shakeout_live.py --tenant t1

Exit codes, matching scripts/prod_watch.py:

    0  every automatable row passes
    1  a row regressed
    2  cannot evaluate (no database, empty tenant)

Why this exists
---------------
prod_watch answers "is the product up?"; nothing answered "is the agent
actually using the person's data?". On 2026-08-04 both were needed in the
same hour: the product was up, the import was perfect (698/0/0), and the
agent was answering "I don't see any recent blood work" over 186 stored
Observations — a failure no health check saw. The shakeout rows come from
docs/2026-08-04-plan-live-data-shakeout.md.

The measurement trick: THE AUDIT TRAIL IS THE SCORECARD. `labs $interpret`
writes `interpreted=N flagged=M` to audit detail; every read writes an
AuditEvent; agent_runs records error_class; checkpoints count tool rounds.
All of it PHI-free by construction (audit detail is constitutionally
PHI-free), so this proves the agent USED the data without reading the data.

Scope, stated honestly
----------------------
This sees the server side only. It cannot read the agent's prose, so it
cannot check: answer quality, the S4 "recorded but not coded" wording
reaching the person, or cross-tenant leakage in generated text (S7 is
enforced by the kernel and pinned by isolation tests; the residual check is
reading an answer). Those are the owner's five minutes in the UI, with the
exact questions listed in the plan doc. A scorecard that quietly checked
less than it appears to would be worse than a narrow, stated one.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse

G, R, Y, D, X = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"

# agent_runs.error_class values that mean "we showed the person a defect
# message". LLMRateLimited is deliberately NOT here — being throttled is a
# truthfully-reported non-defect after PR #345.
DEFECT_ERROR_CLASSES = ("LLMError",)


def _connect(uri: str):
    import psycopg2  # deferred: the container has it; a laptop may not

    uri = uri.replace("postgresql+psycopg2://", "postgresql://", 1)
    conn = psycopg2.connect(uri, connect_timeout=15)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def _one(cur, sql: str, params: tuple):
    cur.execute(sql, params)
    row = cur.fetchone()
    return row


def check_s1_labs_interpreted(cur, tenant: str) -> tuple[str, str]:
    """S1: the most recent labs $interpret actually interpreted something."""
    row = _one(cur, """
        SELECT detail, recorded_at FROM audit_events
        WHERE tenant_id = %s AND detail LIKE 'labs $interpret;%%'
        ORDER BY recorded_at DESC LIMIT 1
    """, (tenant,))
    if row is None:
        return "SKIP", "no $interpret call recorded yet — ask 'what do my labs say?' first"
    match = re.search(r"interpreted=(\d+)", row[0] or "")
    if not match:
        return "FAIL", f"audit detail has no interpreted= marker: {row[0]!r}"
    n = int(match.group(1))
    if n == 0:
        return "FAIL", ("latest $interpret interpreted 0 observations — "
                        "the #342 empty-body bug shape")
    return "PASS", f"latest $interpret interpreted {n} observations at {row[1]}"


def check_s3_medication_reads(cur, tenant: str) -> tuple[str, str]:
    """S3: reference-chasing leaves audited Medication reads (PR #347)."""
    row = _one(cur, """
        SELECT count(*) FROM audit_events
        WHERE tenant_id = %s AND resource_type = 'Medication'
          AND event_type = 'read'
    """, (tenant,))
    if row[0] == 0:
        return "SKIP", ("no audited Medication reads yet — needs #347 "
                        "deployed and a 'what medications am I on?' ask")
    return "PASS", f"{row[0]} audited Medication read(s) — deref is live and visible"


def check_s5_run_health(cur, tenant: str) -> tuple[str, str]:
    """S5: recent runs complete, and failures carry an honest class."""
    cur.execute("""
        SELECT status, coalesce(error_class, ''), count(*)
        FROM agent_runs WHERE tenant_id = %s
        GROUP BY status, error_class
    """, (tenant,))
    rows = cur.fetchall()
    if not rows:
        return "SKIP", "no agent runs for this tenant yet"
    total = sum(r[2] for r in rows)
    defects = {r[1]: r[2] for r in rows if r[1] in DEFECT_ERROR_CLASSES}
    completed = sum(r[2] for r in rows if r[0] == "completed")
    if defects:
        return "FAIL", (f"{sum(defects.values())}/{total} runs failed with a "
                        f"defect-class error: {defects} — read the worker log "
                        "before assuming a provider blip")
    return "PASS", f"{completed}/{total} runs completed; no defect-class failures"


def check_s8_tool_rounds(cur, tenant: str) -> tuple[str, str]:
    """S8: tool-loop inflation. 8 rounds on one question preceded the 429."""
    row = _one(cur, """
        SELECT max(n) FROM (
            SELECT r.id, count(e.id) AS n
            FROM agent_runs r JOIN agent_run_events e ON e.run_id = r.id
            WHERE r.tenant_id = %s AND e.event_type = 'agent.checkpoint'
            GROUP BY r.id) rounds
    """, (tenant,))
    worst = row[0]
    if worst is None:
        return "SKIP", "no checkpointed runs yet"
    if worst > 6:
        return "FAIL", (f"a run took {worst} model rounds — the unlabeled-"
                        "records loop shape that ended in a rate limit")
    return "PASS", f"worst run used {worst} model round(s) (budget 6)"


def check_ingest_not_stranded(cur, tenant: str) -> tuple[str, str]:
    """No FastenJob wedged non-terminal (the reaper's blind spot, live)."""
    row = _one(cur, """
        SELECT count(*) FROM fasten_jobs
        WHERE tenant_id = %s
          AND status NOT IN ('complete', 'completed', 'failed')
    """, (tenant,))
    if row[0]:
        return "FAIL", f"{row[0]} ingest job(s) stuck in a non-terminal state"
    return "PASS", "no stranded ingest jobs"


CHECKS = [
    ("S1 labs interpreted", check_s1_labs_interpreted),
    ("S3 medication deref audited", check_s3_medication_reads),
    ("S5 run health honest", check_s5_run_health),
    ("S8 tool rounds bounded", check_s8_tool_rounds),
    ("ingest not stranded", check_ingest_not_stranded),
]

MANUAL_ROWS = """\
Owner's five minutes in the UI (this script cannot read prose):
  S1  "What do my labs say?"          -> cites actual values, incl. cholesterol
  S2  "What conditions do I have?"    -> names them; no "cannot read clearly" for coded rows
  S3  "What medications am I on?"     -> names, not "I can't read the names"
  S4  "Do I have any allergies?"      -> "recorded but not coded at the source", never absence
  S5  "Give me a timeline of my cholesterol results"  -> completes, or honest busy message
  S7  any answer                      -> nothing from another tenant's record
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", required=True,
                    help="tenant id to score (an opaque id, never PHI)")
    args = ap.parse_args()

    uri = os.environ.get("SQLALCHEMY_DATABASE_URI", "")
    if not uri:
        print(f"{R}no SQLALCHEMY_DATABASE_URI — run inside the container "
              f"(railway ssh) or export a database url{X}")
        return 2
    host = urllib.parse.urlsplit(
        uri.replace("postgresql+psycopg2://", "postgresql://", 1)).hostname
    print(f"{D}scoring tenant {args.tenant} against {host}{X}\n")

    conn = _connect(uri)
    cur = conn.cursor()

    failed = evaluated = 0
    for name, check in CHECKS:
        try:
            verdict, detail = check(cur, args.tenant)
        except Exception as exc:  # noqa: BLE001 — report, don't crash the card
            verdict, detail = "FAIL", f"check crashed: {type(exc).__name__}"
        color = {"PASS": G, "FAIL": R, "SKIP": Y}[verdict]
        print(f"  {color}{verdict:4}{X}  {name}: {detail}")
        if verdict != "SKIP":
            evaluated += 1
        if verdict == "FAIL":
            failed += 1

    cur.close()
    conn.close()

    print()
    print(MANUAL_ROWS)
    if evaluated == 0:
        print(f"{Y}nothing was evaluable — is this the right tenant?{X}")
        return 2
    if failed:
        print(f"{R}{failed} row(s) regressed{X}")
        return 1
    print(f"{G}all evaluable rows pass ({evaluated} checked){X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
