# Integrations — Confirmed Rules

## RULE-1: Never log raw webhook payloads from Fasten

Fasten's `patient.request_support` events explicitly may contain PII/PHI.
Log only the event `type` field. Store only the `failure_reason` category (truncated),
never the full failure payload.

## RULE-2: Bind org_connection_id to tenant_id at registration, not at webhook time

The webhook arrives without tenant context. The tenant mapping must be established
when the Stitch widget completes (POST /fasten/connections from the frontend).
If no mapping exists when a webhook arrives, drop it with a warning — never infer tenant.

## RULE-3: Use httpx streaming for all FHIR NDJSON downloads

Fasten exports are 30MB–3GB+. Never use `response.json()` or `response.read()`.
Use `httpx.stream()` with `iter_lines()`. Commit DB progress every N resources
(default 50) to avoid long transactions.

## RULE-4: Return 200 from the webhook handler immediately; download in a thread

Fasten expects a fast 200 response. Download threads must be daemon=True so they
don't block process shutdown. The FastenJob status tracks progress independently.

## RULE-5: Use tefca_directory_id as the stable identifier in TEFCA mode

In TEFCA mode, `endpoint_id`, `brand_id`, and `portal_id` are often absent or
unreliable. Always store and reference `tefca_directory_id` for TEFCA connections.

## RULE-6: Railway (or persistent server) is required for Fasten Connect in production

Vercel serverless functions have 60s max timeout. Fasten downloads can take minutes.
Any persistent server (Railway, Render, Fly.io, Docker Compose on VPS) works.
Vercel can only be used for the web UI; the FHIR API + Fasten webhook must run elsewhere.
