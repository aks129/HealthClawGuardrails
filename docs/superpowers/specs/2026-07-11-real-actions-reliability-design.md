# Real Actions + Reliability — Design (v3, FINAL after two review-board rounds)

**Date:** 2026-07-11 (v3 after 5-role board × 2 rounds: product, clinician, patient/caregiver, FHIR/CMS, agentic architect)
**Status:** Board-converged; awaiting Gene's sign-off on the one flagged scope change (booking, below)
**Deadline:** Feature freeze 2026-08-11; HIMSS Keystone webinar 2026-08-18
**Goal:** A reliable *real system*: an AI agent that safely reads a patient's real health data AND takes real actions on their behalf — behind a human-in-the-loop gate that is **provably out-of-band**. Success is measured on strangers, not on the founder's tenant.

## Round-2 verdicts (what this version resolves)

Product: NO-GO without cuts → cuts applied. Architect: NO-GO on W0 arithmetic + confirm-flow ambiguity → both fixed. Clinician: 3 fixes → applied. Caregiver: YES ("week two is earned by the transcript") + 3 fixes → applied. FHIR: conditional yes → 3 fixes applied. Unanimous thread across rounds: **the schedule breaks at booking, and the content layer (what humans see, what calls say) is as load-bearing as the code.**

## ⚑ FLAGGED SCOPE CHANGE (needs Gene's explicit call)

**Booking ships zero code before the freeze.** Round 1: four of five reviewers ranked booking last / not-ready. Round 2: product ("relabeling changes the claim, not the burn rate — it remains the first cut") and architect ("W0 doesn't fit its two weeks") converged: the week booking costs is exactly the week the foundation needs. v3 therefore: booking is **designed in this spec** (semantics pinned below so nothing is rework later) but **built as the first post-webinar milestone (September)**, demoed at the webinar as an honestly-labeled gated proposal. This modifies the earlier "all four real by Aug 18" decision — it is the single deviation from that mandate, and it is what "realistic and truth-based" costs. Calls, SMS, and forms remain real by Aug 18.

## Users of record

- **Beachhead: the developer** who must gate an AI agent on clinical data ("gate my agent's actions behind human confirmation + audit in 15 minutes"). The webinar audience.
- **Payload persona: the family caregiver** — the demo's protagonist. v1 ships no consumer onboarding; the roadmap names the bar (20-minute self-serve parent connect), the family/consent model, non-Telegram-first channels, standing approvals. **The webinar says "not yet self-serve" on stage** — the caregiver story without that label is retroactive bait-and-switch.
- **Design partners: 3 named, warm** (Jason/HBO, Gigi, Aanish), onboarding **starts week 3** (not W5), each completing a real gated action **as their own patient** (v1 authorization is a stored attestation, not verified proxy authority — acting-for-others is excluded until the consent model exists). +2 community partners = stretch, not criterion. The BYO-key quickstart IS the partner path.

## Scope (final)

| Rail | Aug 18 state | Acceptance bar |
|---|---|---|
| Phone calls | **Real** | Two-phase script, allowlisted referenced contact, transcript in a minute |
| SMS | **Real** (positioned as channel/quickstart payoff, not headline) | Allowlisted, capped |
| Form completion | **Real, timeboxed** | Structured per-item review → PDF at propose → SHL delivery (SHL-only; SMS-link leg cut). One questionnaire, freeze-quality |
| Appointment booking | **Zero code; labeled proposal demo** | Post-webinar milestone: controlled target line, extraction eval set, zero-false-confirms release gate, `Appointment.status: pending` until required transcript acknowledgment — never `booked` on extraction alone |

Provider onboarding (Twilio A2P/toll-free, Bland account+number) started 2026-07-12 — the uncontrollable critical path. Freeze Aug 11; degradation ladder decided at freeze.

## Architecture

### One action rail
`ProposedAction` engine is the spine; each capability = server-side **builder** (assembles/validates payload at propose; model supplies references + preferences, never clinical facts or raw numbers) + **executor** (side effect). Registered via `register_executor()` from per-rail modules; `VALID_KINDS` derived; rails never touch shared files.

