# Real Actions + Reliability — Design (v2, post round-1 review board)

**Date:** 2026-07-11 (v2 same day, after 5-role review board round 1)
**Status:** Amended per review board; round 2 pending
**Deadline:** Feature freeze 2026-08-11; HIMSS Keystone webinar 2026-08-18
**Goal:** Turn HealthClaw Guardrails from a guardrailed *reader* with simulated actions into a reliable *real system*: an AI agent that safely reads a patient's real health data AND takes real actions on their behalf — phone calls, SMS, form completion, appointment booking (experimental) — every action behind a human-in-the-loop gate that is **provably out-of-band**, not self-attested.
**Benchmark:** adoption mechanics of successful community-driven open-source systems (Medplum, OpenClaw, Hermes) *adapted for zero distribution*: hand-recruited design partners over passive artifacts; one standards artifact in the form the FHIR community actually reviews (an IG); quickstart that ends in a real event on the user's own phone.

## Users of record (new in v2)

- **Beachhead (next 90 days): the developer** at a digital-health company or FHIR shop being asked to bolt an AI agent onto clinical data with no answer to "what stops it doing something bad." Their job-to-be-done: *gate my agent's writes and real-world actions behind human confirmation + audit in 15 minutes.* The webinar audience is this person.
- **Payload persona: the family caregiver** (the demo's protagonist and the system's long-run user). v1 does NOT ship a consumer onboarding; instead the spec names the consumer bar on the roadmap: *a non-developer connects a parent's records from her phone in under 20 minutes, self-serve* — and records what blocks it today (tenant provisioning, Telegram dependency, identity-verification-for-proxy, family/consent model).
- **Acceptance consequence:** at least 5 named external design partners (not Gene) each complete a real 428-gated action on their own tenant before Aug 18. Success is measured on strangers.

## Why

Two code audits (2026-07-11) plus a 5-role review board (product, clinician, patient/caregiver, FHIR/CMS, agentic architect) found:

- The guardrail *engine* is genuinely solid (atomic claim, expiring proposals, signed webhooks, fail-paths audited) — but **the human-confirmation gate is self-attested at the MCP layer**: the Node forwarder hardcodes `X-Human-Confirmed: true` and mints step-up tokens on demand, so the complete authorization chain for a real-world action contains zero humans. The audit trail would record human confirmation that never happened.
- The action layer is dark (no provider credentials; simulated success returned as `completed`), `form-fill` has no executor, booking does not exist.
- The **content layer is unspecified**: what the human sees before confirming, what the voice script says and to whom, how a form is reviewed. Three of five reviewers independently: the words are more load-bearing than the interfaces.
- The rail is **emergency-blind** while the repo already contains the fix (`r6/smbp/triage.py` red-flag doctrine, unused by actions).
- Seven silent-failure config landmines; no recovery after claim (worker death ⇒ action stuck `executing` forever); latent Postgres identity bugs; AuditEvent DCM coding factually wrong (110153 = "Source Role ID", a participant role, not an event type); zero agent evals.
- Nothing FHIR-shaped exists for the standards community to review (no IG, no profiles, no Connectathon footprint) — which is why the community is silent, not because the idea is bad.

## Scope decisions (locked, v2)

1. **Actions and their acceptance bars:**
   - **Phone calls (real):** Bland.ai voice agent places real calls to allowlisted, referenced contacts. Scripts follow the pinned call-script skeleton (below).
   - **SMS (real):** Twilio, allowlisted recipients. Positioned honestly: a notification/confirmation channel and the quickstart payoff — not a headline "action."
   - **Form completion (real, timeboxed):** SDC `$populate` → structured per-item review → completed PDF → SMART Health Link delivery. **SHL-only in v1** (SMS-link leg cut: cross-rail dependency on 10DLC for marginal value). One questionnaire at freeze-quality beats two at demo-quality.
   - **Appointment booking (experimental, labeled):** voice-call booking validated against a **controlled target line we answer — that is the declared acceptance bar**, not a hedge. Real clinics are a measured experiment (booking completion rate tracked), never claimed as a capability until the number says so. Webinar segment is designed around the controlled call and says "experimental" out loud.
