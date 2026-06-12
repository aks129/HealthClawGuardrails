# Unified Action Layer — careagents.cloud × OpenClaw/Hermes Integration

**Date:** 2026-06-12
**Status:** Approved design, pre-implementation

## Goal

One backend, two faces. The careagents.cloud web UI and the Telegram personas
(Sally-PCP et al. via OpenClaw/Hermes) become two front-ends over the same
HealthClaw MCP server. Real-world actions — phone calls, SMS, form filling,
insurance updates, QR health sharing — become MCP tools executed behind the
existing guardrail stack (step-up auth, human confirmation, audit trail, PHI
redaction), so every agent surface gets them identically.

## Decisions (locked during brainstorm)

| Decision | Choice |
| --- | --- |
| Architecture | Actions live in Flask (`r6/actions/`), MCP tools proxy to it |
| Approval gate | Same as FHIR writes: step-up token + `X-Human-Confirmed` + AuditEvent |
| QR format | SMART Health Links (`shlink:/` via existing `r6/shc/` module) |
| Identity | Telegram Login Widget on careagents.cloud → `TelegramBinding` tenant |
| Insurance data | Flexpa as fourth connector (authoritative Coverage + claims) |
| Provider directory | ainpi.dev primary, public NPI Registry fallback |
| Forms v1 | Upload (Telegram attachment / web upload), NOT email; inbound email is stretch |
| Call retries | None — a double-placed call is worse than a failed one |

## Components

### 1. Flask module `r6/actions/` (Blueprint at `/r6/actions`)

- **`models.py`** — `ProposedAction`: `id`, `tenant_id`, `kind`
  (`phone-call` | `sms` | `form-fill` | `insurance-call`), `payload_json`,
  `status` (`proposed` → `confirmed` → `executing` → `completed` | `failed` |
  `expired` | `unknown`), `external_ref`, timestamps. Proposals expire after
  30 minutes (same TTL convention as context envelopes).
- **`routes.py`**
  - `POST /r6/actions/propose` — tenant headers; creates proposal, returns id + redacted draft
  - `POST /r6/actions/<id>/commit` — requires step-up token AND `X-Human-Confirmed: true`; 428 without confirmation (same contract as FHIR writes). Destructure `validate_step_up_token` tuple.
  - `GET /r6/actions/<id>` — status poll
  - `POST /r6/actions/callback/<provider>` — Bland/Twilio webhooks, HMAC-verified; updates status, stores redacted outcome summary
- **`executors.py`** — Bland.ai (calls) + Twilio (SMS) clients; simulation mode
  when keys absent. Form filler (PDF AcroForm fill from FHIR context; OCR via
  vision model for photos). SHL generation delegates to `r6/shc/`.
- **Guardrails** — every propose/commit/callback emits AuditEvent. Stored
  scripts and chat previews pass `apply_redaction`. Telegram pushes via
  `notify_tenant` stay summary-level (no PHI).

### 2. MCP server — 5 new tools

| Tool | Group | Notes |
| --- | --- | --- |
| `action_propose` | write-adjacent | creates proposal, returns id + draft |
| `action_commit` | write (step-up) | forwards step-up + confirmation headers |
| `action_status` | read | poll status/outcome |
| `shl_generate` | write (step-up) | SMART Health Link QR for bundle + Coverage + wearables |
| `provider_lookup` | read | ainpi.dev primary, NPI Registry fallback; no PHI |

Claude Desktop path: `_stepUpToken` / `_tenantId` tool-argument fallbacks, as
with existing write tools.

### 3. Flexpa connector `r6/flexpa/`

Same shape as `r6/fasten/`: link-flow route + ingester. Pulls Coverage,
ExplanationOfBenefit, claims from the payer. Insurance skills prefer
Flexpa-sourced Coverage; EHR-sourced Coverage is fallback.

### 4. Skills (distributed by existing installers to Hermes + OpenClaw)

`connect-health-sources`, `pcp-advisor`, `call-pcp-followup`,
`refill-by-phone`, `find-provider-pharmacy`, `insurance-update-call`,
`fill-intake-form`, `share-health-qr`.

