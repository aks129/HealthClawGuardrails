---
name: fasten-connect
description: "Use this skill whenever connecting a patient's real health records from EHR systems (Epic, Cerner, Athena) or the TEFCA national network into HealthClaw Guardrails. Covers: Fasten Stitch widget embed, org_connection_id registration, EHI export job tracking, NDJSON ingestion status, TEFCA IAS identity-verified multi-provider retrieval, and post-import Curatr quality scan workflow."
homepage: https://docs.connect.fastenhealth.com
disable-model-invocation: true
metadata: {"openclaw":{"requires":{"env":["FASTEN_PUBLIC_KEY","FASTEN_PRIVATE_KEY"]},"primaryEnv":"FASTEN_PUBLIC_KEY"}}
---

# Fasten Connect — Patient Health Record Ingestion

Connects patient-authorized real EHR data into HealthClaw Guardrails via
[Fasten Connect](https://docs.connect.fastenhealth.com). Once ingested, all
records flow through the full guardrail stack: PHI redaction, audit trail,
step-up authorization, and tenant isolation.

**Two modes:**
- **Standard** — patient authenticates with their EHR portal (Epic, Cerner, Athena, etc.)
- **TEFCA IAS** — single identity verification (CLEAR / ID.me) retrieves records from all
  QHINs the patient has records at, without per-provider logins

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `FASTEN_PUBLIC_KEY` | Yes | Fasten API public key (`public_test_*` or `public_live_*`) |
| `FASTEN_PRIVATE_KEY` | Yes | Fasten API private key — never expose client-side |
| `FASTEN_WEBHOOK_SECRET` | Recommended | Standard-Webhooks HMAC secret from Fasten portal |
| `FASTEN_CURATR_SCAN` | No | Set `true` to run Curatr quality scan after each import |

---

## Integration Flow

### 1. Embed the Fasten Stitch Widget

```html
<!-- Standard mode -->
<fasten-stitch-element public-id="your_public_test_key"></fasten-stitch-element>

<!-- TEFCA IAS mode — identity-verified, multi-provider -->
<fasten-stitch-element public-id="your_public_test_key" tefca-mode="true"></fasten-stitch-element>

<link rel="stylesheet" href="https://stitch.fastenhealth.com/v0.4/bundle.css">
<script src="https://stitch.fastenhealth.com/v0.4/bundle.js"></script>
```

### 2. Handle the Widget Callback

```javascript
document.querySelector('fasten-stitch-element')
  .addEventListener('widget.complete', async (event) => {
    const { org_connection_id, endpoint_id, tefca_directory_id,
            platform_type, connection_status } = event.detail;

    // Register connection with HealthClaw Guardrails
    await fetch('/fasten/connections', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Tenant-Id': YOUR_TENANT_ID,
      },
      body: JSON.stringify({
        org_connection_id,
        endpoint_id,
        tefca_directory_id,  // use this as stable ID in TEFCA mode
        platform_type,
        connection_status,
      }),
    });
  });
```

### 3. Configure Your Webhook

In the [Fasten Developer Portal](https://app.connect.fastenhealth.com), set your webhook URL:

```
https://your-domain.com/fasten/webhook
```

The webhook fires `patient.ehi_export_success` when the export is ready.
HealthClaw automatically downloads and ingests the FHIR NDJSON files.

### 4. Trigger an EHI Export (optional — Fasten may auto-trigger)

```bash
curl -X POST https://api.connect.fastenhealth.com/v1/bridge/fhir/ehi-export \
  -u "public_test_XXX:private_test_XXX" \
  -H "Content-Type: application/json" \
  -d '{"org_connection_id": "your-org-connection-id"}'
```

### 5. Monitor Ingestion

```bash
# List jobs for your tenant
curl /fasten/jobs -H "X-Tenant-Id: tenant-001"

# Poll a specific job
curl /fasten/jobs/<task_id> -H "X-Tenant-Id: tenant-001"
```

Job status lifecycle: `pending` → `downloading` → `ingesting` → `complete` | `failed`

---

## HealthClaw API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/fasten/webhook` | HMAC (Fasten-signed) | Receive Fasten events |
| `POST` | `/fasten/connections` | `X-Tenant-Id` | Register connection after Stitch widget |
| `GET` | `/fasten/connections/<id>` | `X-Tenant-Id` | Connection status |
| `GET` | `/fasten/jobs` | `X-Tenant-Id` | List ingestion jobs |
| `GET` | `/fasten/jobs/<task_id>` | `X-Tenant-Id` | Single job status |

---

## Guardrails Applied to Ingested Data

Fasten imports pass through the full HealthClaw guardrail stack. Once
a resource is ingested, all existing guardrails apply automatically:

| Guardrail | Behavior |
|---|---|
| PHI redaction | Applied on every read — names to initials, identifiers masked, addresses stripped |
| Audit trail | Every resource write creates an immutable AuditEvent |
| Tenant isolation | `org_connection_id` is bound to one `tenant_id` at registration |
| Curatr scan | Optional: runs quality evaluation post-import (`FASTEN_CURATR_SCAN=true`) |

---

## TEFCA IAS Specifics

- Enable with `tefca-mode="true"` on the Stitch widget
- Use `tefca_directory_id` (not `endpoint_id`) as the stable identifier
- Scope is always `patient/*.read` — no narrower scope negotiation
- Identity verification via CLEAR (phone + email) or ID.me (username/password)
- May return `tefca_no_documents_found` when health systems have no records
- **Additional live-mode fees apply** — test with synthetic patients first

### TEFCA Test Patients

```
# CLEAR verification (test mode)
Phone: (from Fasten test patient credentials page)
Email: (from Fasten test patient credentials page)

# ID.me verification (test mode)
Username: (test patient email)
Password: IDme2026!!

# CCDA fixture override (API-mode testing)
POST /v1/bridge/fhir/ehi-export
{"org_connection_id": "...", "fixtures": {"tefca_ccda": "myra-jones.xml"}}
```

---

## Post-Import Curatr Workflow

After ingestion completes, run Curatr evaluation on clinical resources:

```
1. GET /fasten/jobs/<task_id>          → wait for status: "complete"
2. fhir.search(patient, Condition)     → list ingested Conditions
3. curatr.evaluate(Condition, id)      → check each for coding issues
4. Present issues to patient in plain language
5. Patient approves fixes
6. curatr.apply_fix(resource, fixes)   → apply with Provenance trail
```

Curatr checks ingested resources for:
- Deprecated code systems (ICD-9-CM flagged as critical)
- Invalid RxNorm, LOINC, SNOMED, CVX codes
- Missing US Core required fields (clinicalStatus, verificationStatus, etc.)

---

## Webhook Events

| Event | Trigger | Action |
|---|---|---|
| `patient.ehi_export_success` | Export ready | Auto-download + ingest |
| `patient.ehi_export_failed` | Export failed | Record failure reason |
| `patient.authorization_revoked` | Consent expired | Mark connection revoked |
| `patient.connection_success` | Widget complete | Optional auto-register |
| `webhook.test` | Config test | Accepted silently |

**Note:** Never log raw webhook payloads — `patient.request_support` events
explicitly may contain PHI per Fasten documentation.

---

## File Size Handling

Fasten exports range from ~30MB to 3GB+. HealthClaw uses HTTP streaming
(httpx `stream()`) to handle large files without loading them into memory.
Progress is committed to the database every 50 resources.

**Vercel limitation:** Serverless functions timeout before large downloads complete.
Railway or any persistent server deployment is required for production use with
Fasten Connect. See `railway.toml` for Railway deployment config.

---

## Setup

```bash
# Local development
export FASTEN_PUBLIC_KEY=public_test_XXX
export FASTEN_PRIVATE_KEY=private_test_XXX
export FASTEN_WEBHOOK_SECRET=whsec_XXX  # from Fasten Developer Portal
export FASTEN_CURATR_SCAN=true

docker-compose up -d --build

# Test webhook locally (use Fasten webhook simulator or ngrok)
# Fasten Webhook Simulator: https://docs.connect.fastenhealth.com/guides/webhook-debugging-simulator.md
```

## Known Limitations

- Background download uses a daemon thread — if the process restarts mid-download,
  the job remains in `downloading` status (re-trigger by calling the Fasten API again)
- Download links expire after 24 hours — webhook triggers immediate download
- TEFCA live mode has additional per-use fees (test mode is free with synthetic patients)
- Upstream tenant isolation is local only — Fasten does not enforce tenant boundaries