2. **Everything lands by August 18**, feature freeze **August 11**. Degradation ladder decided at freeze: real action → honestly-labeled gated proposal → cut segment.
3. **Structure: one action rail, foundation first**, parallel subagent execution, build→test→iterate. PR #1 pins the contracts (below); rails never touch shared files (registry-as-plugin).
4. **Adoption engineering in scope; passive artifacts demoted.** Design partners and the BYO-key quickstart are deliverables; the cookbook chapter and seeded good-first-issues move to webinar week (Aug 12–17), written after three rails have used the contract.
5. **Provider onboarding started 2026-07-12 (task zero):** Twilio A2P 10DLC/toll-free registration, Bland.ai account + number + KYC, webhook URLs, funded balances. Carrier review is the uncontrollable critical path.
6. **Standards workstream added (W-IG):** a continuous-build FHIR IG is the single highest-leverage credibility move and runs parallel to the rails.

## Architecture

### One action rail

`ProposedAction` engine (`r6/actions/`) is the spine. Each capability = **builder** (assembles + validates payload at propose time) + **executor** (performs the side effect on commit). Builders that carry clinical content are server-side (the server assembles med lists / scripts from FHIR data — the model supplies references and preferences, never clinical facts or raw phone numbers).

| Kind | Builder | Executor | v1 side effect |
|---|---|---|---|
| `phone-call` | exists (incl. `rx_transfer`, redesigned) | `_execute_call` hardened | Real Bland.ai call, two-phase script, allowlisted referenced contact |
| `sms` | exists | `_execute_sms` hardened | Real Twilio SMS to allowlisted referenced contact |
| `form-fill` | new: intake builder + review flow | new: PDF at propose, SHL delivery at commit | Reviewed intake PDF via encrypted link |
| `book-appointment` | new: prefs → script (server-side) | call-executor variant + structured extraction | Real booking call (controlled target); confirmed slot → proposed FHIR `Appointment` |

### Pinned contracts (PR #1, with tests; rails build only against merged contracts)

**1. Action state machine (reconciles spec and code — code today has `unknown`/`confirmed`; this is canonical):**
`proposed → awaiting_confirmation → executing → completed | failed | needs_review | expired`, plus `unknown` (provider outcome unknowable after possible send; reaper-reconciled). `needs_review` always carries evidence (transcript/provider payload as DocumentReference). `simulated` is a flag, never a state, only via explicit dev flag, visually distinct everywhere. One canonical `transition_action(action_id, from_states, to_state, actor, **fields)` helper wraps the guarded-UPDATE pattern and writes the event log in the same transaction; the decorative ORM `transition()` method is deleted so parallel agents can't reintroduce TOCTOU.

**2. Out-of-band human confirmation (replaces the spoofable header):**
`propose` pushes an Approve/Reject inline-button message via the existing Telegram path (dashboard button as alternative). Approve writes a single-use `ActionConfirmation` row (action_id, nonce, approved_at, approved_via). `commit` requires and consumes it; step-up nonce is consumed (`consume_nonce=True`) for all real-world actions — single-use, not replayable within TTL. The Node forwarder forwards headers it *received* and never mints `X-Human-Confirmed` or tokens for action commits (line tools.ts:1839 deleted). MCP elicitation is NOT used (protocol pin + uneven client support). The `action_commit` tool result before approval states in words: "terminal for this turn; the patient must approve out-of-band; poll `action_status` or end turn" — phrasing verified by golden evals.

**3. Executor contract (public, versioned — the community extension point):**
```python
class ActionExecutor(Protocol):
    kind: str
    required_env: tuple[str, ...]          # preflight assembles its checks from these
    def validate(self, payload: dict) -> list[str]   # runs at PROPOSE time
    def execute(self, action: ProposedAction) -> ExecutionResult
    def reconcile(self, action: ProposedAction) -> ExecutionResult  # query provider truth
# ExecutionResult: {status, provider_ref, outcome, error}
```
Registered via `register_executor()` from per-rail modules (`r6/actions/rails/{sms,forms,booking}.py`); `VALID_KINDS` derived from the registry. Error taxonomy: `provider_not_configured`, `contact_not_allowlisted`, `daily_cap_reached`, `payload_invalid`, `provider_error`, `extraction_ambiguous`, `emergency_indicated`, `stale_source_data`.

