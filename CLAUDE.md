# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A **reference implementation** of security and compliance patterns for AI agent access to
FHIR data via Model Context Protocol (MCP). Version 1.3.0. A [healthclaw.io](https://healthclaw.io) project.

**Supports:**

- **FHIR R4 with US Core v9** profiles — the production standard (stable, widely deployed US healthcare resources). This is what the app primarily does.
- FHIR R6 v6.0.0-ballot3 (experimental ballot resources only: Permission, SubscriptionTopic, DeviceAlert, NutritionIntake)

**Why the `/r6/` route prefix:** The Flask Blueprint and directory are named `r6` from the project's origin as an R6 ballot resource showcase. The actual clinical data pipeline (Conditions, Observations, Immunizations, MedicationRequests, etc.) uses **R4 resources validated against US Core v9 required fields**. The R6 prefix is a route path, not a statement about which FHIR version the clinical resources use.

**What this is:** A pattern library showing how tenant isolation, step-up authorization,
audit trails, PHI redaction, and human-in-the-loop enforcement work together when an
AI agent accesses clinical data through MCP tools.

**What this is NOT:** A production FHIR server. In local mode, resources are stored as
JSON blobs in SQLite. In upstream proxy mode, real FHIR server data flows through the
guardrail stack. Validation is structural only (required fields + value constraints,
no StructureDefinition conformance, no terminology binding).

## Architecture

```text
┌─────────────────────────────────────────────────┐
│  Flask App (Python)                              │
│  ├── /r6/fhir/* — FHIR REST facade (Blueprint)   │
│  ├── /r6/fhir/health — Liveness probe           │
│  ├── /r6/fhir/oauth/* — OAuth 2.1 + SMART       │
│  ├── /fasten/* — Fasten Connect EHR integration  │
│  ├── / — Landing page                            │
│  ├── /skills — Auto-indexed skill catalogue      │
│  ├── /api/subscribe — Resend signup + welcome    │
│  └── /r6-dashboard — Interactive dashboard       │
├─────────────────────────────────────────────────┤
│  MCP Server (Node.js + TypeScript)               │
│  ├── Streamable HTTP (/mcp) — primary transport  │
│  ├── SSE (/sse + /messages) — legacy transport   │
│  ├── HTTP Bridge (/mcp/rpc) — non-MCP clients   │
│  └── Session management + CORS deny-by-default   │
├─────────────────────────────────────────────────┤
│  Data Source (configurable):                     │
│  ├── LOCAL: JSON blobs in SQLite (default)       │
│  └── UPSTREAM: Real FHIR server via httpx proxy  │
│       (HAPI, SMART Health IT, Epic, etc.)        │
│       Guardrails applied to upstream responses   │
├─────────────────────────────────────────────────┤
│  Guardrail Stack (always active):                │
│  ├── PHI redaction on all read paths             │
│  ├── Immutable audit trail                       │
│  ├── Step-up tokens for writes                   │
│  ├── Tenant isolation on every query             │
│  └── URL rewriting (upstream URLs never leak)    │
├─────────────────────────────────────────────────┤
│  Cache: Redis (optional, rate limiting+sessions) │
└─────────────────────────────────────────────────┘
```

### Upstream Proxy Flow

```text
Client → MCP Server → Flask (guardrails) → Upstream FHIR Server
                           ↓
              redaction, audit, step-up,
              tenant isolation, disclaimers,
              URL rewriting
```

## Key Directories

```text
/                         Main Flask app (main.py, app.py, models.py)
/api/                     Vercel serverless entry point (index.py wraps Flask WSGI app)
/r6/                      FHIR Python modules (routes, models, validator, oauth, stepup, audit, redaction, health_compliance, context_builder, rate_limit, fhir_proxy, agent_client, health_context, schema_sync, seed, telegram_push). Named r6/ for historical reasons; handles both R4 US Core and experimental R6 resources.
/r6/fasten/               Fasten Connect EHR integration (routes, models, ingester, verify)
/r6/wearables/            Wearable device sync MCP app (Apple Health / Fitbit poller + UI)
/r6/command_center/       Command Center module (per-tenant ops dashboard)
/services/agent-orchestrator/  Node.js MCP server (TypeScript)
/scripts/                 CLI utilities: import_healthex.py, export_healthex.py, export_healthex_mcp.py (MCP-SDK pull from HealthEx), export_healthex_legacy.py, healthclaw_redact.py (in-process PHI redaction), bot_commands.py (OpenClaw slash-command dispatcher), convert_fasten.py, demo_e2e.sh, smoke_test.py, seed_demo_tenant.py (HTTP + DB seed entry points), seed_openclaw_workspaces.sh, update_agent_prompts.sh, kristy_schedule_watcher.py, kristy_install.sh, build_quickstart_pdf.py
/openclaw/                Telegram bot (bot.py + Dockerfile) — conversational interface to the stack
/hermes/                  Hermes (Nous Research) integration — SOUL persona, MCP config, idempotent install.sh that wires HealthClaw skills into ~/.hermes/. Parallel to /openclaw/; both share the same MCP server.
/skills/                  Skill definitions consumable by Hermes (agentskills.io standard) AND OpenClaw — getting-started, curatr, fhir-r6-guardrails, phi-redaction, fhir-upstream-proxy, fasten-connect, healthex-export, healthex-export-redacted, personal-health-records, hermes. Also surfaced at /skills (auto-indexed from frontmatter).
/exports/                 Output directory for export_healthex.py bundles (gitignored)
/templates/               Jinja2 templates (base.html, index.html, r6_dashboard.html, fasten_connect.html, wiki.html, faq.html, skills.html, command_center*.html, privacy.html, terms.html, mcp_apps/*)
/static/css/              Dashboard styles (r6-dashboard.css)
/static/js/               Dashboard JavaScript (r6-dashboard.js)
/tests/                   Python tests (531 passing) — conftest.py + test_r6_routes, test_r6_dashboard, test_context_builder, test_fhir_proxy, test_us_core_r4, test_curatr, test_bot_commands, test_command_center, test_export_healthex (legacy), test_healthclaw_redact (MCP export + redaction), test_import_healthex, test_kristy_watcher, test_public_fhir_servers, test_wearables, test_subscribe (Resend signup + welcome email), test_skills_page (/skills route), test_sharp_on_mcp (SHARP context headers + per-request upstream proxy), test_telegram_connect_flow (bind-telegram, /connect/<tenant> render, notify_tenant fan-out)
/e2e/                     Playwright end-to-end tests (landing.spec.ts, dashboard.spec.ts)
/.github/workflows/       CI configuration (ci.yml)
/.claude/rules/           Claude Code rules (build.md, security.md)
/.claude/compliance/      HIPAA / SOC2 / HITRUST gate checklists (hipaa.md, soc2.md, hitrust.md)
/.mcp/                    MCP server manifest (server.json) — server-side tool registry
/.mcp.json                Claude Desktop client config (HTTP transport + X-Tenant-ID header)
/INTEGRATION.md           Setup guides for SmartHealthConnect, Medplum, and upstream server testing
```

### Template notes

- `templates/index.html` is a **standalone page** — it does NOT `{% extends "base.html" %}`. It has its own `<html>`, nav, and footer. All other templates extend `base.html`.
- Flask route names for `url_for()`: `index`, `r6_dashboard`, `wiki`, `faq`, `privacy`, `terms`, `skills_index` (not `skills`), `fasten_connect`.

## Build & Run Commands

Requires **Python 3.11+** and **Node 18+**.

```bash
# Install Python dependencies
uv sync

# Run Flask app (development, local mode)
python main.py

# Run Flask app with upstream FHIR server
FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4 python main.py

# Run all Python tests
uv run python -m pytest tests/ -v

# Run a single test file or specific test
uv run python -m pytest tests/test_r6_routes.py -v
uv run python -m pytest tests/test_r6_routes.py::test_function_name -v

# Agent orchestrator
cd services/agent-orchestrator && npm ci && npm test

# TypeScript compile check (no emit)
cd services/agent-orchestrator && npx tsc --noEmit

# Playwright end-to-end tests (requires Flask running on :5000)
cd e2e && npm ci && npx playwright install --with-deps chromium && npm test

# Playwright with headed browser (for debugging)
cd e2e && npm run test:headed

# Playwright interactive UI mode
cd e2e && npm run test:ui

# Docker Compose (full stack)
docker-compose up -d --build

# Docker Compose with upstream FHIR server
FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4 docker-compose up -d --build

# Vercel (production deployment at healthclaw.io)
vercel deploy --prod                     # deploy current branch to production
vercel logs healthclaw.io                # tail runtime logs
vercel dns ls healthclaw.io              # inspect DNS records
vercel project ls                        # list all projects

# Railway (full-stack with Redis + MCP server)
railway login && railway up              # deploy to Railway

# Scripts — personal health data pipeline
python scripts/export_healthex.py --tenant-id ev-personal --dry-run
python scripts/export_healthex.py --tenant-id ev-personal --import --step-up-secret $STEP_UP_SECRET
python scripts/import_healthex.py --bundle-file my.json --tenant-id ev-personal --step-up-secret $STEP_UP_SECRET
python scripts/convert_fasten.py --input health-records*.json --output bundle.json

# Telegram bot (requires TELEGRAM_BOT_TOKEN)
docker-compose --profile openclaw up -d
```

## Environment Variables

### Flask Server

| Variable | Default | Description |
| --- | --- | --- |
| `SQLALCHEMY_DATABASE_URI` | SQLite `mcp_server.db` | Database URL; use PostgreSQL in production |
| `STEP_UP_SECRET` | (required in prod) | HMAC secret for step-up tokens; auto-generated on Vercel |
| `ANTHROPIC_API_KEY` | — | Claude API key for agent client |
| `REDIS_URL` | `redis://redis:6379/0` | Redis for rate limiting + sessions |
| `FHIR_UPSTREAM_URL` | (empty) | Enables proxy mode when set |
| `FHIR_UPSTREAM_TIMEOUT` | `15` | HTTP timeout for upstream requests (seconds) |
| `FHIR_LOCAL_BASE_URL` | (empty) | Local server URL for URL rewriting in responses |
| `FHIR_VALIDATOR_URL` | `http://localhost:8080` | Optional external FHIR validator |
| `SESSION_SECRET` | dev default | Flask session key |
| `LOG_LEVEL` | `DEBUG` (dev) | Log verbosity |
| `LOG_FORMAT` | — | Set to `json` for structured logging in production |
| `TELEGRAM_BOT_TOKEN` | — | Set on Flask so `r6.telegram_push.notify_tenant` can push post-ingest notifications directly via the Telegram Bot API (no IPC with OpenClaw). Same token OpenClaw polls with. |
| `DASHBOARD_BASE_URL` | `https://healthclaw.io` | Public base URL OpenClaw advertises in `/connect` / `/dashboard` replies. Set to the host that serves `/connect/<tenant_id>` and the dashboard. |

### Fasten Connect

| Variable | Default | Description |
| --- | --- | --- |
| `FASTEN_PUBLIC_KEY` | — | Stitch widget public key; exposes `<fasten-stitch-element>` in dashboard + `/connect/<tenant>` when set |
| `FASTEN_PRIVATE_KEY` | — | Webhook verification secret |
| `FASTEN_WEBHOOK_SECRET` | — | Standard-Webhooks signing secret (starts `whsec_`) used by `verify_webhook` |
| `FASTEN_CURATR_SCAN` | `false` | Auto-run Curatr evaluation after ingest; issue count surfaces in the Telegram post-ingest notification |
| `FASTEN_TEFCA_MODE` | `true` | When truthy, `/connect/<tenant>` renders the Stitch widget with `tefca-mode="true"` so one CLEAR/ID.me verification pulls across all QHINs |

### Newsletter sign-up (Resend)

| Variable | Default | Description |
| --- | --- | --- |
| `RESEND_API_KEY` | — | Resend secret API key. Without this, `POST /api/subscribe` returns 503. |
| `RESEND_AUDIENCE_ID` | — | Resend Audience UUID; sign-ups become contacts in this audience. |

The landing page form (`#subscribe-form`) POSTs to `/api/subscribe`, which calls
`POST https://api.resend.com/audiences/{id}/contacts`. Sending domain is
`updates@healthclaw.io` — uses the same DNS already verified for the
`@healthclaw.io` aliases (privacy/security/legal). Duplicates surface as a 200
with `already_subscribed: true` so the UI shows a friendly "already on the list"
message rather than an error.

After a fresh contact is created (Resend 200/201), `_send_welcome_email()` fires
a transactional email via `POST https://api.resend.com/emails` with
`static/healthclaw-quickstart.pdf` base64-attached. HTML + plaintext fallback,
subject "Your HealthClaw quickstart is here". Failures (5xx, network) are
logged and swallowed — the contact is the load-bearing thing and is already
saved by the time email fires. Duplicates do **not** trigger a re-send.

The PDF is the build output of `scripts/build_quickstart_pdf.py` (reportlab,
14 pages, Path A non-technical + Path B self-host). Re-run that script to
refresh the artifact when content changes.

### MCP Orchestrator (`services/agent-orchestrator/`)

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_PORT` | `3001` | Server port |
| `FHIR_BASE_URL` | `http://localhost:5000/r6/fhir` | Backend FHIR endpoint |
| `ALLOWED_ORIGINS` | (empty = deny-all) | Comma-separated CORS allowlist |
| `RATE_LIMIT_MAX` | `120` | Max requests per minute per IP |
| `TENANT_ID` | `desktop-demo` | Default tenant when `X-Tenant-ID` header is absent (e.g. Claude Desktop) |

### Medplum Upstream Proxy (optional)

| Variable | Default | Description |
| --- | --- | --- |
| `MEDPLUM_BASE_URL` | — | Medplum project base URL (e.g. `https://api.medplum.com/fhir/R4/<project-id>`) |
| `MEDPLUM_CLIENT_ID` | — | OAuth2 client ID |
| `MEDPLUM_CLIENT_SECRET` | — | OAuth2 client secret |

When `MEDPLUM_BASE_URL` is set and `FHIR_UPSTREAM_URL` is not, the proxy uses `MedplumProxy` (a subclass of `FHIRUpstreamProxy` in `r6/fhir_proxy.py`) that fetches OAuth2 tokens via client-credentials grant. Tokens are cached in Redis (key `medplum:access_token`, TTL = `expires_in - 60s`) with in-process fallback.

### Openclaw Telegram Bot (`openclaw/`)

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | (required) | BotFather token. Same value must be set on Flask service so post-ingest pushes work. |
| `TENANT_ID` | `desktop-demo` | Tenant to query and bind chats to on `/start` |
| `MCP_BASE_URL` | `http://localhost:3001` | MCP HTTP bridge base URL |
| `FHIR_BASE_URL` | `http://localhost:5000/r6/fhir` | Flask FHIR base URL |
| `STEP_UP_SECRET` | — | HMAC secret for step-up tokens. Must match Flask service so `/start` → `/internal/bind-telegram` auth succeeds. |
| `DASHBOARD_BASE_URL` | `https://healthclaw.io` | Used to build the `/connect/<tenant>` URL `/connect` replies with. |

## Upstream FHIR Proxy

Connect to real FHIR servers while keeping the full guardrail stack active.

### Tested Upstream Servers

| Server | URL | Auth |
| --- | --- | --- |
| HAPI FHIR R4 | `https://hapi.fhir.org/baseR4` | None (open) |
| SMART Health IT | `https://r4.smarthealthit.org` | None (open) |
| HAPI FHIR R5 | `https://hapi.fhir.org/baseR5` | None (open) |
| Local HAPI | `http://localhost:8080/fhir` | None |
| Epic Sandbox | `https://open.epic.com/Interface/FHIR` | OAuth 2.0 |
| Medplum | `https://api.medplum.com/fhir/R4/<project-id>` | OAuth2 client-credentials (auto) |

### What the Proxy Does

- **Reads**: Fetched from upstream, then redacted + audited + disclaimers added
- **Searches**: Forwarded to upstream with all query params, results redacted per entry
- **Writes**: Validated locally first, then forwarded to upstream with step-up auth check
- **URL rewriting**: All upstream URLs in responses replaced with local proxy URLs
- **Health check**: `/r6/fhir/health` reports upstream connection status
- **Graceful fallback**: Network errors return proper OperationOutcome, not stack traces

### What the Proxy Does NOT Do

- No caching of upstream responses (every request hits the server)
- No SMART-on-FHIR auth forwarding to upstream (uses upstream's native auth model)
- No cross-version translation (R4 responses stay R4)
- Tenant isolation is enforced locally, not on the upstream server

## What's R6-Specific (Experimental Ballot Only)

- **Permission** — R6 access control resource (separate from Consent). $evaluate operation.
- **SubscriptionTopic** — Restructured pub/sub (introduced R5, maturing R6). Storage + discovery only, no notification dispatch.
- **DeviceAlert** — ISO/IEEE 11073 device alarms (new in R6).
- **NutritionIntake** — Dietary consumption tracking (new in R6).
- **DeviceAssociation, NutritionProduct, Requirements, ActorDefinition** — Additional R6 resources (CRUD only).

## What's US Core v9 R4 (Stable)

Standard R4 resources added in Phase 4. All validated against US Core v9 required fields:

- **AllergyIntolerance** — requires clinicalStatus, verificationStatus, patient
- **Immunization** — requires status, vaccineCode, patient, occurrence[x]
- **MedicationRequest** — requires status, intent, medication[x], subject
- **Procedure** — requires status, code, subject
- **DiagnosticReport** — requires status, code, subject
- **DocumentReference** — requires status, subject, content
- **Coverage** — requires status, beneficiary, payor
- **ServiceRequest** — requires status, intent, subject
- **Goal** — requires lifecycleStatus, description, subject
- **CarePlan** — requires status, intent, subject
- **Location, Organization, Practitioner, PractitionerRole, RelatedPerson, CareTeam, Specimen, FamilyMemberHistory** — CRUD only

Curatr evaluates: AllergyIntolerance, MedicationRequest, Immunization, Procedure, DiagnosticReport (in addition to Condition).

## What's Standard FHIR (Not R6-Specific)

- **$stats** — Observation statistics (count/min/max/mean). Available since R4. Only supports valueQuantity.
- **$lastn** — Most recent observations per code. Available since R4. Sorted by storage order, not effectiveDateTime.
- **$validate** — Structural validation only. Falls back silently if external validator unavailable.
- **$deidentify** — Custom operation, not part of FHIR spec. Supports `?mode=hipaa-safe-harbor` (default: strips name/address/identifiers/birthDate) and `?mode=patient-controlled` (preserves birthDate, strips only institutional identifiers, stamps `urn:healthclaw:patient` canonical ID).

## Search Capabilities (Honest)

In **local mode**: Supported parameters: `patient` (reference), `code` (token), `status` (token), `_lastUpdated` (date with ge/le/gt/lt prefix), `_count` (1-200), `_sort` (`_lastUpdated`/`-_lastUpdated`), `_summary` (count). NOT supported: chaining, `_include`, `_revinclude`, modifiers.

In **upstream proxy mode**: All query parameters forwarded to the upstream server. The upstream server's full search capabilities are available (chaining, _include, etc. if the upstream supports them).

## MCP Tools (16)

- **Read tools** (no step-up): `context_get`, `fhir_read`, `fhir_search`, `fhir_validate`, `fhir_stats`, `fhir_lastn`, `fhir_permission_evaluate`, `fhir_subscription_topics`, `curatr_evaluate`
- **Write tools** (require step-up token): `fhir_propose_write`, `fhir_commit_write`, `curatr_apply_fix`
- **Compiled-truth tool**: `fhir_compiled_truth` — returns redacted current resource + curation state + Provenance timeline (the v1.2.0 flagship; backs the `mcp-apps/compiled-truth` UI)
- **Utility tools**: `fhir_get_token` (issues a 5-min step-up token; call before any write), `fhir_seed` (seeds a tenant with demo Patient + Observations + Condition)
- All tool names use underscores (`fhir_search`, not `fhir.search`) — dots are not valid in some MCP clients
- Tools add `_mcp_summary` with reasoning, clinical context, and limitations
- `fhir_propose_write` identifies clinical types requiring human-in-the-loop
- `fhir_permission_evaluate` returns reasoning explaining why permit/deny
- Result entries capped at 50 to stay within token limits

### MCP Transport Header Forwarding

`X-Tenant-ID` must reach Flask on every tool call. The MCP server uses this priority order:

1. `X-Tenant-ID` HTTP header on the incoming MCP request
2. `TENANT_ID` environment variable
3. `"desktop-demo"` hardcoded fallback

For **Streamable HTTP** (`POST /mcp`): headers re-extracted per request — works transparently.
For **SSE** (`GET /sse`): headers captured at connection time and bound to the session via `createMCPServer(sessionHeaders)` — the session's tenant is fixed at connect time.
For **HTTP bridge** (`POST /mcp/rpc`): headers extracted per request — same as Streamable HTTP.

Step-up tokens (`X-Step-Up-Token`) follow the same forwarding path. When calling write tools via Claude Desktop (no HTTP headers available), pass the token as `_stepUpToken` in the tool arguments — it is extracted before execution.

## SHARP-on-MCP + PromptOpinion FHIR Context

The MCP server advertises **two parallel FHIR-context shapes** in its `initialize` response so it auto-detects as compliant on both ecosystems:

- **[SHARP-on-MCP](https://sharponmcp.com)** → `capabilities.experimental.fhir_context_required` + `capabilities.experimental.sharp` — vendor-neutral header-forwarding contract.
- **[PromptOpinion FHIR Extension](https://docs.promptopinion.ai/fhir-context/mcp-fhir-context)** → `capabilities.extensions["ai.promptopinion/fhir-context"]` declaring SMART-on-FHIR scopes (`patient/*.read` required, `patient/*.write` / `offline_access` optional).

Both specs converge on the same per-request headers:

| Header | Sent by agent host | Effect |
| --- | --- | --- |
| `X-FHIR-Server-URL` | Always when SHARP context is active | MCP server forwards to Flask; Flask builds a transient `FHIRUpstreamProxy` per request (see `r6.fhir_proxy.get_proxy_for_request`) |
| `X-FHIR-Access-Token` | When agent host holds a SMART token | Set as `Authorization: Bearer …` on the upstream proxy client |
| `X-Patient-ID` | Optional | Forwarded; not currently used to constrain queries |
| `X-FHIR-Refresh-Token` / `X-FHIR-Refresh-Url` | When `offline_access` was granted | Forwarded but not yet acted on |

**Per-request proxy lifecycle**: the SHARP proxy is created lazily on first call to `get_proxy_for_request()`, cached on `flask.g._sharp_proxy`, and closed by the `teardown_request` handler `close_request_proxy` registered on the blueprint. When SHARP headers are absent, the singleton env-var proxy is returned (FHIR_UPSTREAM_URL / MEDPLUM_BASE_URL), or `None` for local mode.

**Synthetic tenant fallback**: SHARP requests may omit `X-Tenant-ID`. When the SHARP server URL is present and the tenant header is missing, the `enforce_tenant_id` `before_request` derives a stable `sharp-<sha256-prefix-of-server-url>` tenant so audit + redaction scope correctly.

**Tool-arg overrides** (for clients that can't set HTTP headers like Claude Desktop): `_fhirServerUrl`, `_fhirAccessToken`, `_patientId` on any tool's `arguments` are lifted into the equivalent headers before the tool runs. Same pattern as `_stepUpToken` / `_tenantId` / `_authorization`.

The MCP server is publicly hosted at `https://mcp-server-published-5112.up.railway.app/mcp` (Railway service `mcp-server`, `FHIR_BASE_URL=https://app.healthclaw.io/r6/fhir`). PromptOpinion marketplace listings (Agent + Superpower) point at this URL.

## Telegram ↔ Fasten Connect Flow

End-to-end "type `/connect` in Telegram → records arrive → review in chat" flow, used for live patient-empowerment demos. Components:

| Piece | Where |
| --- | --- |
| `TelegramBinding` model — `(tenant_id, chat_id)` with `bind()` / `chat_ids_for_tenant()` classmethods | `r6/models.py` |
| `r6.telegram_push.notify_tenant(tenant_id, message)` | `r6/telegram_push.py` — calls `api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage` directly; no IPC with OpenClaw |
| `POST /r6/fhir/internal/bind-telegram` (step-up gated) | `r6/routes.py` — OpenClaw `/start` calls this; auth is `validate_step_up_token` which returns `(bool, str)` — destructure both, never coerce the tuple to bool |
| `GET /connect/<tenant_id>` | `app.py` — renders `templates/fasten_connect.html` with the Stitch widget; respects `FASTEN_TEFCA_MODE` (default true) |
| `templates/fasten_connect.html` | Lean page — no nav chrome; renders a config-missing warning when `FASTEN_PUBLIC_KEY` is unset rather than failing silently |
| `widget.complete` → `POST /fasten/connections` with `X-Tenant-Id` | Inline JS in `fasten_connect.html` |
| Post-ingest push | `r6/fasten/ingester.py::stream_ingest` calls `notify_tenant` on success and failure with summary counts (no PHI). `_run_curatr_scan` now returns the issue count so the success message can include it. |
| OpenClaw `/start` | Binds chat to `TENANT_ID` via `_bind_chat_to_tenant` → POSTs to `/internal/bind-telegram` |
| OpenClaw `/connect` | Replies with `{DASHBOARD_BASE_URL}/connect/{TENANT_ID}` — no token in the URL; security is via the Stitch widget itself |

Flow:

```text
/start in TG → openclaw → POST /internal/bind-telegram → TelegramBinding row
/connect in TG → openclaw replies with /connect/<tenant> URL
user opens URL → Stitch widget loads (TEFCA mode by default)
widget.complete → POST /fasten/connections (tenant scoped)
Fasten back-channel → POST /fasten/webhook → background stream_ingest
ingest complete → r6.telegram_push.notify_tenant(tenant) → DM every bound chat
```

## Test Fixtures (`tests/conftest.py`)

The conftest provides an in-memory SQLite Flask test app and these fixtures:

- `client` — Flask test client
- `tenant_id` — Standard test tenant string
- `step_up_token` / `auth_headers` — HMAC-signed write auth headers
- `tenant_headers` — Read-only tenant headers
- Sample resources: `sample_patient`, `sample_observation`, `sample_bundle`, `sample_permission`, `sample_subscription_topic`, `sample_nutrition_intake`, `sample_device_alert`

## Security Patterns (What's Real)

- **Tenant isolation** — Enforced at database layer on every query (local mode) or as a guardrail header (proxy mode)
- **Step-up tokens** — HMAC-SHA256 with 128-bit nonce for write authorization
- **OAuth 2.1 + PKCE** — S256 only, dynamic client registration, token revocation
- **PHI redaction** — Applied on all read paths including upstream responses (names truncated to initials, identifiers masked, addresses stripped, birth dates truncated to year, photos removed)
- **Audit trail** — Append-only, database-level immutability enforcement. Logs upstream source when proxied.
- **ETag/If-Match** — Concurrency control on updates
- **Human-in-the-loop** — Clinical writes return 428 until `X-Human-Confirmed` header is set (header-based, not cryptographic)
- **Medical disclaimers** — Injected on clinical resource reads (local and upstream)
- **URL rewriting** — Upstream server URLs never leak to clients

## Fasten Connect Integration

`r6/fasten/` is registered as a Blueprint at `/fasten`. Key endpoints:

- `POST /fasten/demo` — 5-step animated demo: register connection → webhook → ingest 4 FHIR resources → PHI redact → audit trail. Returns structured JSON with per-step results.
- `POST /fasten/webhook` — receives real Fasten webhook events, creates `FastenJob`, triggers ingestion
- `GET /fasten/jobs` — list jobs for a tenant
- `GET /fasten/connections` — list EHR connections

When `FASTEN_PUBLIC_KEY` is set, the dashboard shows a live `<fasten-stitch-element>` widget. On `widget.complete`, the JS calls `/fasten/demo` to simulate the backend flow using the returned `org_connection_id`.

## Scripts (`scripts/`)

| Script | Purpose |
| --- | --- |
| `import_healthex.py` | POST a FHIR R4 transaction Bundle to `/Bundle/$ingest-context` with step-up auth. Entry point for all bundle imports. |
| `export_healthex.py` | Pull all clinical resources from the **local HealthClaw FHIR store** via REST, de-identify (strips name/address/telecom/EHR identifiers, injects `urn:healthclaw:patient` ID), pre-tag Curatr patterns (smoking contradiction, H-flag titers, missing results), write `exports/healthex-<date>.json`. Pass `--import` to auto-ingest. |
| `export_healthex_mcp.py` | Pull from **HealthEx upstream** via the official `mcp>=1.2` Streamable HTTP client, then redact PHI in-process (`healthclaw_redact.py`) before anything hits disk. Outputs JSON or NDJSON. CLI flags: `--tenant-id`, `--output`, `--tools`, `--skip-refresh`, `--no-redact`, `--redact-mode {local,proxy}`, `--ndjson`, `--compact`. Reads `HEALTHEX_AUTH_TOKEN` env var. |
| `export_healthex_legacy.py` | Pre-MCP-SDK version of the HealthEx pull (raw httpx + custom TabularParser). Kept for backward compat with the 47 tests in `test_export_healthex.py`. |
| `healthclaw_redact.py` | In-process PHI redaction module mirroring the HealthClaw guardrail proxy rules. Public API: `redact(payload) -> (redacted, RedactionStats)` and `redact_via_proxy(payload, url, tenant)`. Used by `export_healthex_mcp.py`. |
| `bot_commands.py` | OpenClaw slash-command dispatcher invoked by each Telegram bot persona. Handles `/dashboard`, `/health`, `/conditions`, `/labs`, `/vitals`, `/meds`, `/allergies`, `/immunizations`, `/summary`, `/fhir <type>`, `/export`, `/import <path>`, `/import-help`, `/week`, `/conflicts`, `/help`. Deployed to Mac mini at `~/.healthclaw/commands.py` by `bot_commands_install.sh`. |
| `bot_commands_install.sh` | Bootstraps `~/.healthclaw/venv` (Python 3.13) on the Mac mini and installs `commands.py` + dependencies (requests, mcp>=1.2, httpx, icalendar, itsdangerous). |
| `seed_openclaw_workspaces.sh` | Creates per-persona OpenClaw workspaces (Sally-PCP, Mary-pharmacy, Dom-fitness, Kristy-scheduler) with their AGENTS.md files. |
| `update_agent_prompts.sh` | Re-syncs each persona's AGENTS.md so all bots know about the latest slash commands. |
| `kristy_schedule_watcher.py` | Background daemon for Kristy bot — scans family calendar(s), surfaces conflicts. |
| `kristy_install.sh` | Mac mini installer for the Kristy persona — drops `kristy_schedule_watcher.py` into `~/.healthclaw/` alongside its launchd plist. |
| `seed_demo_tenant.py` | Seed the `desktop-demo` tenant. Two modes: HTTP (`POST /r6/fhir/internal/seed` against a running server, used by deploy hooks) or `--db-mode` (writes directly via SQLAlchemy, no server required). Optional `--bundle-file` accepts an export bundle. |
| `convert_fasten.py` | Convert Fasten Health export format (`providers[].fhir.ResourceType[]`) to a FHIR transaction Bundle. De-duplicates by `(resourceType, id)` using `meta.lastUpdated`. |
| `demo_e2e.sh` | End-to-end gate test: liveness → seed → read with redaction → audit trail → cross-tenant isolation → curatr evaluate → human-in-the-loop. Exits 0 if all gates pass. Requires Flask (:5000) running. |
| `smoke_test.py` | Standalone (no pytest) smoke check for `export_healthex_mcp.py` + `healthclaw_redact.py` against a mocked MCP session. |
| `build_quickstart_pdf.py` | Source of truth for `static/healthclaw-quickstart.pdf` (the downloadable quickstart guide attached to subscribe welcome emails). Reportlab-based, 14 pages, two parallel paths (A: chat-only via claude.ai + HealthEx, B: full self-host). Re-run to refresh. |

Fasten Health exports are **not** standard FHIR Bundles — they use `providers[].fhir.ResourceType[]` structure. Always run `convert_fasten.py` before `import_healthex.py` when working with Fasten exports.

**Two HealthEx pull paths**: Use `export_healthex.py` when copying tenant→tenant inside the local HealthClaw store. Use `export_healthex_mcp.py` when HealthEx is the source of truth and you need fresh upstream data; this is the path the OpenClaw `/export` slash command invokes.

## Telegram Bot (`openclaw/`)

Conversational interface to the local stack via Telegram. Two execution paths share the same slash-command surface.

`openclaw/bot.py` commands: `/start` (binds chat to `TENANT_ID` via `/internal/bind-telegram`), `/connect` (replies with `${DASHBOARD_BASE_URL}/connect/${TENANT_ID}` — the lean TEFCA Stitch page), `/health`, `/conditions`, `/labs`, `/curatr`, `/curatr_fix`, `/approve`, `/token`, `/dashboard`. The Mac-mini dispatcher in `scripts/bot_commands.py` adds: `/summary`, `/meds`, `/vitals`, `/allergies`, `/immunizations`, `/fhir <type>`, `/export`, `/import <path>`, `/import-help`, `/week`, `/conflicts`, `/help`.

1. **Docker `openclaw/bot.py`** — talks to the MCP HTTP bridge (`POST /mcp/rpc`) using JSON-RPC 2.0:

   ```json
   {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"fhir_search","arguments":{...}}}
   ```

   Run via Docker Compose with the `openclaw` profile (opt-in — not started by default):

   ```bash
   TELEGRAM_BOT_TOKEN=<token> docker-compose --profile openclaw up -d
   ```

2. **OpenClaw on the Mac mini** — each persona (Sally-PCP, Mary-pharmacy, Dom-fitness, Kristy-scheduler) is a separate workspace whose `AGENTS.md` execs `~/.healthclaw/commands.py <command> <args>` (a copy of `scripts/bot_commands.py`). The dispatcher resolves secrets from `~/.healthclaw/env` and the macOS Keychain (service `healthex` for `HEALTHEX_AUTH_TOKEN`), then prints structured stdout the LLM paraphrases back to Telegram.

### `/export` end-to-end (Mac mini)

```text
DM: /export
  → bot_commands.cmd_export()
  → ~/.healthclaw/venv/bin/python3 ~/.healthclaw/export_healthex_mcp.py \
        --tenant-id ev-personal --output ~/.healthclaw/exports/healthex-<date>.json
  → mcp ClientSession → https://api.healthex.io/mcp
  → healthclaw_redact.redact() in-process (raw response never hits disk)
  → file written, _meta.redaction_stats summarized back to chat

DM: /import ~/.healthclaw/exports/healthex-<date>.json
  → POST /Bundle/$ingest-context with step-up auth
```

To set the HealthEx token on the Mac mini Keychain:

```bash
security add-generic-password -s healthex -a me -w '<token>'
```

## Hermes Gateway (`hermes/`)

Parallel to OpenClaw — [Hermes](https://github.com/nousresearch/hermes-agent) is Nous Research's self-improving agent that learns skills from conversation. It connects to HealthClaw via native MCP (Streamable HTTP), not via the JSON-RPC bridge OpenClaw uses, so no transport adapter is needed on the HealthClaw side.

| File | Purpose |
| --- | --- |
| `hermes/SOUL.md` | HealthClaw persona — tool selection, step-up flow, HITL confirm, guardrail narration, hard rules. Loaded as `/persona healthclaw`. |
| `hermes/mcp.json` | MCP server config fragment with three entries (hosted demo, local Docker, SHARP-on-MCP). Installer merges into `~/.hermes/config.json` under `mcp_servers`. |
| `hermes/install.sh` | Idempotent installer. Copies `skills/` → `~/.hermes/skills/healthclaw/`, persona → `~/.hermes/personas/healthclaw.md`, merges MCP config via `jq`. Flags: `--dry-run`, `--skills-only`. |
| `hermes/README.md` | User-facing setup + the OpenClaw vs Hermes comparison. |
| `skills/hermes/SKILL.md` | The Hermes integration written as a HealthClaw skill (auto-indexed at `/skills`). |

Hermes' "learns and iterates" loop means skills shipped in `skills/` are starting points, not final forms — Hermes will refine them in `~/.hermes/skills/healthclaw/` based on usage. The repo copy stays canonical; `./hermes/install.sh --skills-only` refreshes from repo without touching the rest of the config.

OpenClaw and Hermes can run side-by-side — they share the same MCP server, the same skills library, and the same guardrail layer. Choose by gateway preference: OpenClaw if you only want one Telegram bot with fixed slash commands; Hermes if you want multi-gateway chat (Telegram + Discord + Slack + WhatsApp + Signal + CLI + HTTP) with cross-session memory.

## SQLAlchemy Model Gotchas

Column names differ from what you might guess — use these exactly:

| Model | Column | NOT |
| --- | --- | --- |
| `R6Resource` | `id` (PK) | ~~`resource_id`~~ |
| `R6Resource` | `resource_json` | ~~`data`~~ |
| `AuditEventRecord` | `recorded` | ~~`recorded_at`~~ |
| `FastenConnection` | `org_connection_id` | — |
| `TelegramBinding` | `chat_id` is `BigInteger` (Telegram IDs can exceed 2^31), unique with `tenant_id` via `uq_tenant_chat`. Use the `bind()` and `chat_ids_for_tenant()` classmethods, not raw `query.filter_by`. | — |

PHI redaction functions:

- `from r6.redaction import apply_redaction` — HIPAA Safe Harbor (not `redact_resource`)
- `from r6.redaction import apply_patient_controlled_redaction(resource, patient_id)` — patient-controlled mode

## Deployment

Multi-host. Each surface lives where it makes sense:

| Surface | Host | URL | Purpose |
| --- | --- | --- | --- |
| Marketing site | Vercel (`healthclaw` project in team `aks129s-projects`) | `https://healthclaw.io` | Landing, wiki, FAQ, skills index, video walkthroughs |
| Flask app (FHIR REST + guardrails + Fasten webhook + OAuth) | Railway service `HealthClawGuardrails` in project `awake-serenity` | `https://app.healthclaw.io` | Persistent storage (PostgreSQL), webhook receiver, dashboard |
| MCP server (Node.js, Streamable HTTP) | Railway service `mcp-server` in project `awake-serenity` | `https://mcp-server-production-5112.up.railway.app/mcp` | Public MCP endpoint — what Claude Desktop, PromptOpinion, and Hermes connect to. `FHIR_BASE_URL` points at `https://app.healthclaw.io/r6/fhir`. |
| OpenClaw Telegram bot | Railway service `openclaw-bot` in project `awake-serenity` | n/a (long-poller) | Conversational interface; binds chats to tenants via `/start` |

Both Vercel and Railway auto-deploy on push to `main`. The Vercel project serves `api/index.py` (serverless WSGI) and static files; SQLite writes don't persist across invocations, so use Railway for anything stateful. SSO/deployment protection on Vercel must remain disabled (`ssoProtection: null`) so the public site is reachable.

PromptOpinion marketplace listings point at the Railway MCP URL:

- Agent: `https://app.promptopinion.ai/marketplace/agent/019e183d-c819-733f-9a91-6cf6756d4bed`
- Superpower (MCP): `https://app.promptopinion.ai/marketplace/mcp/019e1831-3d6f-7f72-99c2-cb8f2efab57e`

### CI (`.github/workflows/ci.yml`)

Seven jobs: `python-tests`, `node-tests`, `playwright-tests`, `compose-smoke`, `compliance-gates`, `secret-scan`, `dependency-audit`. **The compliance-gates job uses `curl -s -o /dev/null -w "%{http_code}"` (no `-f`) when verifying a 4xx response code** — `-f` makes curl exit 22 on 4xx and `set -e` then kills the step before the python assert can inspect `$STATUS`. Don't add `-f` to any gate that captures `%{http_code}` from a 4xx response.

`python-tests` runs on Python 3.11 even though local dev is usually 3.13. **Don't use backslash escapes (`\n`, `—`, etc.) inside f-string `{...}` expressions** — PEP 701 only landed in 3.12, so 3.11 raises `SyntaxError: f-string expression part cannot include a backslash`. Lift the expression into a variable instead.

## Known Limitations

- Local mode: JSON blob storage — no indexed search fields, table scans for filtering
- Structural validation only — no StructureDefinition, cardinality, or binding checks
- SubscriptionTopic stored but notifications not dispatched
- Human-in-the-loop is a header flag, not cryptographic confirmation
- Context envelope tracks membership but consent_decision is always 'permit'
- No historical versioning (version_id increments but old versions not retrievable)
- De-identification at read time, not storage time
- Upstream proxy: no response caching, no cross-version translation
- No Python linter or formatter configured; TypeScript uses strict mode with `tsc --noEmit` only

## Action Policy (`action_policy.yaml`)

Machine-readable allow/deny/approval matrix at repo root. Defines risk level and
required gates per MCP tool × resource type. Key approval modes:

- `auto` — read-only tools with PHI redaction + audit emit
- `step_up` — requires `X-Step-Up-Token` (HMAC, 5-min TTL)
- `human_review` — requires step-up token **and** `X-Human-Confirmed: true`
- `deny` — never permitted (AuditEvent mutations, SubscriptionTopic writes)

Curation states (stored in `R6Resource.curation_state`): `raw → in_review → curated | rejected`.
Promotion requires `quality_score >= 0.7`, human confirmation, and linked Provenance.

## Important Rules

- Always emit AuditEvent for FHIR resource access
- Step-up authorization required for all write operations
- `validate_step_up_token` returns `(bool, str)` — destructure both values; never coerce the tuple to a single boolean (non-empty tuples are truthy → silent auth bypass)
- Before any change touching PHI/audit/access-control: check `.claude/compliance/hipaa.md`
- Before deploying: run `./scripts/demo_e2e.sh` — all 10 gates must pass
- Run tests before committing: `uv run python -m pytest tests/ -v` and `cd services/agent-orchestrator && npm test`
- Local Python is 3.13 but CI is 3.11 — backslash escapes inside f-string `{...}` expressions parse locally and break CI. Lift into a variable.
- `r6.telegram_push.notify_tenant` is for summary-level messages only — never include PHI (names, identifiers, values). Counts, status, tenant ids, "type `/summary`" prompts are fine.
