# Integrations — Facts and Patterns

Last updated: 2026-03-29

## Fasten Connect

### What It Is

Patient-mediated FHIR R4 bulk export platform. Patients authorize access to their
EHR systems (Epic, Cerner, Athena) via the Fasten Stitch web widget or React Native SDK.
Backend receives FHIR R4 NDJSON export files via webhook after patient auth.

### API Surface

| Item | Detail |
|---|---|
| Auth | HTTP Basic — `public_*` + `private_*` API keys |
| Webhook spec | Standard-Webhooks (HMAC-SHA256, `whsec_` prefixed secret) |
| Initiate export | `POST https://api.connect.fastenhealth.com/v1/bridge/fhir/ehi-export` |
| Poll status | `GET /v1/bridge/fhir/ehi-export/{org_connection_id}` |
| Download | `GET /v1/bridge/fhir/ehi-export/{task_id}/download/{filename}` |
| File format | FHIR R4 JSONL / NDJSON |
| File size range | 30MB – 3GB+ |
| Download TTL | 24 hours (link expires ~10 min after webhook fires) |
| Idempotent | Yes — duplicate `org_connection_id` returns existing job |

### TEFCA IAS

- Enable with `tefca-mode="true"` on Stitch widget
- Identity verification: CLEAR (phone+email) or ID.me (username/password: `IDme2026!!` in test)
- No per-provider portal logins — single verification pulls from all QHINs
- Scope always: `patient/*.read`
- Use `tefca_directory_id` as stable identifier (not `endpoint_id`/`portal_id`/`brand_id`)
- May fail: `tefca_no_documents_found` when health system has no records
- Live mode has additional per-use fees

### Webhook Events

| Event | Trigger | HealthClaw action |
|---|---|---|
| `patient.ehi_export_success` | Export ready | Start stream_ingest() thread |
| `patient.ehi_export_failed` | Export failed | Record failure_reason in FastenJob |
| `patient.authorization_revoked` | Consent expired | Set connection_status = 'revoked' |
| `patient.connection_success` | Widget complete | Optional auto-register connection |
| `webhook.test` | Config test | Accept silently |

**PHI warning:** `patient.request_support` events explicitly may contain PII/PHI.
Never log raw webhook payloads.

### HealthClaw Integration Architecture

```text
Patient ──[Stitch Widget]──> Fasten Connect API
                                     │ webhook
                              POST /fasten/webhook
                                     │ verify HMAC
                              FastenJob created
                                     │ daemon thread
                              stream_ingest()
                                     │ httpx.stream()
                              _ingest_one() × N resources
                                     │ R6Resource + AuditEvent
                              FHIR data available via MCP
```

### Key Implementation Decisions

**Threading over RQ:** Background download uses `threading.Thread` with daemon=True.
Simpler (no extra worker process), works on Railway and Docker Compose. Trade-off:
if gunicorn worker dies mid-download, job stays in `downloading` status. Acceptable
for v1 — retry by re-triggering the Fasten export API.

**Streaming, not buffering:** `httpx.stream()` with `iter_lines()` handles
30MB–3GB files without memory pressure. Progress committed every 50 resources.

**Curatr scan opt-in:** `FASTEN_CURATR_SCAN=true` triggers quality evaluation
on ingested Condition, AllergyIntolerance, MedicationRequest, Immunization,
Procedure, DiagnosticReport — capped at 100 resources per import to limit latency.

**Tenant binding:** `org_connection_id` is bound to `tenant_id` at registration
(POST /fasten/connections). Webhook handler looks up tenant from FastenConnection.
If connection not registered, webhook is logged and silently dropped.

### Files Created

```text
r6/fasten/__init__.py       Blueprint export
r6/fasten/models.py         FastenConnection, FastenJob models
r6/fasten/verify.py         Standard-Webhooks HMAC verification (no library dep)
r6/fasten/ingester.py       stream_ingest() + _ingest_one() + _run_curatr_scan()
r6/fasten/routes.py         Blueprint: /fasten/webhook, /connections, /jobs
skills/fasten-connect/SKILL.md  OpenClaw/Claude skill (compliant)
railway.toml                Railway deployment config
```

### Modified Files

```text
main.py              Import Fasten models + register fasten_blueprint
docker-compose.yml   FASTEN_PUBLIC_KEY, FASTEN_PRIVATE_KEY, FASTEN_WEBHOOK_SECRET,
                     FASTEN_CURATR_SCAN env vars added
tests/test_r6_dashboard.py  Two tests updated for dashboard rename to "Health Data Dashboard"
```
