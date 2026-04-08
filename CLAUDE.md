# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A **reference implementation** of security and compliance patterns for AI agent access to
FHIR data via Model Context Protocol (MCP). Version 1.0.0. A [healthclaw.io](https://healthclaw.io) project.

**Supports:**

- FHIR R4 with US Core v9 profiles (stable, widely deployed US healthcare resources)
- FHIR R6 v6.0.0-ballot3 (experimental ballot resources: Permission, SubscriptionTopic, DeviceAlert, NutritionIntake)

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
│  ├── /r6/fhir/* — R6 REST facade (Blueprint)    │
│  ├── /r6/fhir/health — Liveness probe           │
│  ├── /r6/fhir/oauth/* — OAuth 2.1 + SMART       │
│  ├── /fasten/* — Fasten Connect EHR integration  │
│  ├── / — Landing page                            │
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
/r6/                      R6 Python modules (routes, models, validator, oauth, stepup, audit, redaction, health_compliance, context_builder, rate_limit, fhir_proxy)
/r6/fasten/               Fasten Connect EHR integration (routes, models, ingester, verify)
/services/agent-orchestrator/  Node.js MCP server (TypeScript)
/templates/               Jinja2 templates (base.html, index.html, r6_dashboard.html)
/static/css/              Dashboard styles (r6-dashboard.css)
/static/js/               Dashboard JavaScript (r6-dashboard.js)
/tests/                   Python tests (conftest.py, test_r6_routes.py, test_r6_dashboard.py, test_context_builder.py, test_fhir_proxy.py)
/e2e/                     Playwright end-to-end tests (landing.spec.ts, dashboard.spec.ts)
/.github/workflows/       CI configuration (ci.yml)
/.claude/rules/           Claude Code rules (build.md, security.md)
/.mcp/                    MCP server manifest (server.json)
```

### Template notes

- `templates/index.html` is a **standalone page** — it does NOT `{% extends "base.html" %}`. It has its own `<html>`, nav, and footer. All other templates extend `base.html`.
- Flask route names for `url_for()`: `index`, `r6_dashboard`, `wiki`, `faq`, `privacy`, `terms`.

## Build & Run Commands

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

### Fasten Connect

| Variable | Default | Description |
| --- | --- | --- |
| `FASTEN_PUBLIC_KEY` | — | Stitch widget public key; exposes `<fasten-stitch-element>` in dashboard when set |
| `FASTEN_PRIVATE_KEY` | — | Webhook verification secret |

### MCP Orchestrator (`services/agent-orchestrator/`)

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_PORT` | `3001` | Server port |
| `FHIR_BASE_URL` | `http://localhost:5000/r6/fhir` | Backend FHIR endpoint |
| `ALLOWED_ORIGINS` | (empty = deny-all) | Comma-separated CORS allowlist |
| `RATE_LIMIT_MAX` | `120` | Max requests per minute per IP |

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

## What's R6-Specific (Experimental)

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
- **$deidentify** — HIPAA Safe Harbor. Custom operation, not part of FHIR spec.

## Search Capabilities (Honest)

In **local mode**: Supported parameters: `patient` (reference), `code` (token), `status` (token), `_lastUpdated` (date with ge/le/gt/lt prefix), `_count` (1-200), `_sort` (`_lastUpdated`/`-_lastUpdated`), `_summary` (count). NOT supported: chaining, `_include`, `_revinclude`, modifiers.

In **upstream proxy mode**: All query parameters forwarded to the upstream server. The upstream server's full search capabilities are available (chaining, _include, etc. if the upstream supports them).

## MCP Tools (12)

- **Read tools** (no step-up): `context_get`, `fhir_read`, `fhir_search`, `fhir_validate`, `fhir_stats`, `fhir_lastn`, `fhir_permission_evaluate`, `fhir_subscription_topics`, `curatr_evaluate`
- **Write tools** (require step-up token): `fhir_propose_write`, `fhir_commit_write`, `curatr_apply_fix`
- Tools add `_mcp_summary` with reasoning, clinical context, and limitations
- `propose_write` identifies clinical types requiring human-in-the-loop
- `permission_evaluate` returns reasoning explaining why permit/deny
- Result entries capped at 50 to stay within token limits

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

## SQLAlchemy Model Gotchas

Column names differ from what you might guess — use these exactly:

| Model | Column | NOT |
| --- | --- | --- |
| `R6Resource` | `id` (PK) | ~~`resource_id`~~ |
| `R6Resource` | `resource_json` | ~~`data`~~ |
| `AuditEventRecord` | `recorded` | ~~`recorded_at`~~ |
| `FastenConnection` | `org_connection_id` | — |

PHI redaction function: `from r6.redaction import apply_redaction` (not `redact_resource`).

## Deployment (healthclaw.io)

Hosted on **Vercel** (project: `healthclaw`, team: `aks129s-projects`). `api/index.py` is the serverless WSGI entry point. `vercel.json` routes all traffic to it.

- Production URL: `https://healthclaw.io`
- Railway is also configured (`railway.toml`) for full-stack Docker deployment with Redis; use Railway when persistent SQLite or the MCP server is needed.
- Vercel serverless has no persistent filesystem — SQLite writes don't persist between invocations. Suitable for demo/read-only use.
- SSO/deployment protection should remain **disabled** (`ssoProtection: null`) so public visitors can access the site without Vercel auth.

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

## Important Rules

- Always emit AuditEvent for FHIR resource access
- Step-up authorization required for all write operations
- Run tests before committing: `uv run python -m pytest tests/ -v` and `cd services/agent-orchestrator && npm test`
