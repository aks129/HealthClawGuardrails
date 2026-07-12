# HealthClaw Guardrails — Roadmap

HealthClaw Guardrails is an open-source safety layer between AI agents and FHIR health data. The goal: let any AI assistant safely **read** a patient's real health records *and* **take real-world actions on their behalf** — phone calls, SMS, form completion, appointment booking — with every action behind a provable, verifiable human-in-the-loop gate.

This roadmap is public so contributors can see where we're going and pick up work. It's organized **Now / Next / Later**, not by date. Want to help? See [Where to start](#where-to-start) and [CONTRIBUTING.md](CONTRIBUTING.md).

> **Design principle:** safety is *provable, not promised*. Every guardrail is asserted by the conformance harness (`GET $conformance`, graded A–F) and gated in CI. Every real-world action is fail-loud (no silent simulation) and gated by an out-of-band human approval the agent's own toolchain cannot satisfy.

---

## ✅ Shipped — the foundation

The contract floor for real actions is on `main`:

- **Provably out-of-band human gate.** An agent can *propose* an action, but `commit` only *submits* it (HTTP 202). Execution happens exclusively through a separate approval endpoint that requires a single-use step-up credential and wins an expiry-guarded atomic claim — there is no code path where the agent's own tools can approve their own action. The old spoofable `X-Human-Confirmed` header is gone.
- **The action rail + public extension point.** Real-world capabilities are `ActionExecutor`s registered in a plugin registry. Adding a capability behind the full guardrail rail (validation → human gate → audit → observability) is ~50 lines and touches no core code. See [Extending the action rail](#extending-the-action-rail).
- **Durable execution.** Attempt ledger, provider reconciliation, an external-tick reaper, and an append-only action-event log — a crashed worker can't strand or double-fire a real-world action.
- **Reliability floor.** Config preflight (`GET /r6/ops/preflight`), a Postgres CI lane (kills the SQLite-masks-varchar bug class), MCP fetch timeouts, poller storm-detection, source-aware resource identity `(tenant, type, id)`, and a mandatory red-flag emergency screen on action text.

## 🔜 Now — the comms rail

Making the first two actions *real*:

- **Phone calls** (AI voice) and **SMS** to allowlisted, patient-registered contacts — real, fail-loud, transcript-in-a-minute, behind the out-of-band gate.
- Two-phase call scripts (verify you've reached a human before disclosing anything; AI + recording disclosure), contact-by-reference (the model never handles a raw phone number), and RxNorm→DEA schedule awareness for prescription-related calls.
- Action observability: a dashboard panel + daily digest so a 2am failure is known by breakfast.

## 🟡 Next — forms + standards

- **Form completion.** SDC `$populate` fills a standard intake questionnaire from the patient's own record → a **structured per-item review** (meds and allergies confirmed individually; "no known allergies" is never inferred) → a completed PDF delivered via encrypted SMART Health Link.
- **FHIR Implementation Guide.** A continuous-build IG so the guardrails are reviewable in the community's own terms: a two-agent AuditEvent profile (AI agent + human verifier), a Provenance-per-action profile, SDC-conformant questionnaires, and the six guardrail properties as testable requirements. Standards-native expression of what the code already does.
- **Granular SMART v2 scopes** per tool; regulatory-posture doc (patient-directed access under 45 CFR 164.524; FTC Health Breach Notification Rule).

## 🔭 Later — booking, consumer, ecosystem

- **Appointment booking** (voice-call booking with structured extraction; a confirmed slot becomes a `pending` FHIR Appointment until the patient acknowledges the transcript — never `booked` on extraction alone). Validated against a controlled target first; real-clinic reliability is measured, not claimed.
- **Consumer onboarding.** A non-developer connects a family member's records from their phone in under 20 minutes, self-serve — plus a **caregiver/consent model** (one person managing several people's records, minor-turning-18, sharing with a sibling) and a non-Telegram-first approval channel.
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

## How we work

- **Provable over promised.** New guardrails ship with a conformance probe. New actions ship fail-loud with no silent simulation.
- **Docs + a runnable example are part of "done"** for every rail — not an afterthought.
- **Honest scope.** We label things experimental until the numbers say otherwise; we don't claim capabilities we can't measure.

Questions or ideas → [Discussions](../../discussions). Bugs → [Issues](../../issues/new/choose). Want to build a rail or take a `help wanted` → comment and we'll help you get oriented.