Skill rules: recommendations are administrative, never clinical advice;
controlled substances route to provider, not pharmacy refill line; insurance
call confirms active plan summary before proposing; preferred
Practitioner/pharmacy stored on tenant after `find-provider-pharmacy` so later
skills stop asking.

### 5. careagents.cloud rewire

- Telegram Login Widget → `POST /r6/auth/telegram` (hash verified against
  `TELEGRAM_BOT_TOKEN`) → resolve `TelegramBinding.chat_id` → short-lived
  session JWT carrying tenant ID.
- `/api/chat` routes model calls through the MCP HTTP bridge (`/mcp/rpc`)
  instead of executing local tools; `/api/actions/execute` becomes a proxy to
  `action_commit`.
- Flask resolves tenant from JWT, not from browser-supplied `X-Tenant-ID`.
  `?tenant=` query param survives only behind a dev flag.

## Use-case mapping

1. **Connect sources** — `connect-health-sources` skill sends Fasten widget /
   HBO / MEDENT / Flexpa auth links over chat; OAuth callback brokers already
   device-independent; post-ingest `notify_tenant` resumes the conversation.
2. **Recommendations** — `pcp-advisor`: `context_get` + `fhir_lastn` +
   `curatr_evaluate` + compliance disclaimers; each recommendation offers an
   action from the catalog.
3. **Call PCP about results** — propose(phone-call) with script from
   triggering results → confirm → Bland call → webhook → outcome relayed.
4. **Refill call** — same pipeline; med/dosage from MedicationRequest,
   pharmacy from preferences; controlled-substance check.
5. **Find PCP & pharmacy** — `provider_lookup` (ainpi.dev → NPI fallback);
   choice stored as preferred Practitioner/Organization.
6. **Fill forms** — v1 upload path (Telegram attachment or web upload) →
   field extraction → fill from FHIR context → return completed PDF for
   patient review; patient sends onward. v2 stretch: per-tenant inbound email
   via Resend.
7. **Insurance update call** — script from Flexpa Coverage (carrier, member
   ID, group) → billing-desk call after active-plan confirmation.
8. **QR share** — `shl_generate`: clinical export + Coverage + latest
   wearable Observations → patient-controlled redaction → encrypted SHL with
   TTL + revocation ("revoke that QR" kills the link) → QR PNG to chat/web.

## Action lifecycle

```text
PROPOSE  → ProposedAction(status=proposed, TTL 30m), redacted draft to user
CONFIRM  → "yes confirm" / Approve button → step-up + X-Human-Confirmed
           → status=confirmed→executing, AuditEvent
EXECUTE  → Bland/Twilio API call, external_ref stored
CALLBACK → HMAC-verified webhook → status=completed + redacted outcome
           → notify_tenant (counts/status only)
REPORT   → persona or web UI relays outcome (action_status poll as backup)
```

Failure handling: expired → re-propose; bad step-up → 401 + re-auth prompt;
provider API error → `failed` + manual-call fallback message with number;
missing webhook → poll timeout 15 min → `unknown` + "check with them" message.

## Testing

- Python: state machine (expiry, double-commit, 428, step-up tuple),
  webhook HMAC, script redaction; executors in simulation mode + one mocked
  contract test per provider.
- MCP: Jest tests for 5 tools mirroring existing write-tool tests.
- E2E: Playwright careagents flow (propose→approve→simulated→status);
  scripted Telegram flow via skill-test harness.
- `demo_e2e.sh` gate 11: simulated propose+commit asserts AuditEvent exists.

## Rollout phases (independently shippable)

1. **Action core** — `r6/actions/` + MCP tools, simulation mode, audit/step-up
2. **Sally calling skills** — advisor/follow-up/refill skills + Bland key
3. **Lookup + insurance** — ainpi.dev `provider_lookup`, Flexpa connector, insurance skill
4. **Share + forms** — SHL QR + upload form-filler
5. **careagents.cloud rewire** — Telegram login, MCP bridge backend
6. *(stretch)* inbound email forms via Resend

## Out of scope

- Clinical advice of any kind (administrative coordination only)
- Email ingestion in v1 (upload only)
- Call retries / autonomous re-dialing
- Non-Telegram identity providers for careagents.cloud