### Pinned contracts (PR #1, with tests — parallel agents build only against merged contracts)

**1. State machine (canonical; migrates existing rows):**
`proposed → awaiting_confirmation → executing → completed | failed | needs_review | expired`, plus `unknown` (post-possible-send, reaper-reconciled). Transitions only via canonical `transition_action(action_id, from_states, to_state, actor, **fields)` (guarded UPDATE + in-transaction `action_events` append; decorative ORM `transition()` deleted).
- `awaiting_confirmation → expired` is the **common** expiry path: reaper sweeps it, sends a "your pending approval lapsed" notice with **one-tap re-propose** (same reviewed content re-validated, not a redo).
- **Staleness refuses, never reviews:** source-payload hash checked before execution; drift ⇒ refuse commit back to `awaiting_confirmation` with `stale_source_data` (never `needs_review`, which is strictly "executed, outcome unconfirmable, evidence attached").
- `simulated` is a flag, never a state; explicit dev flag only; visually distinct.

**2. Confirmation flow — Approve IS the commit:**
`action_commit` (MCP) = *submit for confirmation*: runs red-flag screen + validation + staleness hash, transitions to `awaiting_confirmation`, pushes the confirm card, returns "terminal for this turn; the patient approves out-of-band; poll `action_status`" (phrasing verified by golden evals). The **human's Approve executes**: the authenticated dashboard/Telegram handler performs the claim + execution server-side — no second agent turn, no expired-token dance, one less trust hop. Consumption is **atomic with the claim** (guarded UPDATE joins `action_confirmations.consumed_at IS NULL`, same transaction). `ActionConfirmation` is single-use with its own TTL (15 min clinical-content kinds; 4 h default). The Node forwarder never mints `X-Human-Confirmed` or tokens for actions (tools.ts:1839 deleted); step-up nonce consumed for all real-world actions.

**3. Ceremony tiers (specced now, not prose):**
- **Tier 1 — clinical-content kinds (form-fill, booking):** chat/SMS button can NEVER satisfy the gate — it deep-links to the **dashboard review flow**, the canonical review+approve surface. `ActionConfirmation` requires a completed review record: per-item, **default-unconfirmed, Approve disabled until every med/allergy item is individually acted on.**
- **Tier 2 — PHI-bearing comms (phone-call, rx):** full confirm card, approvable from Telegram inline button **or** SMS link → dashboard card (the non-Telegram path is first-class v1; the confirmation channel must be individually held, never a shared family chat).
- **Tier 3 — low-stakes (SMS to self/registered own numbers):** lightweight confirm.
- Standing approvals for recurring actions: roadmap, not v1.

**4. Confirmation payload contract (what the human sees, per kind):**
`phone-call`: contact name + relationship + number, **disclosure list rendered ABOVE the verbatim script** ("this call will state: name, DOB, these 2 medications — nothing else"), voicemail policy, cost. `sms`: recipient + full text. `form-fill`: the structured review flow itself (the PDF is its output). All kinds: "not for emergencies — call 911."

**5. Call-script skeleton (pinned):**
Phase 1 verify-before-disclose: AI + recording disclosure, confirm establishment + human; failure ⇒ end call, `needs_review`. Phase 2 minimum-necessary: identity payload (name, DOB, callback), then the request. Voicemail: zero-PHI callback request. Refusal branch ⇒ graceful end, `needs_review` ("clinic requires patient call directly" — a useful outcome). Rx: carries name/DOB/callback/Rx#/prescriber; terminal `needs_review` until extraction confirms acceptance; UI says **"requested," never "transferred."**

