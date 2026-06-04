# Goal · End-to-end production validation

**Deadline:** end of day **2026-06-05** (tomorrow)
**Owner:** Eugene
**Scope:** prove the full HealthClaw stack works against *production* Fasten Connect with real EHR data flowing through every guardrail and surfacing in Telegram

## What we're validating

Eight pieces in one chain. If any link breaks, mark the goal red.

| # | Component | What "working" means |
|---|---|---|
| 1 | **Production Fasten Connect** | `FASTEN_PUBLIC_KEY` is `public_live_*` (not `public_test_*`); webhook secret verifies; live EHR connection authorizes successfully |
| 2 | **Authentication — CLEAR / ID.me (TEFCA mode)** | Identity verification in the Stitch widget succeeds; pulls span multiple QHINs |
| 3 | **Authorization — step-up + HITL** | Reads work without step-up; writes return 401 without and 428 without `X-Human-Confirmed`; `/approve` in Telegram supplies the header |
| 4 | **OpenClaw — Telegram bot** | `/start` binds chat to `ev-personal`; `/connect` returns the TEFCA URL; the four personas (Sally, Mary, Dom, Kristy) respond on their respective workspaces |
| 5 | **FHIR bundle ingestion** | `stream_ingest` background thread drains the Fasten NDJSON; `FastenJob.status` ends at `complete`; `R6Resource` rows exist for the expected types |
| 6 | **Curatr** | `FASTEN_CURATR_SCAN=true` triggers post-ingest scan; at least one quality issue surfaces in `/curatr`; an `apply_fix` round-trip writes a Provenance |
| 7 | **HAPI FHIR — upstream proxy mode** | A separate tenant set with `FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4` returns redacted records via `fhir_read`; URL rewriting verified |
| 8 | **De-identified export on Railway** | `scripts/export_healthex.py --tenant-id ev-personal --import` produces a bundle, re-ingests into a clean tenant, no PHI leaks |

## Pre-flight (do tonight)

Run these first. Each must pass before tomorrow's test cycle starts.

### Environment

```bash
# On Railway HealthClawGuardrails service — production values
railway variables set --service HealthClawGuardrails \
  FASTEN_PUBLIC_KEY=public_live_xxx \
  FASTEN_PRIVATE_KEY=private_live_xxx \
  FASTEN_WEBHOOK_SECRET=whsec_xxx \
  FASTEN_CURATR_SCAN=true \
  FASTEN_TEFCA_MODE=true \
  TELEGRAM_BOT_TOKEN=<token> \
  DASHBOARD_BASE_URL=https://app.healthclaw.io \
  STEP_UP_SECRET=<secret>

# On Railway openclaw-bot service
railway variables set --service openclaw-bot \
  TELEGRAM_BOT_TOKEN=<same-token> \
  TENANT_ID=ev-personal \
  FHIR_BASE_URL=https://app.healthclaw.io/r6/fhir \
  STEP_UP_SECRET=<same-secret> \
  DASHBOARD_BASE_URL=https://app.healthclaw.io
```

### Fasten portal config

