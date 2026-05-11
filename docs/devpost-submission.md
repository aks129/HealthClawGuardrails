# HealthClaw Guardrails

**SHARP-on-MCP + PromptOpinion-compatible compliance layer for AI agents accessing FHIR data**

---

## Links

| | |
|---|---|
| Marketplace — Agent | https://app.promptopinion.ai/marketplace/agent/019e183d-c819-733f-9a91-6cf6756d4bed |
| Marketplace — MCP Superpower | https://app.promptopinion.ai/marketplace/mcp/019e1831-3d6f-7f72-99c2-cb8f2efab57e |
| Demo video (< 3 min) | https://youtu.be/2fVL28CW9p8 |
| Source code | https://github.com/aks129/HealthClawGuardrails |
| Live deployment | https://healthclaw.io · https://app.healthclaw.io · https://mcp-server-production-5112.up.railway.app/mcp |

---

## Problem

Every health system in the country is running AI experiments. Almost none of them have agents touching production charts. The blocker isn't capability — it's compliance. The moment a model sees a name, an MRN, or a date of birth, that conversation is governed by HIPAA, every state's analog, and the organization's BAA stack. So projects stall at "we can't let the agent touch real data."

The status quo for compliant agent access is one of three bad options:

1. **Build per-EHR**: re-implement guardrails in every Epic, Cerner, MEDITECH, athenahealth, eClinicalWorks deployment.
2. **Anonymize upstream**: ship a one-way de-identification pipeline that destroys the round-trip — fine for analytics, broken for agentic workflows that need to read *and* write back.
3. **Trust the model**: include "do not output PHI" in the system prompt and hope for the best. This is the current default. It is not auditable.

HealthClaw makes the right thing the default.

## What it is

HealthClaw is an **MCP server** (Superpower) plus an **A2A agent** that puts a compliance layer between any AI agent and any SMART-on-FHIR endpoint. The agent host obtains a SMART access token; HealthClaw forwards it on every call via `X-FHIR-Server-URL` / `X-FHIR-Access-Token` / `X-Patient-ID` headers, routes the request to the correct upstream EHR, applies the full guardrail stack on the response, and only then returns data to the model.

The same deployment works against Epic, Cerner, MEDITECH, athenahealth, eClinicalWorks, HAPI, SMART Health IT — no per-EHR code, no per-customer rebuild. That portability comes from compliance with two open specs:

