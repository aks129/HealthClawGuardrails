# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A **reference implementation** of security and compliance patterns for AI agent access to FHIR data via Model Context Protocol (MCP). A [healthclaw.io](https://healthclaw.io) project.

**Why the `/r6/` route prefix:** The Flask Blueprint and directory are named `r6` from the project's origin as an R6 ballot resource showcase. The actual clinical data pipeline (Conditions, Observations, Immunizations, MedicationRequests, etc.) uses **R4 resources validated against US Core v9 required fields**. The R6 prefix is a historical route path, not a FHIR version statement.

**What this is not:** A production FHIR server. Local mode stores JSON blobs in SQLite. Upstream proxy mode runs real FHIR server data through the guardrail stack. Validation is structural only — no StructureDefinition conformance or terminology binding.

## Architecture

```text
Flask App (Python)
  /r6/fhir/*            FHIR REST facade (Blueprint)
  /r6/fhir/oauth/*      OAuth 2.1 + SMART
  /fasten/*             Fasten Connect EHR integration
  /shc/*                SmartHealthConnect bridge + OAuth callback brokers
  /r6-dashboard         Interactive dashboard

MCP Server (Node.js + TypeScript)
  /mcp                  Streamable HTTP (primary)
  /sse + /messages      SSE (legacy)
  /mcp/rpc              HTTP bridge for non-MCP clients

Data Source (configurable)
  LOCAL                 JSON blobs in SQLite (default)
  UPSTREAM              Real FHIR server via httpx proxy

Guardrail Stack (always active)
  PHI redaction · immutable audit trail · step-up tokens
  tenant isolation · URL rewriting · medical disclaimers
```

**Upstream proxy flow:** Client → MCP Server → Flask (guardrails) → Upstream FHIR Server. The guardrail layer redacts, audits, applies step-up auth, enforces tenant isolation, and rewrites upstream URLs before the response reaches the client.

## Build & Run Commands

```bash
# Python dependencies
uv sync

# Flask dev server
python main.py

# Flask with upstream FHIR
FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4 python main.py

# All Python tests
uv run python -m pytest tests/ -v

# Single test / single file
uv run python -m pytest tests/test_r6_routes.py::test_function_name -v

# Agent orchestrator
cd services/agent-orchestrator && npm ci && npm test

# TypeScript compile check
cd services/agent-orchestrator && npx tsc --noEmit

# Playwright e2e (requires Flask on :5000)
cd e2e && npm ci && npx playwright install --with-deps chromium && npm test

# Docker full stack
docker-compose up -d --build

# Deploy
railway up                  # Railway (full-stack)
vercel deploy --prod        # Vercel (marketing + API serverless)
```

**`.env` is NOT auto-loaded** — no code calls `load_dotenv`. Env vars come from the shell (local) or the platform (Railway/Vercel). Running `python main.py` after only editing `.env` won't pick up the new values; export them or source the file yourself. (Consequence: a key present in `.env` but absent from the process env behaves as unset — e.g. a missing provider key silently drops the action layer into simulation mode.)

## Key Directories

```text
/r6/                    FHIR modules: routes, models, validator, oauth, stepup, audit,
                        redaction, health_compliance, context_builder, health_context,
                        rate_limit, fhir_proxy, agent_client, curatr, schema_sync,
                        seed, telegram_push
/r6/actions/            Real-world action layer — propose/commit/status/callbacks for
                        phone calls (Bland.ai) + SMS (Twilio), simulation mode without keys
/r6/fasten/             Fasten Connect integration (routes, models, ingester, verify)
/r6/shc/                SmartHealthConnect bridge + OAuth callback brokers for MEDENT and HBO
/r6/wearables/          Wearable device sync (Apple Health / Fitbit)
/r6/command_center/     Per-tenant ops dashboard
/services/agent-orchestrator/  Node.js MCP server (TypeScript)
/scripts/               CLI utilities — export/import pipelines, OAuth helpers
/openclaw/              Telegram bot (bot.py + Dockerfile)
/hermes/                Nous Research Hermes agent config + persona
/skills/                Skill definitions (agentskills.io standard, auto-indexed at /skills)
/tests/                 Python tests (~700 across 37 files, incl. SDC test_sdc_*.py)
/e2e/                   Playwright tests
```

