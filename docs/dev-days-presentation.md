# Dev Days Presentation — HealthClaw Architecture & Learnings

**Submission deadline:** June 15, 2026
**Format:** companion to `dev-days-demo-runbook.md` (live demo script) and `devpost-submission.md` (written submission)
**Talk:** "OpenClaw for Healthcare: Guardrails, Trust, and Patient Empowerment"

---

## 1. The Architecture (one slide, one story)

```text
                        PATIENTS (any device)
        ┌──────────────┬──────────────┬──────────────┐
        │  Telegram     │  careagents  │  Landline    │
        │  (OpenClaw/   │  .cloud web  │  (Bland.ai   │
        │   Hermes)     │  chat        │   voice)     │
        └──────┬───────┴──────┬───────┴──────┬───────┘
               │   MCP (19 tools) / HTTPS    │
        ┌──────▼─────────────────────────────▼───────┐
        │       MCP SERVER (Node.js, Railway)        │
        │  read · write(step-up) · action tools      │
        └──────────────────┬─────────────────────────┘
                           │ SHARP-on-MCP headers
        ┌──────────────────▼─────────────────────────┐
        │   FLASK GUARDRAIL LAYER (app.healthclaw.io)│
        │  ┌───────────────────────────────────────┐ │
        │  │ PHI redaction (Safe Harbor)           │ │
        │  │ Append-only audit (every read/write/  │ │
        │  │   action — immutable at ORM level)    │ │
        │  │ Step-up tokens (HMAC, 5-min TTL)      │ │
        │  │ Human-in-the-loop 428 gate            │ │
        │  │ Tenant isolation (WHERE-clause, not   │ │
        │  │   client-trusted)                     │ │
        │  │ Rate limiting · payload caps          │ │
        │  └───────────────────────────────────────┘ │
        │   ┌──────────┐  ┌──────────┐  ┌─────────┐  │
        │   │ FHIR     │  │ ACTIONS  │  │ Connect │  │
        │   │ store/   │  │ propose→ │  │ Fasten· │  │
        │   │ proxy    │  │ commit→  │  │ HBO·    │  │
        │   │ (R4/US   │  │ call/SMS │  │ MEDENT· │  │
        │   │ Core v9) │  │ +webhook │  │ Flexpa  │  │
        │   └────┬─────┘  └────┬─────┘  └────┬────┘  │
        └────────┼─────────────┼─────────────┼───────┘
                 │             │             │
          Any SMART/FHIR   Bland.ai      TEFCA QHINs,
          server (Epic,    (calls),      payers, EHRs
          Cerner, HAPI…)   Twilio (SMS)
```

The one-sentence version: **HealthClaw is the guardrail OS between health data and AI agents — and now between AI agents and the real world.** Reads come in through redaction; writes go out through step-up + human confirmation; and as of this week, *actions* (a phone call to your pharmacy, a text to your nurse line) go through the same propose → confirm → audit pipeline as a FHIR write.

## 2. What's new since the devpost: the Action Layer

AI agents that can only read charts are analysts. Agents that can *act* — call the pharmacy, confirm the appointment, update the insurance on file — are coordinators. The dangerous part isn't the model; it's the blast radius of a real-world side effect. So actions got the same treatment as clinical writes:

1. **Propose**: agent drafts a call script; patient sees it (tenant-scoped, rate-limited, 30-min TTL)
2. **Commit**: requires a 5-minute HMAC step-up token AND `X-Human-Confirmed: true` (HTTP 428 without it)
3. **Execute**: Bland.ai dials / Twilio texts — or **simulation mode** when no keys are configured, so every dev, CI run, and demo works with zero credentials
4. **Resolve**: provider webhook (fail-closed shared secret) records the outcome; patient gets a summary-level Telegram push — never PHI

## 3. Learnings (the section other teams can steal)

These are the things we got wrong first, found in review, and fixed — the actual transferable knowledge:

**L1 — A truthy tuple is an auth bypass.** Our step-up validator returns `(is_valid, error)`. Code that checks `if validate_step_up_token(...)` instead of destructuring passes *every* request, because a non-empty tuple is always truthy. This survived until a reviewer hunted for it specifically. Lesson: security predicates should not be tuples — and your CLAUDE.md / lint rules should encode the footguns you can't remove.