- **SHARP-on-MCP** (https://sharponmcp.com) — vendor-neutral header-forwarding contract advertised under `capabilities.experimental.{fhir_context_required, sharp}`
- **PromptOpinion FHIR Extension** — advertised under `capabilities.extensions["ai.promptopinion/fhir-context"]` with a SMART-on-FHIR scope manifest (`patient/*.read` required, `patient/*.write` and `offline_access` optional)

Both declare the same headers and the same scope model, so a single MCP server satisfies both ecosystems.

## What every response gets

**On every read:**
- PHI redaction (names → initials, identifiers masked, addresses stripped, birth dates truncated to year, photos removed)
- Immutable `AuditEvent` appended to a tenant-scoped, append-only trail
- Medical disclaimer injected on clinical resources
- Upstream URLs rewritten so the source EHR never leaks into the response

**On every write:**
- HMAC-SHA256 signed step-up tokens with 128-bit nonce and 5-minute TTL
- Human-in-the-loop gate on clinical resources (HTTP 428 until `X-Human-Confirmed`)
- Local `$validate` runs before commit
- `ETag` / `If-Match` concurrency control

**Always:**
- Tenant isolation enforced at the database layer in local mode, propagated as a guardrail header in proxy mode
- OAuth 2.1 + PKCE (S256), dynamic client registration, token revocation

## Tool catalog

14 tools published to the marketplace, organized into three tiers:

| Tier | Tools | Step-up needed |
|---|---|---|
| Read | `context_get`, `fhir_read`, `fhir_search`, `fhir_validate`, `fhir_stats`, `fhir_lastn`, `fhir_permission_evaluate`, `fhir_subscription_topics`, `curatr_evaluate` | No |
| Write | `fhir_propose_write`, `fhir_commit_write`, `curatr_apply_fix` | Yes (HMAC token) |
| Utility | `fhir_get_token`, `fhir_seed` | n/a |

Coverage spans FHIR R4 US Core v9 stable resources (AllergyIntolerance, Immunization, MedicationRequest, Procedure, DiagnosticReport, Coverage, ServiceRequest, Goal, CarePlan, Patient, Encounter, Observation, Condition, …) and FHIR R6 ballot3 experimental resources (Permission, SubscriptionTopic, DeviceAlert, NutritionIntake).

The **Curatr** quality engine flags US Core data-quality issues inline — smoking-status contradictions between notes and structured fields, H-flag titers without interpretation, missing lab results, missing required US Core fields — each issue scored and paired with a guarded `apply_fix` path.

## The AI factor

Three places where generative AI does what conditional logic can't:

1. **Tool selection under uncertainty.** A clinical question doesn't map cleanly to one endpoint. "What's this patient's recent diabetes control look like?" turns into `fhir_search`(Condition, code=diabetes) → `fhir_lastn`(Observation, code=HbA1c) → `fhir_stats`(Observation, code=glucose) → narrative synthesis. The agent does the planning; HealthClaw does the policy.

2. **Curatr semantic quality checks.** "Smoking status = current smoker" plus a 2024 note saying "patient denies tobacco use" is a contradiction no validator catches with conformance rules. The agent reads both, flags the inconsistency, and (with step-up + HITL) proposes a Provenance-linked fix.

3. **Guardrail narration.** The demo agent doesn't just retrieve data; it points out *what HealthClaw did* to each response — "the patient's name has been truncated to initials per HealthClaw's HIPAA Safe Harbor de-identification" — making the compliance layer visible to clinical reviewers in a way pure log lines can't.

## Potential impact

PHI exposure is THE blocker for clinical AI deployment in 2026. Every CIO survey says it; every pilot that doesn't ship cites it. HealthClaw is a deployable architectural pattern that converts the blocker into a configuration choice. Once a health system trusts the redaction + audit + HITL guarantees, the conversation moves from "can we let the agent see this?" to "which tools should we enable?"

Because the server is SHARP-on-MCP + PromptOpinion compliant rather than EHR-specific, a single HealthClaw deployment can sit in front of an entire health system's SMART-launched agent ecosystem. That's a 50× reduction in per-vendor compliance work versus the per-EHR alternative.

The pattern is also reusable beyond healthcare. The same shape — agent host forwards an access token, server applies policy on response, audit trail emitted — applies to any regulated domain with similar boundary requirements (financial records under GLBA, education records under FERPA, government records under CUI handling).

## Feasibility

This is not a slide-deck submission. The full stack is deployed today:

- **Flask app** on Railway at `app.healthclaw.io` — FHIR REST facade, OAuth 2.1, audit, redaction, Curatr
- **Node.js MCP server** on Railway at `mcp-server-production-5112.up.railway.app` — streamable HTTP, SSE, JSON-RPC bridge; SHARP + PromptOpinion capabilities advertised in `initialize`
- **Telegram bot stack** (OpenClaw) for conversational personas — Sally-PCP, Mary-pharmacy, Dom-fitness, Kristy-scheduler — each with their own persona prompt calling shared slash commands
- **Vercel front-end** at `healthclaw.io` for marketing, skills catalogue, and a quickstart PDF

**Test coverage**: 516 Python tests + 49 Node tests, all passing. TypeScript strict-mode `tsc --noEmit` clean. End-to-end gate script (`scripts/demo_e2e.sh`) covers 10 compliance gates: liveness → seed → read-with-redaction → audit trail → cross-tenant isolation → Curatr evaluate → human-in-the-loop.

**Compliance posture**:
- HIPAA Safe Harbor de-identification on by default; patient-controlled mode preserves selected fields
- SOC2-aligned audit trail with database-level immutability
- HITRUST-aligned tenant isolation
- `.claude/compliance/{hipaa,soc2,hitrust}.md` gate checklists committed to the repo

**Demo data is synthetic**. The `desktop-demo` tenant is seeded with a Grover Keeling sample record on first boot. No real PHI was used in any test, screenshot, or video.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Agent Host (PromptOpinion, SMART launcher, ...)│
│  Obtains SMART-on-FHIR access token              │
└──────────────────┬──────────────────────────────┘
                   │ X-FHIR-Server-URL
                   │ X-FHIR-Access-Token
                   │ X-Patient-ID
                   ▼
┌─────────────────────────────────────────────────┐
│  MCP Server (Node.js + TypeScript)               │
│  /mcp — Streamable HTTP (primary)                │
│  /sse + /messages — Legacy SSE                   │
│  /mcp/rpc — JSON-RPC HTTP bridge                 │
│  Advertises SHARP + PromptOpinion FHIR ext       │
└──────────────────┬──────────────────────────────┘
                   │ Headers forwarded
                   ▼
┌─────────────────────────────────────────────────┐
│  Flask Guardrail Layer (Python)                  │
│  - SHARP per-request proxy creation              │
│  - PHI redaction on all read paths               │
│  - Immutable AuditEvent emission                 │
│  - Step-up token verification                    │
│  - Human-in-the-loop gate (clinical writes)      │
│  - Tenant isolation + URL rewriting              │
│  - Curatr quality engine                         │
└──────────────────┬──────────────────────────────┘
                   │ Per-request upstream
                   ▼
┌─────────────────────────────────────────────────┐
│  Upstream FHIR Server                            │
│  Epic · Cerner · MEDITECH · athenahealth        │
│  eClinicalWorks · HAPI · SMART Health IT · ...  │
└─────────────────────────────────────────────────┘
```

## Tech stack

- **Backend**: Python 3.11+ (Flask, SQLAlchemy, httpx, marshmallow, gunicorn), Node.js 20 + TypeScript (MCP SDK, Express, node-fetch)
- **Specs**: MCP 2024-11-05, SHARP-on-MCP 1.0, PromptOpinion FHIR Context extension, SMART-on-FHIR, FHIR R4 US Core v9, FHIR R6 ballot3, OAuth 2.1, PKCE S256
- **Infrastructure**: Railway (Flask + MCP + Redis), Vercel (marketing site), GitHub Actions CI
- **Storage**: SQLite default (local mode), PostgreSQL on Railway (production), Redis (rate-limit + sessions + token cache)
- **Testing**: pytest (516 tests), Jest (49 tests), Playwright (browser e2e)

## What's next

- Strict StructureDefinition + binding validation (currently structural only)
- SubscriptionTopic notification dispatch (currently storage + discovery)
- Cryptographic human-in-the-loop confirmation (currently header-based)
- Cross-version translation for R5/R6 upstreams (currently pass-through)
- Provider Directory de-duplication on the upstream proxy path

## Repository

Open source under the same repository as this submission.

`github.com/aks129/HealthClawGuardrails` · `healthclaw.io`

A project of **fhiriq**.