**Template notes:** `templates/index.html` is standalone — it does NOT extend `base.html`. All other templates do. Flask route names for `url_for()`: `index`, `r6_dashboard`, `wiki`, `faq`, `privacy`, `terms`, `skills_index` (not `skills`), `fasten_connect`.

## Deployment Hosts

| Surface | Host | URL |
| --- | --- | --- |
| Marketing site | Vercel (`healthclaw` project) | `https://healthclaw.io` |
| Flask app + guardrails + DB | Railway `HealthClawGuardrails` | `https://app.healthclaw.io` |
| MCP server (Node.js) | Railway `mcp-server` | `https://mcp-server-production-5112.up.railway.app/mcp` |
| Telegram bot | Railway `openclaw-bot` | (long-poller) |

Vercel serves `api/index.py` (serverless WSGI) — SQLite writes don't persist, use Railway for anything stateful. Vercel and the Railway `HealthClawGuardrails` (Flask) service auto-deploy on push to `main`.

**`mcp-server` does NOT auto-deploy** (discovered 2026-06-12: it had served May 11 code for a month). After merging MCP-server changes, deploy manually — and note `railway up` uploads the *linked directory tree* (where `railway link` ran), so deploying a sub-service from inside the repo picks up the root `railway.toml` (Flask Dockerfile) and builds the wrong app. Use a staging dir:

```bash
mkdir /tmp/mcp-deploy && cd services/agent-orchestrator \
  && cp -R Dockerfile package.json package-lock.json tsconfig.json src /tmp/mcp-deploy/ \
  && cd /tmp/mcp-deploy \
  && railway link --project <project-id> --service mcp-server --environment production \
  && railway up --service mcp-server --detach
```

Same pattern for `shl-server` (Dockerfile + railway.toml only). Setting a variable with `railway variables --set` redeploys the *old image* — it does not rebuild from new source.

## Critical Rules & Gotchas

### Security

- `validate_step_up_token` returns `(bool, str)` — **destructure both values**; never coerce the tuple to a boolean (non-empty tuple is truthy → silent auth bypass).
- Before any PHI/audit/access-control change: check `.claude/compliance/hipaa.md`.
- Always emit AuditEvent for FHIR resource access.
- Step-up authorization required for all write operations.
- **Tenant reads are authenticated, not just tenant-scoped** (`enforce_tenant_id` in `r6/routes.py`). A bare `X-Tenant-Id` only works for tenants in `PUBLIC_TENANTS` (synthetic demo tenants) or SHARP-on-MCP requests (which carry their own SMART token to the upstream). Every other tenant must send a tenant-bound `X-Step-Up-Token` OR a SMART `Authorization: Bearer` whose `tenant_id` matches — else `401`. Mint a read token via `POST /r6/fhir/internal/step-up-token`. The `/metadata` CapabilityStatement advertises the SMART OAuth service in its `rest.security` block.

### Python version

- Local dev is Python 3.13; **CI runs Python 3.11**. Backslash escapes inside f-string `{...}` expressions parse locally and break CI (PEP 701 is 3.12+). Lift into a variable before the f-string.

### CI

- `compliance-gates` job uses `curl -s -o /dev/null -w "%{http_code}"` (no `-f`) when verifying 4xx responses. Adding `-f` causes curl to exit 22 on 4xx, killing the step before the assertion can inspect `$STATUS`.

### PHI redaction imports

