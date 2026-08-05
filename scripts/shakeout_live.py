#!/usr/bin/env python3
"""Live-data shakeout scorecard — is the agent actually using the record?

    railway ssh --service HealthClawGuardrails \
        "python scripts/shakeout_live.py --list-tenants"
    railway ssh --service HealthClawGuardrails \
        "python scripts/shakeout_live.py --tenant <tenant-id>"

or locally against any database:

    SQLALCHEMY_DATABASE_URI=... python scripts/shakeout_live.py --tenant t1

Exit codes, matching scripts/prod_watch.py:

    0  every automatable row passes
    1  a row regressed
    2  cannot evaluate (no database, or no behavioural row was scored —
       an empty tenant, or the wrong one)

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

Finding the tenant (#378)
-------------------------
`--list-tenants` exists because the first live run scored two tenants that
reported every behavioural row as SKIP, which reads as "the deploy is
unexercised" — while the agent was running on a third tenant found only by
hand-querying `AgentRun`. A SKIP is supposed to mean "nothing has exercised
this yet"; pointed at the wrong tenant it means "you are looking in the wrong
place", and the scorecard could not tell those two apart. Distinguishing
states honestly is the whole product, so a state it cannot name is a defect.

The discovery listing carries tenant ids, counts and timestamps and nothing
else — the same class of data the audit trail already holds, PHI-free by
construction rather than by filtering.
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

# S5 looks at a WINDOW, not all history. Scored over all time, a single
# already-fixed failure fails the card forever, and a card that cannot return
# to green stops being read — which is how a real regression gets missed. The
# first live run hit exactly this: one pre-#345 LLMError from a provider 429,
# fixed hours earlier, still failing the row.
RUN_HEALTH_WINDOW_HOURS = 24


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


# --- tenant discovery (#378) ------------------------------------------------
#
# Three sources, because a tenant can be interesting for three different
# reasons and any one of them alone would hide the tenant this script was
# pointed at by hand: agent runs (someone asked something), stored resources
# (records landed), audit events (anything touched the record at all).
#
# Every column here is an id, a count or a timestamp. No resource content, no
# audit `detail`, no free text of any kind reaches this query — the listing is
# PHI-free by construction, not by redaction.

DISCOVER_TENANTS_SQL = """
    SELECT tenant_id,
           sum(runs) AS runs,
           sum(resources) AS resources,
           sum(audits) AS audits,
           max(last_seen) AS last_seen
    FROM (
        SELECT tenant_id, count(*) AS runs, 0 AS resources, 0 AS audits,
               max(created_at) AS last_seen
          FROM agent_runs GROUP BY tenant_id
        UNION ALL
        SELECT tenant_id, 0, count(*), 0, max(last_updated)
          FROM r6_resources
         WHERE coalesce(is_deleted, FALSE) = FALSE
         GROUP BY tenant_id
        UNION ALL
        SELECT tenant_id, 0, 0, count(*), max(recorded)
          FROM audit_events
         WHERE tenant_id IS NOT NULL
         GROUP BY tenant_id
    ) activity
    GROUP BY tenant_id
    ORDER BY max(last_seen) DESC
"""


def discover_tenants(cur) -> list[tuple]:
    """(tenant, runs, resources, audits, last_seen), newest activity first."""
    cur.execute(DISCOVER_TENANTS_SQL, ())
    return [(r[0], int(r[1] or 0), int(r[2] or 0), int(r[3] or 0), r[4])
            for r in cur.fetchall()]


def tenant_record_count(cur, tenant: str) -> int:
    """How many live resources this tenant holds. Separates "nobody has asked
    yet" from "there is nothing here to ask about"."""
    row = _one(cur, """
        SELECT count(*) FROM r6_resources
        WHERE tenant_id = %s AND coalesce(is_deleted, FALSE) = FALSE
    """, (tenant,))
    return int(row[0] or 0) if row else 0


