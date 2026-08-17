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
(`docs/evidence/2026-08-16-set2-connectors.md`):

| Kind | Auth | Proven live against |
|---|---|---|
| `hapi` | Basic / anonymous | **HAPI FHIR 8.11.16** ✓ |
| `generic` | Basic / anonymous | **Firely Server 6.9.1** ✓ |
| `aidbox` | HTTP Basic | **not run** — local server down |
| `medplum` | OAuth2 client-credentials | **not run** — local server down |

## Architecture ratchets

Eight numbers that may only go down. `tests/test_ratchets.py` holds them.

| Ratchet | 05 Aug | Today | 2.0 |
|---|---|---|---|
| Raw `X-Tenant-Id` reads outside the kernel | 55 | **5** | 0 |
| Step-up call sites outside the kernel | 13 | **12** | 0 |
| Post-commit audit call sites | 88 | **89** | 0 |
| Files querying without `is_deleted` | 21 | **10** | 0 |
| Modules importing into `r6/routes.py` | 7 | **4** | 0 |
| `r6/routes.py` lines | — | **3,924** | shrink only |

## The two numbers that explain why this document exists

**50,125 lines of test code guard 29,446 lines of engine — 1.7 to 1.**
`3,151` tests pass on `main`.

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