In [portal.connect.fastenhealth.com/developers](https://portal.connect.fastenhealth.com/developers):

- [ ] Webhook delivery URL: `https://app.healthclaw.io/fasten/webhook`
- [ ] Events enabled: `patient.ehi_export_success`, `patient.ehi_export_failed`, `patient.authorization_revoked`, `webhook.test`
- [ ] Click **Send test event** → confirm 200 in `railway logs --service HealthClawGuardrails`

### Smoke test the deployment

```bash
# All three should return 200 / healthy
curl -s https://app.healthclaw.io/r6/fhir/health | jq .status
curl -s https://mcp-server-production-5112.up.railway.app/health | jq .status
curl -s https://app.healthclaw.io/fasten/connections -H "X-Tenant-Id: ev-personal" | jq .
```

### CI green confirms shippable code

- [ ] `gh run list --workflow=ci.yml --limit 1 --json conclusion` → `success`
- [ ] All seven jobs green (python, node, playwright, compose-smoke, compliance-gates, secret-scan, dependency-audit)

## Test sequence (tomorrow, ~60-90 min)

Run these in order. Each step has a verification command — capture the output for the sign-off section at the end.

### T-0  Bot binding (component 4)

In Telegram:

```
/start
```

**Expect:** "✅ Chat bound to tenant `ev-personal` — you will get a ping when records arrive."

**Verify server-side:**
```bash
railway run --service HealthClawGuardrails \
  python -c "from main import app; from r6.models import TelegramBinding;
import json
with app.app_context():
    print(json.dumps(TelegramBinding.chat_ids_for_tenant('ev-personal')))"
```
**Expect:** `[<your chat id>]`

### T-5  Live connect (components 1 + 2)

In Telegram:
```
/connect
```

**Expect:** Bot replies with `https://app.healthclaw.io/connect/ev-personal`.

Open the URL in a browser:
- [ ] Page renders with eyebrow text "Fasten Connect · TEFCA mode"
- [ ] Stitch widget loads (no "FASTEN_PUBLIC_KEY is not set" warning)
- [ ] Click through CLEAR or ID.me — full verification flow
- [ ] On `widget.complete`, the in-page status shows "Connection registered. Records will stream in over the next 5-45 minutes."

**Verify server-side:**
```bash
curl -s -H "X-Tenant-Id: ev-personal" \
  https://app.healthclaw.io/fasten/connections | jq '.[] | .org_connection_id'
```
**Expect:** the new `org_connection_id` from the widget.

### T-10 to T-45  Ingest watch (component 5)

While Fasten is pulling:

```bash
# Tail the Flask logs — should see webhook events and stream_ingest progress
railway logs --service HealthClawGuardrails 2>&1 | \
  grep --line-buffered -E "Fasten|stream_ingest|ingested|webhook"
```

When the Telegram push arrives ("📥 Records imported — N resources"):

- [ ] Push appears within 30s of `Fasten job ... complete` log line
- [ ] Resource count > 0
- [ ] Curatr issue count > 0 (if your real data has at least one quality issue)

**Verify resource counts:**
```bash
for type in Patient Condition Observation MedicationRequest Immunization Procedure; do
  count=$(curl -s -H "X-Tenant-Id: ev-personal" \
    "https://app.healthclaw.io/r6/fhir/$type?_summary=count" | jq -r .total)
  echo "$type: $count"
done
```

### T-50  Read with redaction (component 3 read tier)

In Telegram:
```
/summary
/conditions
/labs
```

For each:
- [ ] Returns data (not "No conditions found" — you have real records)
- [ ] Patient name appears as initials (`G. H. K.` style), not full name
- [ ] Identifiers masked to last 4 chars
- [ ] Telecom values stripped or `[Redacted]`

**Verify via direct read:**
```bash
PID=$(curl -s -H "X-Tenant-Id: ev-personal" \
  "https://app.healthclaw.io/r6/fhir/Patient?_count=1" | jq -r '.entry[0].resource.id')
curl -s -H "X-Tenant-Id: ev-personal" \
  "https://app.healthclaw.io/r6/fhir/Patient/$PID" | jq '.name, .identifier, .address'
```
**Expect:** redacted shapes (initials, `***####`, no `line` on address).

### T-55  Curatr round-trip (component 6)

```
/curatr
```

- [ ] Returns ≥1 finding from real data
- [ ] Each finding has severity, resource type, ICD/SNOMED context

If a fix is available:
```
/curatr fix
/approve
```

- [ ] Curatr proposes a specific change
- [ ] `/approve` returns success and creates a Provenance

**Verify the Provenance:**
```bash
curl -s -H "X-Tenant-Id: ev-personal" \
  "https://app.healthclaw.io/r6/fhir/Provenance?_count=5&_sort=-_lastUpdated" | \
  jq '.entry[].resource | {recorded, target: .target[0].reference, agent: .agent[0].who.display}'
```
**Expect:** newest Provenance has `agent.who.display = "curatr"`.

### T-60  Authorization gates (component 3 write tier)

```bash
# 1. Write without step-up should 401
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  "https://app.healthclaw.io/r6/fhir/Patient" \
  -H "X-Tenant-Id: ev-personal" \
  -H "Content-Type: application/json" \
  -d '{"resourceType":"Patient","name":[{"family":"GateTest"}]}')
echo "no-step-up status: $STATUS (expect 401)"

# 2. With step-up but no human confirm on a clinical type should 428
TOKEN=$(curl -s -X POST \
  "https://app.healthclaw.io/r6/fhir/internal/step-up-token" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"ev-personal"}' | jq -r .token)

STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  "https://app.healthclaw.io/r6/fhir/Condition" \
  -H "X-Tenant-Id: ev-personal" \
  -H "X-Step-Up-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"resourceType":"Condition","subject":{"reference":"Patient/test"},
       "clinicalStatus":{"coding":[{"system":"http://terminology.hl7.org/CodeSystem/condition-clinical","code":"active"}]},
       "verificationStatus":{"coding":[{"system":"http://terminology.hl7.org/CodeSystem/condition-ver-status","code":"confirmed"}]},
       "code":{"text":"GateTest"}}')
echo "step-up without HITL status: $STATUS (expect 428)"
```

**Both must match expected codes** or component 3 is red.

### T-65  HAPI upstream proxy (component 7)

Switch a *test* tenant to HAPI proxy mode (do not touch `ev-personal`):

```bash
# Set on Railway via dashboard or CLI
railway variables set --service HealthClawGuardrails \
  FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4

# Wait for redeploy, then verify
curl -s -H "X-Tenant-Id: hapi-test" \
  "https://app.healthclaw.io/r6/fhir/Patient?_count=1" | \
  jq '.entry[0].resource | {id, name, _source}'
```

- [ ] `_source: "upstream"` field present
- [ ] Names redacted as usual
- [ ] Resource IDs match HAPI's ID space (long numerics)
- [ ] No `hapi.fhir.org` URL leaks in response body

**Unset and redeploy** when this step finishes so `ev-personal` returns to local mode:
```bash
railway variables --service HealthClawGuardrails --remove FHIR_UPSTREAM_URL
```

### T-75  De-identified Railway re-ingest (component 8)

```bash
# Export ev-personal to a bundle (de-identified)
python scripts/export_healthex.py \
  --tenant-id ev-personal \
  --base-url https://app.healthclaw.io/r6/fhir \
  --output /tmp/ev-export.json

# Inspect — no PHI should survive
jq '.entry[].resource | select(.resourceType == "Patient")' /tmp/ev-export.json
```

- [ ] No `family` or `given` names with more than initials
- [ ] `identifier[].value` is `urn:healthclaw:patient` only (institutional identifiers stripped)
- [ ] `birthDate` truncated to year
- [ ] No `address.line`, no `telecom`

Re-ingest into a clean tenant:
```bash
python scripts/import_healthex.py \
  --bundle-file /tmp/ev-export.json \
  --tenant-id ev-deidentified-test \
  --base-url https://app.healthclaw.io/r6/fhir \
  --step-up-secret <secret>
```

- [ ] Returns 200, `created_count` > 0
- [ ] `curl /Patient?_count=1` against the new tenant shows the de-identified shape

### T-85  Agent persona check (component 4 cont'd)

In each persona's workspace on the Mac mini:

| Persona | Test prompt | Expected behavior |
|---|---|---|
| Sally-PCP | "Show my conditions" | Calls `cmd_conditions` → returns redacted list |
| Mary-pharmacy | "What meds am I on?" | Calls `cmd_meds` → redacted MedicationRequest list |
| Dom-fitness | "Recent vitals" | Calls `cmd_vitals` → recent BP / HR / weight |
| Kristy-scheduler | "Any conflicts this week?" | Calls `cmd_week` / `cmd_conflicts` |

Each persona invocation should show in Flask logs with its own `agent_id`.

## Success criteria · Definition of Done

The goal is green if **all of the following are true at end of day 2026-06-05:**

- [ ] Components 1-8 each have at least one ✅ above
- [ ] No PHI appears in `railway logs --service HealthClawGuardrails 2>&1 | head -500`
- [ ] Audit trail has entries for every read above:
  ```bash
  curl -s -H "X-Tenant-Id: ev-personal" \
    "https://app.healthclaw.io/r6/fhir/AuditEvent?_count=50&_sort=-_lastUpdated" | \
    jq '.entry | length'
  # expect 30+
  ```
- [ ] Telegram chat shows the full session: `/start` → `/connect` → "📥 Records imported" → `/summary` / `/conditions` / `/curatr` → fix applied → `/dashboard` link
- [ ] CI on main is green (Michael's fork test passes on a fresh clone)

## Recovery plays

| If… | Then… |
|---|---|
| Stitch widget fails to load | Check `FASTEN_PUBLIC_KEY` is set on Railway and re-deploy; check browser console for CSP/CORS |
| Webhook never fires | Verify webhook URL in Fasten portal; resend a test event from the portal |
| `📥 Records imported` ping never lands | Check `TELEGRAM_BOT_TOKEN` is set on Flask service (not just openclaw-bot); check `TelegramBinding.chat_ids_for_tenant('ev-personal')` is non-empty |
| 401 on `/internal/bind-telegram` | `STEP_UP_SECRET` mismatch between Flask and openclaw-bot services; align values + redeploy both |
| Curatr finds nothing | Your data may be clean — pivot to demonstrating a synthetic example from `desktop-demo` tenant |
| HAPI upstream times out | HAPI public server is sometimes slow; try `https://r4.smarthealthit.org` instead |
| Export bundle has PHI | Bug — file an issue immediately, do not re-ingest |

## Sign-off

When done, paste this into the issue or chat:

```
GOAL 2026-06-05 RESULT: <PASS / PARTIAL / FAIL>

Components:
  1. Production Fasten Connect:   <PASS / FAIL / NOTES>
  2. CLEAR/ID.me auth:            <PASS / FAIL / NOTES>
  3. Step-up + HITL:              <PASS / FAIL / NOTES>
  4. OpenClaw bot:                <PASS / FAIL / NOTES>
  5. FHIR ingestion:              <PASS / FAIL / NOTES>
  6. Curatr:                      <PASS / FAIL / NOTES>
  7. HAPI upstream proxy:         <PASS / FAIL / NOTES>
  8. De-identified re-ingest:     <PASS / FAIL / NOTES>

Resource counts in ev-personal: <fill in>
Audit events emitted during test: <fill in>
Curatr findings (true positives): <fill in>
Provenance records created: <fill in>

Issues filed: <list>
Followups for next session: <list>
```
