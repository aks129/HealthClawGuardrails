# Real Actions + Reliability — Design

**Date:** 2026-07-11
**Status:** Approved by Gene 2026-07-11
**Deadline:** Feature freeze 2026-08-11; HIMSS Keystone webinar 2026-08-18
**Goal:** Turn HealthClaw Guardrails from a guardrailed *reader* with simulated actions into a reliable *real system*: an AI agent that safely reads a patient's real health data AND takes real actions on their behalf — phone calls, SMS, form completion, appointment booking — every action behind the existing human-in-the-loop gate.
**Benchmark:** the adoption mechanics of successful community-driven open-source systems (Medplum, OpenClaw, Hermes): crisp public extension points, docs-as-product, quickstart-in-minutes, every capability shipped with a runnable example.

## Why

Audit findings (2026-07-11, two independent code audits):

- The guardrail layer (propose→commit, HTTP 428, step-up tokens, atomic claim, signed webhooks, audit trail) is genuinely solid.
- The action layer is dark: Bland.ai calling and Twilio SMS are coded but have no credentials, so every "call"/"text" silently returns `simulated: true` and is marked `completed`. `form-fill` is a registered action kind with **no executor**. Appointment booking **does not exist**.
- Seven silent-failure config landmines can kill demo segments with no visible error (unset `FASTEN_WEBHOOK_SECRET`, Fasten's off-by-default `patient.connection_success` event, unset `INTERNAL_TOKEN_MINT_SECRET`, no MCP fetch timeouts, zombie ingest jobs after restart, Telegram poller conflicts, `STEP_UP_SECRET` drift).
- Latent Postgres bugs remain: `R6Resource` primary key is the raw FHIR id **globally** (cross-tenant id collision silently drops resources on ingest); `FastenConnection.org_connection_id` and `FastenJob.task_id` are still `String(64)`; the whole Python test suite runs on SQLite, which hides every width/PK bug.

The product conclusion: the safety layer is real; the "power" layer is theater in the current deployment. This design lights the power layer for real and removes the silent fragility.

## Scope decisions (locked)

1. **All four actions become provably real** — actual side effects in the world, verifiable by a third party:
   - Phone calls: Bland.ai voice agent places real calls.
   - SMS: Twilio sends real texts.
   - Form completion: agent populates a standard intake questionnaire from the patient's FHIR data and produces a completed PDF, delivered via encrypted SMART Health Link (+ optional SMS link). No portal automation in v1.
   - Appointment booking: voice-call booking — the voice agent calls the clinic, negotiates a slot from the patient's stated preferences, and the confirmed result becomes a FHIR `Appointment` after human confirmation. No FHIR `$book` in v1.
2. **Everything lands by August 18**, with feature freeze **August 11**. Any rail not solid by freeze is demoed as an honestly-labeled gated proposal; the freeze does not move.
3. **Structure: Approach A — one action rail, foundation first**, with maximum parallel subagent execution and a build→test→iterate loop on every workstream. We are building a reliable real system, not a demo.
4. **Adoption engineering is in scope.** Docs, quickstart, and runnable examples are engineering deliverables, not marketing: every rail's definition of done includes documentation and an example. Promotion/outreach remains out of scope.
5. **Provider onboarding starts immediately (W0 task zero, before code).** Twilio US A2P 10DLC campaign registration (or toll-free verification) takes days-to-weeks of carrier review; Bland.ai needs an account, phone number, and webhook setup. These external approval clocks are the true critical path for Aug 18 and start the weekend of Jul 12-13.

## Architecture

### One action rail

The `ProposedAction` engine (`r6/actions/`) is the spine. Every capability is a **builder** (assembles the proposal payload) plus an **executor** (performs the real side effect on commit):

| Kind | Builder | Executor | v1 side effect |
|---|---|---|---|
| `phone-call` | exists (incl. `rx_transfer`) | `_execute_call` (exists, harden) | Real Bland.ai call to an allowlisted number |
| `sms` | exists | `_execute_sms` (exists, harden) | Real Twilio SMS to an allowlisted number |
| `form-fill` | new: intake builder | new: PDF renderer + SHL delivery | Completed intake PDF, shareable via encrypted link |
| `book-appointment` | new: prefs → call script | new: call-executor variant with structured extraction | Real booking call; confirmed slot → proposed FHIR `Appointment` |

Uniform lifecycle, unchanged from today's (working) gate:
`agent proposes → HTTP 428 until human confirms (X-Human-Confirmed) + step-up token → atomic single-UPDATE claim (proposed→executing) → executor runs → outcome resolved synchronously or by HMAC-signed provider webhook → AuditEvent + Telegram push`.

The MCP server stays a thin forwarder; no action logic moves into Node.

### Pinned contracts (parallel agents build against these; they land as PR #1 with tests)

**Action state machine:** `proposed → executing → completed | failed | needs_review | expired`. `needs_review` is a NEW terminal-ish state (resumable to a follow-up proposal): the action ran but its outcome could not be confirmed (e.g., ambiguous booking-call extraction); it always carries the evidence (transcript/provider payload reference). `simulated` is a boolean flag on the record, never a state, and only settable via the explicit dev flag.

**Executor contract (public, versioned — this is the community extension point):**
```python
class ActionExecutor(Protocol):
    kind: str                     # e.g. "phone-call"
    def validate(self, payload: dict) -> list[str]:      # payload errors, [] if ok
    def execute(self, action: ProposedAction) -> ExecutionResult
# ExecutionResult: {status: executing|completed|failed|needs_review,
#                   provider_ref: str|None, outcome: dict, error: str|None}
```
Async resolution (provider webhooks) transitions `executing → completed|failed|needs_review` through the existing signed-callback path. Error taxonomy at the gate: `provider_not_configured`, `number_not_allowlisted`, `daily_cap_reached`, `payload_invalid`, `provider_error`, `extraction_ambiguous`.

**Preflight check contract:** each check returns `{name, ok, detail, fatal}`; the endpoint aggregates to `{ok, checks[]}`; a `fatal` failure turns the dashboard card red.

### The rail as a public extension point (community-driven design)

The executor registry and the conformance-probe registry become the project's plugin API — the same move as Medplum bots and OpenClaw personas. Deliverables: a versioned `ActionExecutor` interface doc with a cookbook chapter ("add a real-world capability behind the 428 gate in ~50 lines"), a worked toy example (e.g. a webhook-poster executor), and 3-5 seeded "build an executor" good-first-issues before the webinar, so the webinar's community-feedback ask lands on a concrete contribution surface.

### Safety hardening that real actions require (non-negotiable)

- **Contact allowlist:** v1 only calls/texts numbers registered to the tenant as "mine" or "my providers" (new `TenantContact` table; managed via dashboard + a guarded MCP tool). Commit of an action targeting an unlisted number is refused with a clear error, and the refusal is audited.
- **Rate/cost caps:** per-tenant daily caps on calls and SMS (config, sane defaults). Cap breach refuses commit, audited.
- **No silent simulation, ever:** if provider credentials are missing, `commit` fails loudly with `provider_not_configured` — it never returns a fake success. Simulation remains available only as an explicit `?simulate=true` dev flag that marks the action record `simulated` and is visually distinct everywhere it is shown.
- Existing Schedule-II keyword denial for rx transfers stays.

## Workstreams

### W0 — Foundation (weeks 1–2; many tasks parallelizable immediately)

0. **Provider onboarding (task zero, starts before code, weekend of Jul 12-13):** Twilio account + A2P 10DLC/toll-free registration submitted; Bland.ai account + phone number + KYC; `PUBLIC_BASE_URL` webhook endpoints registered with both; balances funded. Tracked as a calendar item, not a code task — carrier review latency is outside our control and everything in W1/W3 queues behind it.
0b. **De-risk spikes (week 1, half-day each):** (a) one real Bland.ai call to Gene's own number exercising post-call structured extraction; (b) one real Twilio send (or toll-free test route if 10DLC still pending). Purpose: learn real provider failure modes while course changes are still cheap. Findings feed the W1/W3 designs.

1. **Config preflight** — `GET /r6/ops/preflight`: one green/red JSON covering every demo-critical dependency: `FASTEN_WEBHOOK_SECRET` set, `INTERNAL_TOKEN_MINT_SECRET` set + accepted by Flask, `STEP_UP_SECRET` set and not the compose default (consistency verified via an HMAC challenge, never by exposing the secret), Bland/Twilio credentials present + validated with a cheap provider ping, `ACTIONS_WEBHOOK_SECRET` + `PUBLIC_BASE_URL` set, DB engine + column-width assertions, Fasten `patient.connection_success` verified-connection health, Telegram poller singleton status. Dashboard card renders it. This endpoint is the pre-demo ritual and the nightly monitor's probe.
2. **Resource identity migration** — `R6Resource` uniqueness becomes `(tenant_id, id)`; ingest lookup switches from global-PK `db.session.get` to tenant-scoped query. Kills the cross-tenant silent-drop bug. **Rollout is expand-and-contract on live prod (real tenants: Gene, Gigi):** (1) prod snapshot; (2) add composite unique constraint alongside the existing PK; (3) backfill-verify no violations; (4) switch all lookups to tenant-scoped; (5) retire global-PK assumptions. Rollback = revert lookup commit + drop constraint; rehearsed on a prod-copy first.
3. **Column widths** — widen `FastenConnection.org_connection_id`, `FastenJob.task_id`, `tenant_id`, `platform_type` (and any `String(64)` that carries external ids) to 255; extend `schema_sync` + the static width-assertion test to cover them.
4. **Boot reaper** — on startup, non-terminal Fasten jobs re-trigger `trigger_ehi_export` for fresh signed URLs (never replay stale ones); job state surfaces on the dashboard.
5. **MCP fetch timeouts** — `AbortSignal.timeout(15s)` (60s for `guardrail_conformance`) on all 33 tools, returning structured `{error, retryable}` instead of hanging.
6. **Poller singleton hardening** — bot detects mid-run 409 Conflict storms (not just at startup) and exits loudly with a Telegram alert to the admin.
7. **Postgres CI** — a GitHub Actions job runs the ingest/width/identity/actions test subsets against real Postgres. The "SQLite hid it" class dies here.

### W1 — Comms rail (week 2)

Bland.ai + Twilio credentials live on Railway. Executor hardening: provider-call timeouts + bounded retry, provider errors surfaced into the action record (never swallowed), call transcript/recording reference captured PHI-safely, webhook callback path load-tested. Allowlist + caps enforced at commit. Contract tests use recorded provider fixtures; one live smoke test per provider against Gene's own number.

**Action observability (rides with W1):** dashboard panel listing recent actions (kind, state, provider outcome, timestamps) assembled from the existing action records + Telegram push; a daily Telegram digest (actions executed / failed / needs_review) so a 2am failure is known by breakfast, not on stage.

### W2 — Forms rail (week 3)

`form-fill` executor: SDC `$populate` (existing) fills a FHIR Questionnaire from the tenant's record → completed `QuestionnaireResponse` → rendered to a clean PDF (HTML template → headless-Chromium print, matching the toolchain already used in CI) → stored → delivered via encrypted SMART Health Link (existing) + optional SMS link through the comms rail. Ships with two real questionnaires with population mappings: new-patient intake and medical history. Human confirms (428) before generation + delivery; the rendered PDF is previewable at propose time.

### W3 — Booking rail (week 4)

`book-appointment`: builder takes `{provider contact (allowlisted), reason, time windows, patient constraints}` → generates the call script; executor places the Bland.ai call with post-call **structured extraction** → `{confirmed: bool, datetime, notes, transcript_ref}`. On `confirmed`, the system proposes a FHIR `Appointment` write (standard propose→428→commit); on ambiguous extraction it resolves to `needs_review` with the transcript — it never fabricates a confirmation. Depends on W1's hardened call path and the week-1 extraction spike. **Validation ladder: controlled target first (a test line we answer), then Gene's real providers; real-clinic reliability is a stretch goal, not the acceptance bar.** The AI caller identifies itself as an AI assistant calling on behalf of the patient (disclosure is both etiquette and, in several states, law).

### Definition of done for every rail (W1-W3)

Code + tests (unit, contract, Postgres CI) + **docs page** (what it does, guardrails that bind it, config) + **runnable example** (script or MCP conversation transcript) + preflight checks for its dependencies + entry in the actions dashboard. A rail without docs and an example is not done.

### W4 — Continuous verification (throughout)

- E2E test that drives the **currently-untested full path**: Fasten webhook → daemon thread → ingest → agent read → propose → 428 → confirm → commit, against the docker-compose stack in CI.
- **Nightly cron against prod**: preflight + `$conformance`; any regression pushes a Telegram alert. Drift is caught the night it happens, not on stage.
- Every executor developed TDD; every workstream runs build→test→iterate in its own worktree; integration is continuous (small merges), not big-bang.

### W5 — Freeze + rehearsal (Aug 11–17)

Feature freeze Aug 11. Daily full run-throughs on prod (the exact webinar choreography, using Gene's real data + allowlisted numbers). Only rehearsal-surfaced fixes land. Runbook written from the rehearsals; preflight green is a hard precondition to going on stage.

## Error handling principles

- Fail loud and specific at the gate (`provider_not_configured`, `number_not_allowlisted`, `daily_cap_reached`, `extraction_ambiguous`) — never a fake success, never a silent skip.
- Every failure path writes the same audit trail as success paths.
- Degradation ladder for the webinar: real action → honestly-labeled gated proposal → skip segment. Decided at freeze, not improvised on stage.

## Out of scope (v1)

Portal/browser form automation; FHIR `Slot/$book`; electronic Rx transmission (NCPDP/Surescripts); outbound email actions; calling numbers outside the tenant allowlist; growth/marketing work (rides on this, specified separately).

## Success criteria

1. **A stranger runs the full stack locally in under 15 minutes** (`docker compose up` + documented quickstart) and can propose→confirm→execute a (simulated-flagged) action against it.
2. `preflight` green on prod, and it stays green nightly through Aug 18.
3. Each of the four actions performed for real, end-to-end, behind the 428 gate, on Gene's own tenant with his real data — repeatably, on three consecutive rehearsal days.
4. Zero known silent-failure paths: every audit finding either fixed or explicitly accepted in writing.
5. Postgres CI green including identity-migration and width tests.
6. The executor extension point is public: versioned interface doc + cookbook example + seeded good-first-issues live before Aug 18.
7. A stranger following the runbook can execute the full demo without tribal knowledge.
