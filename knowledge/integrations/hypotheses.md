# Integrations — Hypotheses (need more data)

## HYP-1: RQ job queue improves reliability for large Fasten imports

**Hypothesis:** Using Redis Queue (RQ) instead of `threading.Thread` for download
jobs prevents job loss on gunicorn worker restart and enables retry logic.

**Evidence for:** Redis is already in docker-compose. RQ is lightweight and well-suited
to this pattern. Worker restart leaves jobs in `downloading` status with no auto-retry.

**Evidence against:** `threading.Thread` is simpler, requires no extra worker process,
and has no additional dependencies. For single-instance deployments (Railway with
one web service), thread loss is rare and acceptable — just re-trigger the export.

**How to test:** Run a 3GB export and kill the gunicorn worker mid-download. Observe
whether the job recovers or stays stuck.

**Confirmation count:** 0 / 5 needed for promotion to rule.

---

## HYP-2: FASTEN_CURATR_SCAN=true adds meaningful latency to import completion

**Hypothesis:** Running Curatr on up to 100 clinical resources after ingestion
adds >30 seconds to the total import time (due to external terminology API calls
to tx.fhir.org, NLM, RXNAV).

**Evidence for:** Each Curatr evaluation makes up to 3 external HTTP calls.
100 resources × 3 calls × ~500ms each = ~150 seconds.

**Evidence against:** Curatr has a 5-second timeout per call and skips unavailable
services gracefully. In practice, many resources won't need terminology lookups.

**How to test:** Time a full import with and without FASTEN_CURATR_SCAN=true on
a real Fasten export with 50+ clinical resources.

**Confirmation count:** 0 / 5 needed for promotion to rule.

---

## HYP-3: patient.connection_success event enables serverless-compatible registration

**Hypothesis:** Enabling the `patient.connection_success` webhook event (disabled by
default in Fasten) allows the backend to register connections server-side without
a frontend /fasten/connections POST call. This would make the integration work on
Vercel if the webhook handler completes quickly (no download triggered at connection time).

**Evidence for:** The event payload includes org_connection_id and could include
a custom tenant_id if Fasten supports custom webhook payloads.

**Evidence against:** The event doesn't naturally carry tenant_id — Fasten has no
concept of tenants. The tenant must be injected somehow (e.g., via a Stitch widget
metadata parameter passed through to the webhook).

**How to test:** Check if Fasten Stitch widget supports custom metadata that flows
through to webhook payloads. If yes, this pattern is viable for serverless.

**Confirmation count:** 0 / 5 needed for promotion to rule.