**L2 — "Failed" is the most dangerous status in a calling system.** When a phone-call API times out *after* the request was sent, the call may still happen. If you mark it `failed`, the patient re-proposes — and grandma's pharmacy gets called twice. We added a fourth outcome: `unknown` ("the provider may have acted — check, don't retry"). Mapping: timeout / 5xx / connection-reset / garbled response → `unknown`; only a pre-acceptance 4xx → `failed`. No automatic retries, ever, by design.

**L3 — Every status write must be a guarded UPDATE.** Read-then-write status transitions race: two commits both read `proposed`, both dial. The fix is one SQL statement — `UPDATE … WHERE status='proposed' AND expires_at > now` — and checking rowcount. We now apply that pattern to *every* state change (expiry, webhook resolution, post-execute), because a webhook's verdict arriving mid-request must win over the request's stale in-memory snapshot.

**L4 — PHI boundaries need a named type, not discipline.** Our model has exactly one method allowed in audit logs and Telegram pushes: `summary()` (id, kind, recipient label, status). The verbatim call script lives only in the tenant-scoped row. Tests assert the phone number and medication name *never* appear in audit detail. If the safe representation has a name, reviews can grep for violations.

**L5 — Simulation mode is a compliance feature.** Executors that no-op gracefully without API keys mean CI, the e2e gate, and live demos never need production credentials — and a partially configured provider (2 of 3 Twilio vars) *hard-fails* instead of silently pretending it sent the SMS.

**L6 — Webhooks fail closed, parse defensively.** Unconfigured secret → reject everything (403). Non-ASCII secret crashing `hmac.compare_digest` with a pre-auth 500 → compare bytes. Twilio sends form-encoded `MessageStatus`, not the JSON your tests assumed. Interim statuses (`queued`, `ringing`) must not terminally resolve an action. First verdict wins, atomically.

**L7 — Local models are not ready to hold the guardrails.** We benchmarked Claude vs. qwen3/hermes3/gemma4/qwen3.5 on five agent-safety cases (tool-call fidelity, ask-before-acting, chest-pain escalation, MCP tool selection, PHI-free summaries). Claude: 5/5. Best local (qwen3.5:9b): 4/5 at 37s/turn. hermes3:8b cheerfully booked a routine appointment for a patient describing a heart attack and leaked medications into a notification. Local models are fine as *fallbacks* for non-PHI tasks; the guardrailed patient-facing path stays on Claude.

**L8 — The OAuth callback broker pattern.** EHR OAuth flows assume a localhost redirect. Hosting a tiny callback-store on Railway (`/shc/<provider>/callback` + polled `/code` endpoint) lets a patient authorize from *any* device — their phone, a kiosk — while the script doing the token exchange runs anywhere else. This unlocked MEDENT and HBO without shipping a local server to patients.

## 4. Call to Action

1. **Fork the patterns, not just the repo.** The guardrail stack (redaction → audit → step-up → 428 → tenant isolation) is ~2,000 lines of Flask you can read in an afternoon: `github.com/aks129/HealthClawGuardrails`. MIT-style reuse is the point.
2. **Adopt the contract, get the ecosystem.** SHARP-on-MCP (sharponmcp.com) is three headers. Implement it and your agent host works against Epic, Cerner, MEDITECH, HAPI — and against every other compliant guardrail layer, including ours.
3. **Demand propose→confirm for agent actions.** If a vendor's agent can place a call or submit a form without a step-up credential and an explicit human confirmation that the server *enforces* (not the prompt), that's a prompt away from an incident. HTTP 428 is your friend.
4. **Try it in 3 commands:**
   ```bash
   git clone https://github.com/aks129/HealthClawGuardrails
   cd HealthClawGuardrails && uv sync && python main.py
   # → http://localhost:5000 · hosted: app.healthclaw.io
   ```
5. **Bootstrap Program:** `developer@healthbankone.com` — identity-verified patient data, one QR code, every AI tool you authorize.

## 5. Numbers (for the closing slide)

| | |
|---|---|
| Guardrail checks on every request | 6 (redact · audit · step-up · 428 · tenant · rate) |
| MCP tools | 19 (10 read · 5 write/step-up · 4 utility) |
| Tests | 597 Python + 56 Node, all green |
| e2e gates before deploy | 11 |
| Data sources, one guardrail layer | Fasten/TEFCA · HealthEx · Health Bank One · MEDENT · Flexpa |
| EHRs supported per-customer-code written | 0 — SHARP-on-MCP headers do the routing |
