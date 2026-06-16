# Security

HealthClaw Guardrails is a reference implementation of security and compliance
patterns for AI-agent access to health data. Security is the product, so we hold
the codebase to the bar we advocate.

## Reporting a vulnerability

Email **security@vestelai.com** (operated by Vestel AI LLC). Please include a
description, reproduction steps, and impact. We aim to acknowledge within 3
business days and will coordinate disclosure. Please do not open public issues
for security reports.

## Security posture

The guardrail stack runs on every request:

| Control | What it does |
| --- | --- |
| **PHI redaction** | Reads pass through HIPAA Safe Harbor or patient-controlled redaction before leaving the store. |
| **Tenant isolation** | Every database query is scoped to the requested tenant at the query layer; cross-tenant access is blocked. |
| **Tenant-authenticated reads** | Hosted deployments handling real records require a tenant-bound token on reads (config: `READ_AUTH_ENABLED`); synthetic/demo tenants remain open. |
| **Step-up authorization** | Writes require an HMAC-signed, tenant-bound, expiring token. |
| **Human-in-the-loop** | Clinical writes and real-world actions return HTTP 428 until a human confirms (`X-Human-Confirmed`). |
| **Append-only audit** | Every read/write/action is logged immutably (no UPDATE/DELETE on audit rows, enforced at the ORM layer). |
| **Transport & headers** | HTTPS; HSTS, CSP, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy` on every response. |
| **Abuse controls** | Per-tenant/per-IP rate limiting, request payload caps, and replay-resistant tokens. |

## Deployment hardening

Production deployments should set:

- `READ_AUTH_ENABLED=true` and `PUBLIC_TENANTS=<demo tenants only>` — authenticate reads for real tenants.
- `STEP_UP_SECRET` — strong random secret for HMAC tokens.
- `INTERNAL_TOKEN_MINT_SECRET` — locks the internal token-mint endpoint; distribute to trusted minters only.
- `ACTIONS_WEBHOOK_SECRET` — verifies real-world-action provider callbacks.
- Terminate TLS at the edge; run behind a WAF; back the rate-limit/nonce stores with Redis for multi-worker.

## Scope & honest limits

This is a **reference implementation**, not a turnkey production PHI service.
Local mode stores JSON blobs in SQLite. Validation is structural (US Core v9
required fields), not full StructureDefinition conformance or terminology
binding. The hosted demo runs synthetic or patient-directed data. Operators
deploying this to process ePHI are responsible for their own BAA-covered
infrastructure, access governance, and compliance program — see the Terms of
Service.

## Messaging platforms

When records are delivered through consumer chat platforms (Telegram, Slack,
Discord), HealthClaw acts as the **patient's own agent** retrieving the
**patient's own records** to a channel the patient chooses — patient-directed
access under HIPAA's individual right of access, after a clear risk
acknowledgment. These channels are not BAA-covered transport. Notifications are
PHI-free by rule, reads are redacted, and a summary-only mode is available.