**6. Red-flag screen (owner: PR #1/W0 — mandatory, non-bypassable):**
Runs at propose on every free-text reason/body. SMS bodies: lexicon (triage.py SYMPTOMS + suicidal ideation, anaphylaxis, pregnancy bleeding). Booking reasons (when built): **structured red-flag question set or a classifier held to a zero-false-negative eval gate — lexicon-only is insufficient for free-text reasons.** Hit ⇒ refuse, 911/urgent-care escalation messaging, `emergency_indicated`, audited.

**7. Executor contract (public, versioned — the extension point):**
```python
class ActionExecutor(Protocol):
    kind: str
    required_env: tuple[str, ...]        # preflight self-assembles
    def validate(self, payload: dict) -> list[str]      # at PROPOSE
    def execute(self, action: ProposedAction) -> ExecutionResult
    def reconcile(self, action: ProposedAction) -> ExecutionResult
```
Error taxonomy: `provider_not_configured`, `contact_not_allowlisted`, `daily_cap_reached`, `payload_invalid`, `provider_error`, `extraction_ambiguous`, `emergency_indicated`, `stale_source_data`.

**8. Audit + provenance contract (bindings pinned so FSH can't garble):**
FHIR REST: IHE BALP (`type=rest`, `subtype` from restful-interaction). Real-world actions: own CodeSystem (IG-published), never repurposed DCM. Two agents per committed action: AuditEvent agent[0] = AI Device `requestor=true`; agent[1] = human confirmer `requestor=false`. One Provenance per action: Device `agent.type=performer`, human `agent.type=verifier` (provenance-participant-type), `onBehalfOf`=Patient, transcript DocumentReference at `entity.role=source`, target = QuestionnaireResponse/Appointment. **QR authorship: post-attestation `author`=Patient** (responsible for the answers), Device's population role carried in Provenance — resolves the v2 contradiction.

### Durable execution (unchanged from v2, semantics tightened)
Attempt ledger inside the claim transaction (`attempt_id`, `claimed_at`, `provider_request_at`); `reconcile()` queries provider truth; external-tick reaper (`POST /r6/ops/reap`, 5-min external cron): `executing`+ref stale ⇒ reconcile; no ref + no `provider_request_at` ⇒ `failed`; `provider_request_at` set + no ref ⇒ `needs_review`, never auto-retry; plus the `awaiting_confirmation` expiry sweep. At-most-once + detect-and-reconcile. `action_events` append-only, in-transaction. Twilio signature validation; Bland secret out of query string. PDF renders at propose. No RQ/Celery. Node: authority stripped + `AbortSignal.timeout(15s)`; Flask-native MCP = first post-webinar issue.

### Trust & safety (load-bearing set — final)
**In v1:** red-flag screen · contact-by-reference (`contact_id`; server resolves; contacts verified on first successful contact) · structured form review (per-item, default-unconfirmed; "no known allergies" only by explicit attestation; absent renders "not reviewed with patient"; never guess; provenance footer) · **RxNorm→DEA schedule lookup** (RxNav, ~1 day, closes a real hole; keyword list as offline fallback; II refuse, III–V one-transfer warning) · stored checkbox attestation + counsel-reviewed state matrix + legal-posture memo · staleness hash · transcripts readable within one minute (DoD) · allowlist + per-tenant caps · no-silent-simulation.
**Deferred (filed issues):** NPI/pharmacy directory verification · e-signed authorization flow · family/consent model · standing approvals.

## Standards workstream (W-IG — minimum viable, ~1 day/week)
**In:** AuditEvent + Provenance profiles (bindings above), action-type CodeSystem, ONE validating SDC Questionnaire instance (observationLinkPeriod, launchContext, item.definition; QR status flips at the gate), CapabilityStatement, prose home page stating the six guardrail properties ("formalization as testable requirements: 2027, issue #N").
**Out (post-webinar):** guardrails-as-Requirements, `$conformance` repoint, SMART v2 per-tool scope table (docs task, week 5 if time), SDC-exemplary polish.
**Gates:** chat.fhir.org announcement only on a **clean QA build** (zero errors, explained warnings) — a red-QA IG is worse than none. Regulatory one-pager (164.524 foundation — the phrase goes in the README first paragraph; FTC HBNR; not-a-covered-entity; "via a TEFCA-connected IAS provider (Fasten)"; US Core badge → "handles US Core resource types; profile validation on the roadmap"). CMS pledge + September Connectathon registration before Aug 18 (webinar announces it).

## Adoption & product
3 warm design partners onboarded from week 3 (~1 day/week support budgeted), as their own patients, via the BYO-key quickstart. Quickstart payoff = **a real SMS on the stranger's own phone** after the out-of-band approve loop, <15 min. README front fold rewrite (1 day, W2): one sentence naming user+job, 20-second GIF of the loop, one command. Metrics: TTFA <15 min (measured by watching partners); activation = 10 non-Gene deployments in 30 days post-webinar; partners live = 3 (stretch 5); week-4 retention = 3; third-party executors = 2 by Sep 30. Cookbook + good-first-issues written webinar week.

## Schedule (re-cut per round-2 arithmetic)
- **W0 (Jul 13 – Jul 29, ~2.5 wk):** provider onboarding + spikes (recordings = eval + fake-provider fixtures) · **PR #1** (contracts, registry-as-plugin, fake-provider harness, generic contract tests, transition helper, state-machine migration, red-flag screen) · **confirmation flow** (Approve-is-commit, atomic consumption, TTLs, expiry sweep + re-propose, Node strip) · **durability stack** · **identity migration SPLIT:** wk 1 = composite unique constraint + tenant-scoped lookups (the bug fix; expand-and-contract, prod snapshot, rehearsed rollback, serialized, Gene-owned); `source` column + ingest Provenance = W2 additive · widths + schema_sync · preflight · MCP timeouts · Fasten boot reaper · poller hardening · Postgres CI · legal memo. Big-four items serialized; small items parallel via subagents.
- **W1 (Jul 30 – Aug 5) Comms rail:** calls + SMS real behind the gate; two-phase scripts; contact-by-reference; RxNorm; transcripts-in-a-minute; observability views; golden tool-call evals (12–15, nightly); **partner onboarding begins.**
- **W2 (Aug 5 – Aug 11) Forms rail:** structured review → PDF at propose → SHL; SDC-conformant instance; README fold; BYO-key quickstart; `source`/Provenance ingest completion; IG clean-QA build.
- **Freeze Aug 11 → Aug 17:** half partner completion, half rehearsal; cookbook/GFIs; runbook from rehearsals; preflight green = stage precondition.
- **Post-webinar first milestones (filed now):** booking rail (controlled target, extraction evals, pending-until-ack), Flask-native MCP, read-tool consolidation, `$conformance`→IG repoint, consumer onboarding path, family/consent model.

### Definition of done, every rail
Code + tests (unit, contract-vs-fake-provider, Postgres CI) + agent-eval coverage + docs page + runnable example + confirmation-payload definition + preflight via `required_env` + `action_events` visibility + transcript access where applicable.

## Out of scope (v1)
Booking code (post-webinar) · portal automation · `Slot/$book` · NCPDP/Surescripts · outbound email · unlisted numbers · consumer self-serve onboarding · family/consent model · standing approvals · NPI directory verification · e-sign flow · MCP read consolidation · Flask-native MCP · IG ballot (2027).

## Success criteria
1. A stranger runs the stack locally in <15 min and receives a **real SMS on their own phone** (BYO key) after the out-of-band approve loop.
2. **3 named design partners** each completed a real gated action (as their own patient) before Aug 18.
3. The gate is provably out-of-band: no code path lets the agent's toolchain satisfy it; Approve-is-the-commit with atomic single-use consumption + TTL; golden evals verify no-commit-before-approval and no retry-looping.
4. Preflight green on prod nightly through Aug 18; zero known silent-failure paths (every audit/board finding fixed or accepted in writing).
5. Postgres CI green incl. the constraint migration and widths.
6. IG: clean QA build, announced on chat.fhir.org, Connectathon registered — all before Aug 18.
7. Every shipped rail meets its DoD; a stranger executes the full demo from the runbook.
8. Webinar honesty: booking demoed as a labeled proposal; "not yet self-serve" said on stage.