def format_tenant_table(rows: list[tuple]) -> str:
    """Fixed-width listing. Counts, never content."""
    if not rows:
        return ("no tenant has any agent run, stored resource or audit event "
                "on this database")
    head = (f"  {'tenant':<32}{'runs':>7}{'records':>10}{'audits':>9}"
            f"   last activity")
    out = [head]
    for tenant, runs, resources, audits, last_seen in rows:
        seen = str(last_seen or "")[:19] or "-"
        out.append(f"  {str(tenant)[:32]:<32}{runs:>7}{resources:>10}"
                   f"{audits:>9}   {seen}")
    return "\n".join(out)


def recent_activity_hint(rows: list[tuple], limit: int = 5) -> str:
    """One line naming where the activity actually is.

    A fruitless run has to end by pointing somewhere. Without this the reader
    is left choosing between "the deploy is unexercised" and "wrong tenant" —
    the two states #378 was filed about.
    """
    if not rows:
        return ("tenants with recent activity: none — this database has no "
                "agent runs, stored resources or audit events at all")
    names = ", ".join(str(r[0]) for r in rows[:limit])
    more = f" (+{len(rows) - limit} more)" if len(rows) > limit else ""
    return f"tenants with recent activity: {names}{more}"


EMPTY_TENANT_SKIP = ("this tenant holds no records — asking the agent will "
                     "not move this row; the records are somewhere else")


def explain_skip(detail: str, record_count: int) -> str:
    """Re-word a SKIP on a tenant that holds nothing.

    "ask the agent something first" is the right next step on a stocked
    tenant and the wrong one on an empty tenant, where no amount of asking
    changes the row. Different problems, different fixes.
    """
    return EMPTY_TENANT_SKIP if record_count == 0 else detail