**4. Confirmation payload contract (what the human SEES — per kind, mandatory):**
Every kind defines its confirm-card content. `phone-call`: contact name + relationship ("CVS on Elm — Mom's pharmacy") + number, **verbatim script**, explicit disclosure list ("this call will state: name, DOB, these 2 medications — nothing else"), voicemail policy, cost estimate. `sms`: recipient + full text. `form-fill`: the rendered PDF itself plus the structured review below. `book-appointment`: everything phone-call shows + requested windows + what gets recorded on confirmation.

**5. Call-script skeleton (pinned; more load-bearing than the Python):**
Phase 1 — verify before disclosing: AI disclosure + recording disclosure ("This is an AI assistant calling on behalf of [patient], with their recorded authorization; this call may be recorded"), confirm establishment and human ("Have I reached X? Am I speaking with staff?"); extraction failure here ends the call → `needs_review`. Phase 2 — minimum-necessary content: identity payload the receiving side actually needs (name, DOB, callback), then the request. Voicemail: callback request only, zero PHI. Refusal branch: end gracefully → `needs_review` with "requires patient call directly" (a useful outcome, not a hidden failure). Rx-transfer scripts carry name/DOB/callback, Rx numbers when available, prescriber; terminal state defaults to `needs_review` until extraction confirms the pharmacy accepted — UI says **"requested," never "transferred."**

**6. Audit + provenance contract (fixes wrong DCM coding; the IG profiles this):**
FHIR REST activity: IHE BALP patterns (`type=rest`, `subtype` from restful-interaction). Real-world actions: own small CodeSystem published in the IG (not misused DCM). **Two agents per committed action**: agent[0] = the AI agent as Device (requestor), agent[1] = the human confirmer — the differentiator expressed in vocabulary auditors know. One **Provenance per committed action**: who=Device, onBehalfOf=Patient, second agent = human confirmer, entity.what = transcript DocumentReference, target = Appointment/QuestionnaireResponse.

**7. Preflight contract:** checks self-declared via `required_env` on executors + core checks (secrets set + not compose-defaults, mint-secret handshake, Fasten webhook secret, provider pings, DB engine + width/identity assertions, poller singleton). `{name, ok, detail, fatal}`; aggregated `{ok, checks[]}`; dashboard card; nightly probe.

### Trust & safety layer (new in v2 — the clinical content layer)

- **Red-flag screen (mandatory, non-bypassable):** every free-text `reason`/message body passes the emergency lexicon (reuse `triage.py` SYMPTOMS + expansion: suicidal ideation, anaphylaxis, pregnancy bleeding) at propose time. Hit ⇒ refuse with 911/urgent-care escalation messaging, `emergency_indicated`, audited like a Schedule-II refusal. In-product "not for emergencies — call 911" at every propose step.
- **Structured form review (not one-tap):** per-section attestation, per-item for meds ("still taking? y/n", dose, source + date provenance) and allergies. **"No known allergies" only ever by explicit attestation — absent data renders "not reviewed with patient," never blank.** Unknown fields render blank + flagged; the system never guesses on a medical form. PDF footer states generation method, review date, per-section provenance. One 428 tap then covers *delivery* of the reviewed form.
- **Contact by reference:** payloads carry `contact_id` → `TenantContact`; server resolves numbers at execute; the model never handles raw phone numbers (wrong-number hallucination becomes a type error). Provider/pharmacy contacts verified against NPI/pharmacy directory or prior successful contact, not just patient entry.
- **RxNorm→DEA schedule lookup** (RxNav) replaces keyword matching; covers II–V (II refuse; III–V warn re one-transfer rule); keyword list kept as offline fallback.
- **Consent + authorization artifact:** stored per-tenant e-signed authorization naming the account holder as the patient's agent for scheduling/pharmacy communication (the script's "with recorded authorization" refers to something real). Half-day counsel-reviewed state matrix (recording consent, AI-disclosure statutes) before any real third-party calls. Legal-posture memo in W0.
- **Stale-data recheck:** actions carry a source-resource payload hash; commit re-verifies and demotes to `needs_review` on drift (`stale_source_data`); clinical-content proposals expire in hours.
- **Transcript as the receipt:** the account holder can read the full transcript (or hear recording) of every completed call **within a minute of it ending** — definition-of-done on comms + booking rails. Booking confirmations prompt: "check the transcript before you calendar it."
- **Allowlist + caps (unchanged from v1)** and **no-silent-simulation, fail-loud taxonomy (unchanged).**
- **Confirmation fatigue named as a design constraint:** ceremony tiered by stakes (SMS-to-self ≠ PHI-bearing call). Standing approvals for recurring low-risk actions are explicitly on the roadmap, not v1.

