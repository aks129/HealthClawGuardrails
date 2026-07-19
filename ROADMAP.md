# HealthClaw Guardrails — Roadmap

HealthClaw Guardrails is an open-source safety layer between AI agents and FHIR health data. The goal: let any AI assistant safely **read** a patient's real health records *and* **take real-world actions on their behalf** — phone calls, SMS, form completion, appointment booking — with every action behind a provable, verifiable human-in-the-loop gate.

This roadmap is public so contributors can see where we're going and pick up work. It's organized **Now / Next / Later**, not by date. Want to help? See [Where to start](#where-to-start) and [CONTRIBUTING.md](CONTRIBUTING.md).

> **Design principle:** safety is *provable, not promised*. Every guardrail is asserted by the conformance harness (`GET $conformance`, graded A–F) and gated in CI. Every real-world action is fail-loud (no silent simulation) and gated by an out-of-band human approval the agent's own toolchain cannot satisfy.

---

## ✅ Shipped — the foundation

The contract floor for real actions is on `main`:

- **Provably out-of-band human gate.** An agent can *propose* an action, but `commit` only *submits* it (HTTP 202). Execution happens exclusively through a separate approval endpoint that requires a single-use step-up credential and wins an expiry-guarded atomic claim — there is no code path where the agent's own tools can approve their own action. The old spoofable `X-Human-Confirmed` header is gone.
- **The action rail + public extension point.** Real-world capabilities are `ActionExecutor`s registered in a plugin registry. Adding a capability behind the full guardrail rail (validation → human gate → audit → observability) is ~50 lines and touches no core code. See [Build an ActionExecutor](docs/extending-the-action-rail.md).
- **Durable execution.** Attempt ledger, provider reconciliation, an external-tick reaper, and an append-only action-event log — a crashed worker can't strand or double-fire a real-world action.
- **Reliability floor.** Config preflight (`GET /r6/ops/preflight`), a Postgres CI lane (kills the SQLite-masks-varchar bug class), MCP fetch timeouts, poller storm-detection, source-aware resource identity `(tenant, type, id)`, and a mandatory red-flag emergency screen on action text.
- **Seven provable guardrail properties (Grade A).** `GET /r6/fhir/$conformance` grades a live deployment A–F across PHI redaction, immutable audit, step-up authorization, human-in-the-loop, tenant isolation, medical disclaimers, and **error fidelity** — the failure-path property (unknown parameters and unsupported modifiers are rejected or flagged, never silently swallowed). Enforced as a CI gate (`tests/test_guardrail_conformance.py`) and served as a self-grading endpoint.
- **The forms rail — first end-to-end real action.** `$populate` fills the canonical intake questionnaire from the patient's own record → a **structured per-item review** (every medication and allergy confirmed individually; "no known allergies" is rejected server-side unless explicitly attested, and the item list is re-derived from FHIR so a crafted request can't skip a row) → a reviewed `QuestionnaireResponse` → a provenance-stamped PDF persisted as a FHIR `DocumentReference` → a **signed, expiring download link**. The `form-fill` executor fails loud (`needs_review` / `provider_not_configured` / `stale_source_data`) and only reports `completed` on a fully rendered, persisted, linked PDF. See [the demo run-of-show](docs/demos/forms-rail-run-of-show.md).
- **CareAgents — the hosted consumer experience.** A non-developer spins up a guardrailed health agent in a minute at [careagents.cloud](https://careagents.cloud): **sign in with your face** (WebAuthn passkey — biometric stays on-device), connect records, create an agent, chat, run the forms rail. CareAgents' *only* data path is the HealthClaw guardrail layer's HTTP API — it adds experience, never policy, and stores no PHI (a connection is a pointer to a HealthClaw tenant). Shipped so far: a **pluggable connector marketplace** (Fasten, Apple Health via Open Wearables, sample records, honest "coming soon" tiles — #138/#140), **three surfaces** (web, Telegram, iMessage — #135–#137), an **advisor registry** (specialties ported from the now-archived SmartHealthConnect, prompt-blocks over the guarded tool set), **versioned informed consent** enforced server-side before any real-record connection, and Claude-subscription auth (`ANTHROPIC_OAUTH_TOKEN`, #145).
- **MCP Apps served by the engine.** `wearables` and `care_gaps` tool results carry a `_meta.ui.resourceUri` pointing at an engine-rendered view (`text/html; profile=mcp-app`) whose only fetch targets are the guarded operations — the embedded UI inherits the guardrails by construction.

## 🎯 Consumer — CareAgents toward real users (the Aug 18 push)

The consumer track, tracked in **[#134](../../issues/134)** (milestone *Aug 18 — consumer demo*). The connector marketplace, Apple Health path, and iMessage surface above have shipped; what remains is making the advisors genuinely useful and the demo bulletproof:

- **Advisor router** — route a free-form question to the right advisor instead of making the user pick. ([#158](../../issues/158))
- **Shared advisor memory** — tenant-scoped preferences only (units, goals, tone), no PHI, so advisors stop re-asking. ([#159](../../issues/159))
- **Advisor escalation** — when an advisor hits the edge of its read-only powers (e.g. "actually submit the refill"), hand off to the human approval gate instead of dead-ending. ([#160](../../issues/160))
- **Refills through the action rail** — the medication-refills advisor is read-side today (it says so); submitting a refill request becomes a proposed action behind the out-of-band gate. ([#162](../../issues/162))
- **Deferred advisors, unblocked honestly:** *kids-health* needs the caregiver identity model ([#157](../../issues/157)); *research-monitor* needs trial/preprint/FDA tools that don't exist yet. Both stay visibly "not yet" in the picker until then.
- **Sleep/nap ingestion wired into the wearables connector** ([#141](../../issues/141)); demo readiness — rehearsable run-of-show, burst-proof capacity, ecosystem contributions + directory listings. ([#142](../../issues/142), [#143](../../issues/143), [#144](../../issues/144))
- **Real disconnect + delete, self-serve** — today "Leaving" is an email to support; it should be a button that revokes the connection, deletes the tenant's data behind the guardrails, and confirms. ([#173](../../issues/173))

## 🔜 Now — the comms rail

Making the first two actions *real* — the epic is [#161](../../issues/161):

- **Phone calls** (AI voice) and **SMS** to allowlisted, patient-registered contacts — real, fail-loud, transcript-in-a-minute, behind the out-of-band gate.
- Two-phase call scripts (verify you've reached a human before disclosing anything; AI + recording disclosure), contact-by-reference (the model never handles a raw phone number), and RxNorm→DEA schedule awareness for prescription-related calls.
- Action observability: a dashboard panel + daily digest so a 2am failure is known by breakfast.

## 🟡 Next — standards + delivery hardening

- **Encrypted SMART Health Link delivery.** The forms rail ships a signed, expiring download link today; the next step wraps it in a full SHL envelope (encrypted manifest + one-time flag), reusing the Node server's SHL builder, so the PDF can be shared through the standard SHL viewer ecosystem.
- **FHIR Implementation Guide.** A continuous-build IG so the guardrails are reviewable in the community's own terms: a two-agent AuditEvent profile (AI agent + human verifier), a Provenance-per-action profile, SDC-conformant questionnaires, and the seven guardrail properties as testable requirements. Standards-native expression of what the code already does.
- **Production rigor for de-identification and validation** ([#112](../../issues/112)). Today redaction is HIPAA **Safe-Harbor-*style* field redaction** (demographics), not Expert Determination, and `fhir_validate` is **structural**, not full StructureDefinition/terminology conformance. The guardrail *contract* (redact + audit + step-up + human-confirm + tenant isolation + [error fidelity](../../pull/108)) is what's demonstrated today; closing the de-id gap (profile-specific recursive allowlists, an Expert-Determination path, PHI-canary tests) and the validation gap (profile-aware conformance + terminology binding) is how it becomes production-grade. Pairs with the IG above.
- **Error-fidelity sanitization beyond the read path.** The PHI-safe error sanitizer covers FHIR reads today; the action rail, SHL endpoints, and raw exception messages need the same allowlist-that-constructs treatment. ([#153](../../issues/153))
- **Compliance decision before beta: FTC Health Breach Notification Rule.** Multi-source aggregation makes CareAgents a personal health record under 16 CFR 318 — counsel review and a documented posture (the audit trail is the compliance asset) before real-user beta. ([#168](../../issues/168))
- **Deploy-drift observability.** The MCP server deploys manually; nothing signals when prod lags `main` (a merged tool change isn't live until someone remembers). Make the drift visible. ([#155](../../issues/155))
- **Granular SMART v2 scopes** per tool; regulatory-posture doc (patient-directed access under 45 CFR 164.524; FTC Health Breach Notification Rule).

## 🔭 Later — booking, identity, distribution

- **Appointment prep + booking through the action rail** (voice-call booking with structured extraction; a confirmed slot becomes a `pending` FHIR Appointment until the patient acknowledges the transcript — never `booked` on extraction alone). Validated against a controlled target first; real-clinic reliability is measured, not claimed. ([#163](../../issues/163))
- **One patient identity across the stack** — a person is one identity whether they arrive through HealthClaw, CareAgents, or a connector; the prerequisite for the caregiver model below. ([#157](../../issues/157))
- **Caregiver / consent model.** Self-serve consumer onboarding has shipped (see CareAgents above); what remains is one person managing several people's records — proxy access, minor-turning-18, sharing with a sibling — and the consent surface around it.
- **Distribution beyond developers** — MCP App directory submission and an install path that never shows a terminal. ([#164](../../issues/164))
- **Standing approvals** for recurring low-risk actions; NPI/pharmacy-directory contact verification; electronic Rx transfer where a rail exists.

---

## Where to start

New here? These are scoped so a stranger can land a PR:

- Issues labeled [`good first issue`](../../issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) — self-contained, with pointers to the files involved.
- Issues labeled [`help wanted`](../../issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22) — larger, mentor available (comment on the issue and we'll pair).
- The highest-leverage contribution is a **new action executor** — see below.

Run the whole stack locally in one command (see the [README](README.md) quickstart) and run the conformance harness against it to see the guardrails grade themselves.

## Extending the action rail

The `ActionExecutor` interface is the project's plugin surface — the same idea as OpenClaw personas or Medplum bots. A capability that registers here inherits the entire guardrail rail (propose-time validation, the out-of-band human gate, the audit trail, reconciliation, observability) without touching core:

```python
class ActionExecutor(Protocol):
    kind: str                       # e.g. "fax", "portal-message"
    required_env: tuple             # preflight self-assembles from these
    def validate(self, payload) -> list:      # [] if ok, else error codes
    def execute(self, action) -> ExecutionResult
    def reconcile(self, action) -> ExecutionResult   # query provider truth
```

Register it from a module under `r6/actions/rails/` and it's live. The generic contract test suite runs against every registered executor automatically — pass it and your rail is a first-class citizen. Ideas we'd love PRs for are labeled [`area: action-rail`](../../issues?q=is%3Aissue+is%3Aopen+label%3A%22area%3A+action-rail%22).

For the full walkthrough and a runnable synthetic example, see [Build an ActionExecutor](docs/extending-the-action-rail.md).

## How we work

- **Provable over promised.** New guardrails ship with a conformance probe. New actions ship fail-loud with no silent simulation.
- **Docs + a runnable example are part of "done"** for every rail — not an afterthought.
- **Honest scope.** We label things experimental until the numbers say otherwise; we don't claim capabilities we can't measure.

Questions or ideas → [Discussions](../../discussions). Bugs → [Issues](../../issues/new/choose). Want to build a rail or take a `help wanted` → comment and we'll help you get oriented.