def check_s1_labs_interpreted(cur, tenant: str) -> tuple[str, str]:
    """S1: the most recent labs $interpret actually interpreted something."""
    row = _one(cur, """
        SELECT detail, recorded FROM audit_events
        WHERE tenant_id = %s AND detail LIKE 'labs $interpret;%%'
        ORDER BY recorded DESC LIMIT 1
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
    """S5: recent runs complete, and failures carry an honest class.

    Throttling is reported but does not fail the row (ruling Q3,
    docs/2026-08-04-shakeout-rulings.md). Fail-fast-and-honest was chosen
    over parking a run, and the price of that choice is that throttling must
    stay VISIBLE — otherwise "the provider throttles us occasionally" and
    "the provider is throttling us constantly" look identical from here, and
    the ruling's revisit condition could never be observed.
    """
    cur.execute("""
        SELECT status, coalesce(error_class, ''), count(*)
        FROM agent_runs
        WHERE tenant_id = %s AND created_at > now() - make_interval(hours => %s)
        GROUP BY status, error_class
    """, (tenant, RUN_HEALTH_WINDOW_HOURS))
    rows = cur.fetchall()
    if not rows:
        return "SKIP", (f"no agent runs in the last {RUN_HEALTH_WINDOW_HOURS}h "
                        "— ask the agent something first")
    total = sum(r[2] for r in rows)
    defects = {r[1]: r[2] for r in rows if r[1] in DEFECT_ERROR_CLASSES}
    completed = sum(r[2] for r in rows if r[0] == "completed")
    throttled = sum(r[2] for r in rows if r[1] == "LLMRateLimited")
    note = ""
    if throttled:
        share = throttled * 100 // max(total, 1)
        note = (f"; {throttled}/{total} ({share}%) rate-limited — reported, "
                "not a defect. Revisit Q3 if this share is material")
    if defects:
        return "FAIL", (f"{sum(defects.values())}/{total} runs failed with a "
                        f"defect-class error: {defects} — read the worker log "
                        f"before assuming a provider blip{note}")
    return "PASS", (f"{completed}/{total} runs in {RUN_HEALTH_WINDOW_HOURS}h "
                    f"completed; no defect-class failures{note}")


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

# The rows that can only go green because the agent DID something. `ingest not
# stranded` is not one of them: it returns PASS on a tenant with nothing in it
# at all, because "no stranded jobs" is trivially true of no jobs.
#
# That distinction is load-bearing for #378. The report was "every behavioural
# row was SKIP", and counting the vacuous PASS as an evaluated row would make
# a run on the wrong tenant exit 0 with "all evaluable rows pass (1 checked)" —
# i.e. the scorecard would report success for a run that measured nothing about
# the agent, and the "look over here instead" hint would be unreachable code.
BEHAVIOURAL_CHECKS = frozenset({
    "check_s1_labs_interpreted", "check_s3_medication_reads",
    "check_s5_run_health", "check_s8_tool_rounds",
})

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
    ap.add_argument("--tenant",
                    help="tenant id to score (an opaque id, never PHI)")
    ap.add_argument("--list-tenants", action="store_true",
                    help="list tenants with any activity, newest first, "
                         "then exit (ids and counts only)")
    args = ap.parse_args()
    if not args.list_tenants and not args.tenant:
        ap.error("--tenant is required (or --list-tenants to find one)")

    uri = os.environ.get("SQLALCHEMY_DATABASE_URI", "")
    if not uri:
        print(f"{R}no SQLALCHEMY_DATABASE_URI — run inside the container "
              f"(railway ssh) or export a database url{X}")
        return 2
    host = urllib.parse.urlsplit(
        uri.replace("postgresql+psycopg2://", "postgresql://", 1)).hostname

    conn = _connect(uri)
    cur = conn.cursor()

    if args.list_tenants:
        rows = discover_tenants(cur)
        cur.close()
        conn.close()
        print(f"{D}tenants on {host} — ids, counts and timestamps only{X}\n")
        print(format_tenant_table(rows))
        print()
        if not rows:
            print(f"{Y}{recent_activity_hint(rows)}{X}")
            return 2
        print(f"{D}score one with: --tenant <id>{X}")
        return 0

    print(f"{D}scoring tenant {args.tenant} against {host}{X}\n")

    # Counted up front: it is what tells a SKIP meaning "nobody has asked yet"
    # apart from a SKIP meaning "there is nothing here to ask about".
    try:
        records = tenant_record_count(cur, args.tenant)
    except Exception:  # noqa: BLE001 — never let discovery break the card
        records = -1

    failed = evaluated = behavioural = 0
    for name, check in CHECKS:
        try:
            verdict, detail = check(cur, args.tenant)
        except Exception as exc:  # noqa: BLE001 — report, don't crash the card
            verdict, detail = "FAIL", f"check crashed: {type(exc).__name__}"
        if verdict == "SKIP" and records >= 0:
            detail = explain_skip(detail, records)
        color = {"PASS": G, "FAIL": R, "SKIP": Y}[verdict]
        print(f"  {color}{verdict:4}{X}  {name}: {detail}")
        if verdict != "SKIP":
            evaluated += 1
            if check.__name__ in BEHAVIOURAL_CHECKS:
                behavioural += 1
        if verdict == "FAIL":
            failed += 1

    hint = ""
    if behavioural == 0:
        # Say where the activity IS. Leaving the reader to guess between
        # "unexercised deploy" and "wrong tenant" is the #378 defect.
        try:
            hint = recent_activity_hint(discover_tenants(cur))
        except Exception:  # noqa: BLE001
            hint = "tenants with recent activity: could not query"

    cur.close()
    conn.close()

    print()
    print(MANUAL_ROWS)
    if failed:
        print(f"{R}{failed} row(s) regressed{X}")
        return 1
    if behavioural == 0:
        held = records if records >= 0 else "?"
        print(f"{Y}no behavioural row was evaluable on tenant {args.tenant} "
              f"({held} stored record(s)) — this run measured nothing about "
              f"the agent{X}")
        print(f"{Y}{hint}{X}")
        return 2
    print(f"{G}all evaluable rows pass ({evaluated} checked){X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