- `from r6.redaction import apply_redaction` — HIPAA Safe Harbor
- `from r6.redaction import apply_patient_controlled_redaction(resource, patient_id)` — patient-controlled mode
- NOT `redact_resource` (that name doesn't exist).

### Telegram push

- `r6.telegram_push.notify_tenant` is summary-level only — never include PHI (names, identifiers, values). Counts, status, and tenant IDs are fine.

## SQLAlchemy Model Gotchas

Column names differ from what you might guess:

| Model | Column | NOT |
| --- | --- | --- |
| `R6Resource` | `id` (PK) | ~~`resource_id`~~ |
| `R6Resource` | `resource_json` | ~~`data`~~ |
| `AuditEventRecord` | `recorded` | ~~`recorded_at`~~ |
| `TelegramBinding` | `chat_id` is `BigInteger` (Telegram IDs can exceed 2^31). Use `bind()` / `chat_ids_for_tenant()` classmethods, not raw `query.filter_by`. | — |

## MCP Server

**23 tools in three groups:**

- **Read** (no step-up *for public tenants only*): `context_get`, `fhir_read`, `fhir_search`, `fhir_validate`, `fhir_stats`, `fhir_lastn`, `fhir_permission_evaluate`, `fhir_subscription_topics`, `questionnaire_populate`, `curatr_evaluate`, `action_status`. Since the read-auth gate landed, reads against a **non-public** tenant also need a tenant-bound token — the MCP server must mint one (`fhir_get_token`) and forward it as `X-Step-Up-Token`/`_stepUpToken` on reads too, or those calls 401. The default `desktop-demo` tenant is public, so default-tenant reads are unaffected.
- **Write** (require step-up): `fhir_propose_write`, `fhir_commit_write`, `curatr_apply_fix`, `action_propose`, `action_commit`, `shl_generate`, `questionnaire_extract` (step-up enforced by Flask's `$extract` route on the commit path; `dry_run=true` previews without committing)
- **Utility**: `fhir_compiled_truth`, `fhir_get_token`, `fhir_seed`

Tool names use underscores (`fhir_search`, not `fhir.search`).

**`X-Tenant-ID` forwarding priority:**

1. `X-Tenant-ID` HTTP header on the incoming MCP request
2. `TENANT_ID` environment variable
3. `"desktop-demo"` hardcoded fallback

Same pattern for `X-Step-Up-Token`. Claude Desktop (no HTTP headers): pass as `_stepUpToken` / `_tenantId` / `_fhirServerUrl` / `_fhirAccessToken` in tool arguments.

**SHARP-on-MCP per-request proxy:** When `X-FHIR-Server-URL` is present, Flask builds a transient `FHIRUpstreamProxy` per request via `r6.fhir_proxy.get_proxy_for_request()`, cached on `flask.g._sharp_proxy`, closed by `teardown_request`. When absent, uses the singleton env-var proxy (or `None` for local mode).

## OAuth Callback Broker Pattern

Used for MEDENT and HBO OAuth so any browser (phone, laptop, VPS) can complete the flow without a local server:

1. Script builds auth URL with `redirect_uri=https://app.healthclaw.io/shc/<provider>/callback`
2. User opens URL, approves in provider app
3. Browser redirects to Railway — code stored in `_pending_codes` keyed by state
4. Script polls `GET /shc/<provider>/code?state=<state>` to pick up the code
5. Script exchanges code for tokens locally

Routes in `r6/shc/routes.py`: `/shc/medent/callback` + `/shc/medent/code`, `/shc/hbo/callback` + `/shc/hbo/code`.

**HBO caveat:** HBO's DCR endpoint normalizes non-loopback redirect URIs to `http://localhost/hbo/callback`. Awaiting HBO support to whitelist `https://app.healthclaw.io/shc/hbo/callback`. Until then, HBO authorize only works from the Mac mini with `HBO_REDIRECT_URI=http://localhost:8742/hbo/callback`.

## Scripts — Key Patterns

| Script | Purpose |
| --- | --- |
| `import_healthex.py` | POST a FHIR R4 transaction Bundle to `/Bundle/$ingest-context` with step-up auth |
| `export_healthex.py` | Pull from local HealthClaw FHIR store; use when copying tenant→tenant |
| `export_healthex_mcp.py` | Pull from HealthEx upstream via MCP SDK; use when HealthEx is source of truth. Redacts PHI in-process before anything hits disk. |
| `export_healthbankone_mcp.py` | Pull from Health Bank One MCP; same in-process redaction pattern |
| `healthbankone_oauth.py` | HBO OAuth PKCE helper — `authorize` / `status` / `refresh` / `revoke` / `register`. `authorize` defaults to Railway broker URI. |
| `medent_oauth.py` | MEDENT SMART on FHIR — `register` / `practices` / `authorize` / `status` / `refresh`. Railway broker for callback. |
| `export_medent_fhir.py` | Pull US Core R4 from MEDENT; redacts PHI in-process |
| `healthclaw_redact.py` | In-process PHI redaction — `redact(payload)` → `(redacted, RedactionStats)` |
| `seed_demo_tenant.py` | Seed `desktop-demo` tenant. `--db-mode` writes via SQLAlchemy directly (no server required) |
| `convert_fasten.py` | Convert Fasten export format to FHIR transaction Bundle (Fasten exports are NOT standard FHIR Bundles) |
| `bot_commands.py` | OpenClaw slash-command dispatcher — deployed to Mac mini at `~/.healthclaw/commands.py` |

**Two HealthEx pull paths:** `export_healthex.py` for local store copies; `export_healthex_mcp.py` for fresh upstream data (what `/export` in OpenClaw invokes).

## Telegram Bot Architecture

Two execution paths share the same slash-command surface:

1. **Docker `openclaw/bot.py`** — talks to MCP HTTP bridge (`POST /mcp/rpc`) via JSON-RPC 2.0. Run via `docker-compose --profile openclaw up -d`.
2. **Mac mini dispatcher** (`scripts/bot_commands.py` deployed as `~/.healthclaw/commands.py`) — each persona (Sally-PCP, Mary-pharmacy, Dom-fitness, Kristy-scheduler) is a Claude workspace whose `AGENTS.md` execs the dispatcher. Secrets from `~/.healthclaw/env` and macOS Keychain (service `healthex` for `HEALTHEX_AUTH_TOKEN`).

Post-ingest Telegram push: `r6.telegram_push.notify_tenant` is called directly via the Telegram Bot API — no IPC with the bot process. `TELEGRAM_BOT_TOKEN` must be set on the Flask service.

## Fasten Connect

`r6/fasten/` is registered at `/fasten`. After `widget.complete`, inline JS in `fasten_connect.html` POSTs to `POST /fasten/connections` with `X-Tenant-Id`. Background `stream_ingest` calls `notify_tenant` on completion (counts only, no PHI). `FASTEN_TEFCA_MODE=true` (default) makes the Stitch widget use CLEAR/ID.me for one-shot QHIN access.

## Test Fixtures (`tests/conftest.py`)

- `client` — Flask test client with in-memory SQLite
- `tenant_id` — standard test tenant string
- `step_up_token` / `auth_headers` — HMAC-signed write auth
- `tenant_headers` — read-only tenant headers
- Sample resources: `sample_patient`, `sample_observation`, `sample_bundle`, `sample_permission`, `sample_subscription_topic`, `sample_nutrition_intake`, `sample_device_alert`

**Testing write paths against a live server:** `POST /r6/fhir/internal/step-up-token` with `{"tenant_id": "..."}` returns a `token` signed with that server's `STEP_UP_SECRET` — the only way to get a valid token for a deployed server (you can't mint one locally unless the local `STEP_UP_SECRET` matches the deployed one). Use it for action/FHIR commit smoke tests; pass as `X-Step-Up-Token`.

## CI (`.github/workflows/ci.yml`)

Seven jobs: `python-tests`, `node-tests`, `playwright-tests`, `compose-smoke`, `compliance-gates`, `secret-scan`, `dependency-audit`.

Before deploying: run `./scripts/demo_e2e.sh` — all 11 gates must pass (gate 11 exercises the action layer in simulation mode).

## Action Layer (`r6/actions/`)

Real-world actions (phone calls, SMS) behind the same guardrails as FHIR writes. Lifecycle: `proposed → executing → completed | failed | unknown | expired` (the `confirmed` state exists in the transition graph but the commit route claims straight to `executing` atomically).

- `POST /r6/actions/propose` — tenant header only; returns draft + 30-min-TTL action id.
- `POST /r6/actions/<id>/commit` — requires `X-Step-Up-Token` AND `X-Human-Confirmed: true` (401/428 otherwise). Claims via a single guarded UPDATE (`WHERE status='proposed' AND expires_at > now`) — concurrent commits get 409.
- `GET /r6/actions/<id>` — status poll, tenant-isolated, lazy expiry.
- `POST /r6/actions/callback/<bland|twilio>` — fail-closed shared-secret (`ACTIONS_WEBHOOK_SECRET`); Twilio sends form-encoded `MessageStatus`/`MessageSid`, Bland sends JSON.

**House rules (from review — keep them):**

- Every status write is a guarded query-level UPDATE whose WHERE includes the expected current status. Never a bare ORM `transition()` write — stale in-memory state can clobber a webhook's verdict and cause a duplicate call.
- Post-send ambiguity (timeout, 5xx, ConnectionError, garbled response) maps to `outcome_unknown=True` → status `unknown`, NEVER `failed`. A `failed` status invites re-propose → double-placed phone call.
- Audit `detail` and `notify_tenant` use `ProposedAction.summary()` only (id/kind/recipient-label/status) — never payload, phone numbers, or provider transcripts.
- Executors log `type(exc).__name__` and HTTP status codes only — `str(exc)` can leak the secret-bearing webhook URL.
- No retries on calls — by design.

**Env vars:** `BLAND_AI_API_KEY` (calls; `BLAND_API_KEY` accepted as an alias — either name dials for real), `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`/`TWILIO_FROM_NUMBER` (SMS — all three or hard-fail), `ACTIONS_WEBHOOK_SECRET` (callbacks 403 without it), `PUBLIC_BASE_URL` (webhook base, defaults to app.healthclaw.io). Absent provider keys → simulation mode (commit completes synchronously).

**Design docs:** `docs/superpowers/specs/2026-06-12-unified-action-layer-design.md` (full integration spec — phases 2-6 cover skills, Flexpa, ainpi.dev lookup, SHL QR, careagents.cloud rewire) and `docs/superpowers/plans/2026-06-12-action-core.md` (Phase 1 plan, executed).

## SMART Health Links

Adopted **jmandel/kill-the-clipboard-skill** (MIT, pinned `6ff88e9`) as the SHL storage server rather than building bespoke — it implements SHL STU 1 and is zero-knowledge (the server stores only ciphertext + `sha256(auth)`; it can never read PHI).

- **Storage server** — Docker Compose service `shl-server` (profile `shl`); exposes port 8000; SQLite at `/data/db.sqlite` (named volume `shl-data`); `SHL_PUBLIC_URL` env sets `BASE_URL`.
- **MCP client-side crypto** — vendored into `src/ktc/`; keep diffable against upstream. AES-256-GCM encryption happens in the MCP server before anything is uploaded to the storage server.
- **Flask `$share-bundle`** operation — profiles: `intake` (default, US Core R4 clinical; name/DOB/address preserved, SSN-class identifiers and free-text stripped) and `deidentified` (apply_patient_controlled_redaction — preserves birthDate, differs from HIPAA Safe Harbor). Feeds the SHL server.
- **`shl_generate` MCP tool** — step-up gated (Write group); clinical export + Coverage + Observations (incl. wearable-sourced) → patient-controlled redaction → encrypted SHL with TTL; returns shlink/viewer/manage links. Passes `deflate:true` explicitly, so payloads still compress after upstream's compression-default flip to opt-in.
- **QR + revocation now exist UPSTREAM** (as of the `6ff88e9` bump; `app/src/components/QrCard.tsx`, `app/src/lib/qr.ts`, and `manage-shl.ts` verbs `pause|resume|re-arm`/revoke). These are **adoption** tasks (wire our manage-page/QR flow to the upstream implementation), not features to build from scratch. Also upstream: an unauthenticated `/health` liveness probe (wired into the `shl-server` container/compose healthcheck) and first-class never-expiring links.
- **`SHL_SERVER_URL` env** on the MCP server — absent → simulation mode (link generated locally, not persisted).
- **Zero-knowledge property:** storage server sees only ciphertext + `sha256(auth)`; PHI never leaves the MCP server unencrypted.
- **Railway deploy caveat:** The repo-root `railway.toml` targets the Flask Dockerfile — always `cd services/shl-server && railway up --service shl-server`; deploying from repo root picks up the wrong Dockerfile. A service that inherited root `watchPatterns` may also skip Dockerfile-only deploys until the per-service `railway.toml` takes effect after the first successful build.
- **Personas MUST use `skills/share-health-qr`** — never direct-encode PHI into QR images (incident 2026-06-12). The QR must encode only the `shlink:/` URI from `shl_generate`.

## Quality Measures (NQF 0018 / CMS165)

`r6/quality/` — a **Python measure calculator** (NOT a CQL execution engine) for
Controlling High Blood Pressure. Pure engine + report builders; Flask handler
registered on `r6_blueprint` via `register_quality_routes`.

- **`POST|GET /r6/fhir/Measure/nqf0018-controlling-high-bp/$evaluate-measure`** — computes the
  measure over the tenant's stored Patient/Condition/Observation and returns a FHIR
  `MeasureReport` (individual with `subject`/`?subject=`, or population summary with the
  performance rate). Params `periodStart`/`periodEnd` (Parameters body or query). Read-shaped:
  tenant-read-authenticated + AuditEvent. `GET /Measure/<id>` returns the `Measure` resource.
- `r6/quality/measures.py` — `evaluate_nqf0018()` / `evaluate_population()`: initial pop 18–85,
  denominator = active essential-hypertension dx, **numerator = most recent BP in period < 140/90**.
- **Threshold gotcha (clinicians check this):** the measure CONTROL target is **140/90** (office),
  deliberately distinct from the **130/80** home *diagnostic* threshold in `r6/smbp/triage.py`.
  A patient can be hypertensive (>130/80) yet controlled for the measure (<140/90).
- **Honesty:** this is a calculator, not a certified eCQM. Denominator exclusions are **partial**
  (pregnancy/ESRD only; the full CMS165 exclusion set is a documented v1 gap). The `Measure`
  resource and `description` say so — never represent it as the certified/complete eCQM.
- Demo cohort: `scripts/seed_quality_demo.py` seeds a synthetic hypertensive panel (~70% control).

## Lab Interpreter (Observation/$interpret)

`r6/labs/` — a **decision-support** interpreter (NOT a diagnostic device) that
flags lab `Observation` values against reference ranges. Pure engine
(`interpret.py`) + report builders (`report.py`); Flask handler registered via
`register_labs_routes`.

- **`POST /r6/fhir/Observation/$interpret`** — body is one Observation, a Bundle,
  or `?subject=Patient/<id>` (pulls the tenant's stored Observations). Read-shaped:
  tenant-read-authenticated + AuditEvent (PHI-free detail). Returns a `Parameters`
  with the annotated Observations (HL7 v3 `ObservationInterpretation`), a clinician
  `summary`, a plain-language `consumerSummary`, and a `disclaimer`.
- **Resource range wins:** `Observation.referenceRange` (the performing lab)
  always takes precedence over the built-in `LOINC_RANGES` table. Unknown LOINC,
  a missing unit, or a unit mismatch → *indeterminate*, never a false "normal".
- **Standards:** LOINC / UCUM / HL7 v3 ObservationInterpretation / FHIR R4. Every
  `LOINC_RANGES` entry carries a cited `source` (enforced by test).
- **Honesty / scope:** adult population defaults (clinician-reviewable before a
  live demo); panic flags are advisory (never auto-act); v1 has no unit
  conversion, no pediatric/pregnancy ranges, no trend analysis.
- **Exempt from the human-in-the-loop write gate** (`enforce_human_in_loop`) like
  `$validate` — it never persists the posted resource.
- **MCP tool:** `fhir_interpret_labs` (read group) forwards to the operation.

## SDC Forms ($populate / $extract)

HL7 Structured Data Capture form round-trip. Engines are pure (no Flask/DB) in `r6/sdc/`; the Flask handlers (`r6/sdc/routes.py`, registered on `r6_blueprint` via `register_sdc_routes`) own auth/audit/store I/O.

- **`POST /r6/fhir/Questionnaire[/<id>]/$populate`** — Questionnaire + subject + content → pre-filled QuestionnaireResponse. Mechanisms: expression-based (`initialExpression` FHIRPath via `fhirpathpy`) and observation-based (`item.code` LOINC matched against the subject's Observations). Read-shaped: tenant-read-authenticated + AuditEvent.
- **`POST /r6/fhir/QuestionnaireResponse/$extract`** — completed QR → transaction Bundle. Mechanisms: observation-based (`observationExtract`) and definition-based (`definitionExtract` + item `definition` element paths). `?dryRun=true` returns the Bundle without committing (read-auth only); otherwise requires `X-Step-Up-Token`, runs `$validate` per resource, commits, audits.
- **MCP tools:** `questionnaire_populate` (read) and `questionnaire_extract` (write).
- **Demo:** seeded `healthclaw-intake` Questionnaire (`r6/seed.py`).

**Deliberate compliance postures (decided 2026-06-18):**
- **H2 (redaction):** `$populate` returns UNREDACTED PHI by design — the form must hold real data, and the read-auth gate is the compensating control. An optional `?redaction=<profile>` param (de-identified output) is a tracked **follow-up**, not yet implemented.
- **H4 (human-in-the-loop):** `$extract` commit is exempt from the per-resource `X-Human-Confirmed` gate, treated as an ingest-class operation like `Bundle/$ingest-context`. Step-up + `$validate` gate the write.

**v1 scope limits:** definition-based extract reliably maps only `name.*` and `birthDate` element paths (see `_set_path` in `r6/sdc/extract.py`); StructureMap/CQL/template mechanisms are out of scope.