### Durable execution (poor-man's Temporal — deliberate; no queue, no workflow engine)

- **Intent before side effect:** attempt record inside the claim transaction (`attempt_id` = idempotency key, `claimed_at`, `provider_request_at` set immediately before provider POST) — every crash window distinguishable.
- **`reconcile()`** queries provider truth (Bland GET /calls/{id}, Twilio GET /Messages/{sid}).
- **External-tick reaper:** authenticated `POST /r6/ops/reap` on a 5-min external cron (Railway cron/GH Actions — deliberately not in-process). `executing`+`external_ref` stale → reconcile; no `external_ref`, no `provider_request_at` → `failed` (provider never called); `provider_request_at` set but no ref → `needs_review`, never auto-retry. Target semantics: **at-most-once + detect-and-reconcile** (providers give no idempotency keys; "no retries by design" stands).
- **`action_events` append-only table** (action_id, from→to, actor: commit-route|webhook|reaper|confirm, detail, ts) written in-transaction by `transition_action`. Dashboards, digests, webhook-lag, dead-letter lists, per-tenant cost/caps are views over it.
- Webhook hardening: validate `X-Twilio-Signature`; move Bland secret out of query string if supported; attempt record closes the fast-webhook race (`external_ref` null at callback).
- Long work runs at **propose** time (PDF renders synchronously before the human sees the draft; commit only delivers). Fasten daemon threads + boot reaper stay (adequate at current scale). RQ/Celery explicitly rejected for this window.
- **Node layer:** authority stripped now (no header/token minting for actions), timeouts added (`AbortSignal.timeout(15s)`, 60s conformance), Flask-native MCP migration filed as first post-webinar issue.

## Standards & regulatory workstream (W-IG, new in v2 — parallel, ~1 day/week)

1. **Continuous-build FHIR IG** (IG Publisher, auto-built via GH Actions): *Guardrails for Agent Access to Patient-Directed FHIR Data* — agent-action AuditEvent profile (two-agent shape), Provenance profile, the intake Questionnaire as a conformant SDC instance (observationLinkPeriod, launchContext, item.definition), CapabilityStatement, six guardrail properties as testable requirements. `$conformance` repointed at the IG's requirements. Unballoted continuous-build; ballot explicitly deferred to 2027.
2. **SDC exemplary-grade details:** QR `author`=agent Device, `source`=Patient, `status` flips in-progress→completed **at the 428 confirmation** (the gate expressed standards-natively); PDF as DocumentReference relating to the QR; invite SDC-workgroup review of the instance.
3. **SMART v2 per-tool granular scope table** (docs + IG); retire v1 wildcard advertising. Step-up documented as **RFC 9470 semantics** for IdP-less deployments with an upgrade path to `acr_values`.
4. **Regulatory posture doc (1 page):** foundation = patient-directed access under 45 CFR 164.524 (say the phrase in the README first paragraph); applicable breach regime = FTC Health Breach Notification Rule; explicitly not a HIPAA covered entity and what that means; corrected "via a TEFCA-connected IAS provider (Fasten)" phrasing. Fix the overstated US Core badge: "handles US Core resource types; profile-level validation on the roadmap (issue #N)."
5. **Ecosystem moves:** CMS interoperability pledge signature ("kill the clipboard" language on the forms rail); September 2026 HL7 Connectathon registration (SDC/SHL track) — registered before Aug 18 so the webinar can announce it; one chat.fhir.org thread when the IG first builds.

## Adoption & product (new in v2)

