# System topology — what exists, measured

Ground truth for the PRDs in `docs/prd/` and the process in
`docs/2026-08-16-delivery-process.md`. Every number here was measured on
`main` on 2026-08-16, not quoted from an earlier document.

It is deliberately one page. A topology nobody can hold in their head is the
problem it was written to solve.

## The shape

```
  SURFACES            ENGINE                        STORES / UPSTREAMS
  ────────            ──────                        ──────────────────
  MCP server  ─┐                                 ┌─ local SQLite / Postgres
  (TS, 7.9k)   │                                 │   (R6Resource, AuditEvent,
               ├──▶  r6/  Flask, 29.4k LOC  ─────┤    ProposedAction, …)
  CareAgents  ─┤     11 blueprints               │
  (Py, 5.3k)   │     ┌─────────────────────┐     ├─ Aidbox    (HTTP Basic)
               │     │ access kernel       │     ├─ Medplum   (OAuth2 CC)
  Web / MCP   ─┤     │ redaction           │     ├─ HAPI      (Basic/none)
  Apps         │     │ audit               │     └─ generic   (Basic/none)
               │     │ step-up + human gate│
  Telegram    ─┘     │ tenant isolation    │        + SHARP per-request
  (openclaw,         └─────────────────────┘          (caller-supplied token)
   1.3k)
```

## Components, and what state each is really in

| Component | Size | State |
|---|---|---|
| `r6/` — the guardrail engine | 29,446 LOC, 11 blueprints | Conformance **Grade A 7/7 local**, measured today. Proxy mode **not measured** since 2026-08-16 morning. |
| `r6/routes.py` — the god module | **3,924 lines, 39 routes** | Ratcheted; shrinking. Decomposition (#56) has not started. |
| `r6/access.py` — the access kernel | one module | Adopted by 9 modules. `require_grant` + `has_grant` (#506). |
| `careagents/` — consumer app | 5,311 LOC | On Railway + Postgres. **Deployed build is stale** (#427). Stores no PHI. |
| `services/agent-orchestrator` — MCP server | 7,899 LOC (TS) | Token-locked in production, non-negotiable. Unreachable from hosted connectors (#290). |
| `services/shl-server` | — | Smart Health Links. |
| `openclaw/` — Telegram bot | 1,269 LOC | **Sunset candidate** once MCP parity is confirmed. |
| `hermes/` | README + SOUL.md + install.sh + mcp.json, **no code** | A *reference* for standards-based integration (SKILL.md + native MCP), not a component. Named here because the topology previously implied otherwise. |
| `e2e/` — Playwright | 6 specs | Landing, dashboard, careagents, demo-tenant walkthrough, api, design invariants. |
| `examples/aidbox-…/qa/` | 1 spec | The assertion-driven recording pattern. |

## The four upstream connectors, and which are proven

Measured 2026-08-16 by the set-2 evidence run
(`docs/evidence/2026-08-16-set2-connectors.md`), and **re-run on 2026-09-04 by
someone other than its author** (`docs/evidence/2026-09-04-set2-connectors-rerun.md`,
#530). The two `yes` rows reproduced step for step.

| Kind | Auth | Proven live against |
|---|---|---|
| `hapi` | Basic / anonymous | **HAPI FHIR 8.11.16** ✓ twice |
| `generic` | Basic / anonymous | **Firely Server 6.9.1** ✓ twice |
| `aidbox` | HTTP Basic | **not run** — local server down, both days |
| `medplum` | OAuth2 client-credentials | **not run** — local server down, both days |

Anyone can now re-run the two: `scripts/walkthrough-upstream.sh hapi|generic`,
transcripts in `docs/evidence/2026-09-04-set2-rerun/`. Until 2026-09-04 the
claim rested on an uncommitted script and could not be checked by anyone but
its author, which is what #530 was.

## Architecture ratchets

Six numbers that may only go down. `tests/test_ratchets.py` holds them, and
the table below is all of them.

The 2.0 playbook sets out eight ratchets, and the two sets are not the same
six. Five of the six pins below are playbook ratchets; the playbook's other
three have no pin at all, and `r6/routes.py` lines is a sixth pin the playbook
does not list. Saying "eight" here read as *eight numbers a test enforces*,
which was never true of either set.

| Ratchet | 05 Aug | Today | 2.0 |
|---|---|---|---|
| Raw `X-Tenant-Id` reads outside the kernel | 55 | **5** | 0 |
| Step-up call sites outside the kernel | 13 | **12** | 0 |
| Post-commit audit call sites | 88 | **89** | 0 |
| Files querying without `is_deleted` | 21 | **10** | 0 |
| Modules importing into `r6/routes.py` | 7 | **4** | 0 |
| `r6/routes.py` lines | — | **3,924** | shrink only |

## The two numbers that explain why this document exists

**50,429 lines of test code guard 29,508 lines of engine — 1.7 to 1.**
**3,153** tests pass, 13 skip, 1 xfails.

Measured at `4cb3771`, which is this document's branch point, with
`git ls-files 'r6/**/*.py' 'r6/*.py' | xargs wc -l` and the same over
`tests/*.py`. The first published version of this block carried 50,125 /
29,446 / 3,151 — figures exact at `4ce28c3`, four merges earlier, under a
header claiming they were measured on `main`. Worse, `3,151` was the
*collected* count reported as a *passing* count. Corrected here, with the
commit and the command stated so the next reader can re-run rather than
trust.

**All eight defects found on 2026-08-16 were found by running the system.
Zero were found by that suite.** Three of the eight had passing tests sitting
directly over them, and one of those tests asserted the defect as a
specification.

That is not an argument for fewer tests. It is the measurement that says unit
coverage and working software are different claims, and only one of them is
currently being made.

## Deployments

| Where | What | Deploy trigger |
|---|---|---|
| Railway | `app.healthclaw.io` (Flask + site) | push to `main`, automatic |
| Railway | CareAgents | **manual `railway up`** from a staged directory — has drifted 13 commits before |
| Railway | MCP server | manual, needs explicit authorization |
| Vercel | demo-only copy (`api/index.py`, second Flask entry point) | **purpose unsettled** — an open owner decision |

## What this document does not cover

`skills/`, `knowledge/`, `adapters/`, `migrations/`, `scripts/` (10,998 LOC of
operator tooling), and `deploy/`. They are real and they are not feature sets;
they are named here so their absence below is a choice rather than an
oversight.