- **5 named design partners** recruited from warm network + chat.fhir.org + Medplum Discord (candidates: HBO/Jason, Gigi, Aanish, + 2 from community), each personally onboarded to a completed real action before Aug 18. Half of W5 is this, not rehearsal (a stranger executing the runbook IS the rehearsal).
- **Quickstart ends in reality:** BYO-Twilio-key path — the stranger's 15-minute payoff is a real SMS arriving on their own phone after the 428→approve loop (their key, their number: our 10DLC clock and caps don't apply).
- **README front fold rewrite (1 day, W2):** one sentence naming the user and job; 20-second GIF of the loop (propose → phone buzzes with Approve button → tap → real SMS arrives → audit trail); one command. Kitchen sink moves to docs/.
- **Metrics:** TTFA (<15 min, measured by watching design partners); activation = distinct non-Gene deployments running `$conformance` or a gated action within 30 days post-webinar (target 10); design partners live (5); week-4 retention (3); third-party executors (2 by Sep 30).
- **Consumer roadmap honesty:** the caregiver bar (20-minute self-serve parent connect), family/consent model, non-Telegram confirmation channel, and standing approvals are named roadmap items with issues, explicitly not v1.

## Workstreams & schedule

- **W0 (wk 1–2) Foundation:** provider onboarding (started 7/12) · spikes (1 real Bland call w/ extraction + 1 Twilio send; recordings become eval + fake-provider fixtures) · **out-of-band confirmation + Node authority strip** · **attempt ledger + reconcile + reaper + action_events** · identity migration **once**: `(tenant_id, resource_type, source, id)` + `meta.source` + ingest Provenance (expand-and-contract, prod snapshot, rehearsed rollback, serialized, owned by Gene) · widths + schema_sync coverage · preflight · MCP timeouts · Fasten boot reaper · poller hardening · Postgres CI · legal-posture memo · **PR #1: contracts + registry-as-plugin + fake-provider harness + generic contract tests + transition helper + state-machine reconciliation/migration**.
- **W1 (wk 2) Comms rail:** call/SMS executors on real providers behind out-of-band confirm; two-phase scripts; contact-by-reference; RxNorm lookup; transcripts-in-a-minute; observability views; golden tool-call evals (12–15 scripted conversations, tool-trace assertions, nightly).
- **W2 (wk 3) Forms rail:** structured review flow → PDF at propose → SHL delivery; SDC-conformant questionnaire; README front fold; BYO-key quickstart.
- **W3 (wk 4) Booking rail (experimental):** server-side builder; controlled-target validation; extraction eval set (15–20 transcripts incl. voicemail/reschedule/partial/wrong-number; **zero false-confirms = release gate**); real-clinic completion rate measured, not claimed.
- **W-IG (parallel, ~1 day/wk):** IG online by end wk 3; scope table + regulatory doc wk 2; pledge + Connectathon registration wk 4.
- **W5 (Aug 11–17):** freeze; half design-partner onboarding, half rehearsal; cookbook + good-first-issues written now; runbook from rehearsals; preflight green as stage precondition.

### Definition of done, every rail
Code + tests (unit, contract vs fake-provider, Postgres CI) + agent-eval coverage + docs page + runnable example + confirmation-payload definition + preflight checks via `required_env` + action_events/dashboard visibility + transcript access where applicable. No docs/example ⇒ not done.

## Error handling principles
Fail loud and specific at the gate; never fake success; never silent skip; every failure audited like success. Degradation ladder decided at freeze.

## Out of scope (v1)
Portal/browser automation; FHIR `Slot/$book`; NCPDP/Surescripts; outbound email; unlisted-number calls; consumer self-serve onboarding; family/consent model (roadmap); standing approvals (roadmap); MCP read-tool consolidation and Flask-native MCP (first post-webinar issues); IG ballot (2027).

## Success criteria
1. A stranger runs the stack locally in <15 min and receives a **real SMS on their own phone** (BYO key) after the out-of-band approve loop.
2. **5 named design partners** each completed a real gated action on their own tenant before Aug 18.
3. The confirmation chain is provably out-of-band: no code path exists where the agent's own toolchain can satisfy the gate; nonce single-use for real-world actions; golden evals verify no-commit-before-approval.
4. Preflight green on prod nightly through Aug 18; zero known silent-failure paths (each audit finding fixed or accepted in writing).
5. Postgres CI green incl. identity migration (type+source-aware) and widths.
6. Booking: zero false-confirms on the extraction eval set; real-clinic completion rate reported honestly wherever booking is described.
7. FHIR IG builds continuously and is announced (chat.fhir.org + webinar) with Connectathon registration.
8. Every rail meets its DoD; a stranger executes the full demo from the runbook.
