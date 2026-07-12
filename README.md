<div align="center">

<img src=".github/assets/healthclaw-logo.png" alt="HealthClaw — AI-Powered Healthcare Intelligence" width="440">

# HealthClaw Guardrails

### The open-source security layer between AI agents and clinical data.

*FHIR standardized how health data is structured. MCP standardized how AI connects to tools.*
***Nobody standardized the guardrails in between. This project does.***

<br/>

<!-- Project -->
[![Release](https://img.shields.io/badge/release-v1.8.0-f97316?style=flat-square)](https://github.com/aks129/HealthClawGuardrails/releases)
[![License](https://img.shields.io/badge/license-MIT-2dd4bf?style=flat-square)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/aks129/HealthClawGuardrails/ci.yml?branch=main&style=flat-square&label=CI&logo=github)](https://github.com/aks129/HealthClawGuardrails/actions/workflows/ci.yml)
[![Code size](https://img.shields.io/github/languages/code-size/aks129/HealthClawGuardrails?style=flat-square&color=0ea5e9)](https://github.com/aks129/HealthClawGuardrails)

<!-- Community metrics -->
[![Stars](https://img.shields.io/github/stars/aks129/HealthClawGuardrails?style=flat-square&logo=github&color=eab308)](https://github.com/aks129/HealthClawGuardrails/stargazers)
[![Forks](https://img.shields.io/github/forks/aks129/HealthClawGuardrails?style=flat-square&logo=github&color=8b5cf6)](https://github.com/aks129/HealthClawGuardrails/network/members)
[![Issues](https://img.shields.io/github/issues/aks129/HealthClawGuardrails?style=flat-square&logo=github&color=ef4444)](https://github.com/aks129/HealthClawGuardrails/issues)
[![Contributors](https://img.shields.io/github/contributors/aks129/HealthClawGuardrails?style=flat-square&color=14b8a6)](https://github.com/aks129/HealthClawGuardrails/graphs/contributors)
[![Last commit](https://img.shields.io/github/last-commit/aks129/HealthClawGuardrails?style=flat-square&color=64748b)](https://github.com/aks129/HealthClawGuardrails/commits/main)

<!-- Stack & scope -->
[![Tests](https://img.shields.io/badge/tests-1170%2B%20Python%20%2B%20112%20Node-22c55e?style=flat-square)](#testing)
[![MCP tools](https://img.shields.io/badge/MCP%20tools-29-6366f1?style=flat-square&logo=anthropic)](#mcp-tools-29)
[![FHIR](https://img.shields.io/badge/FHIR-R4%20US%20Core%20v9-0ea5e9?style=flat-square)](#fhir-version-support)
[![Guardrail conformance](https://img.shields.io/endpoint?url=https%3A%2F%2Fapp.healthclaw.io%2Fr6%2Ffhir%2F%24conformance%3Fformat%3Dshields&style=flat-square)](#prove-it-guardrail-conformance)
[![Glama score](https://glama.ai/mcp/servers/aks129/HealthClawGuardrails/badges/score.svg)](https://glama.ai/mcp/servers/aks129/HealthClawGuardrails)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED?style=flat-square&logo=docker&logoColor=white)](#docker)

<br/>

**[Quick Start](#quick-start)** · **[MCP Tools](#mcp-tools-29)** · **[Recipes](docs/recipes/)** · **[Roadmap](ROADMAP.md)** · **[Claude Plugin](#install-as-a-claude-plugin)** · **[Architecture](#what-it-does)** · **[healthclaw.io](https://healthclaw.io)** · **[Contributing](CONTRIBUTING.md)** · **[Dev Guide](docs/development.md)**

</div>

---

> **What it is:** an open reference implementation of the FHIR × MCP guardrail layer — PHI redaction,
> immutable audit, step-up auth, and tenant isolation — that sits between *any* AI agent and *any* FHIR
> server. Built in the open as a community project, MIT-licensed. Not a product, not a pitch: if the
> pattern is useful, take it; if it's wrong, tell us or fix it.

**This is a community effort.** It's most useful when implementers, clinicians, and standards folks poke holes in it. Issues, PRs, and "you got the SDC extraction wrong" critiques are all welcome — start with **[CONTRIBUTING.md](CONTRIBUTING.md)** and the **[Code of Conduct](CODE_OF_CONDUCT.md)**.

**At a glance:** v1.8.0 · 1,170+ Python + 112 Node tests · 29 MCP tools · real-world action rail (provably out-of-band gate) · FHIR R4 US Core v9 + R6 v6.0.0-ballot3 · HL7 SDC forms (`$populate`/`$extract`) · NQF 0018 quality measure · lab interpreter (`$interpret`) · care-gaps reminders (`$care-gaps`) · ChatGPT-connector `search`/`fetch` · Fasten TEFCA · HealthEx · HBO · Flexpa · Epic · MEDENT · Open Wearables · real-world actions (calls/SMS) · SMART Health Links · Claude Code plugin · OpenAI/Gemini adapters

## Try it in 60 seconds — no clone, no keys

The hosted demo runs synthetic data behind the full guardrail stack:

```bash
# Watch the deployment grade its own guardrails (PHI redaction, audit, step-up, ...):
curl "https://app.healthclaw.io/r6/fhir/\$conformance?format=text"
```

Point any MCP client at the live server — URL `https://mcp-server-production-5112.up.railway.app/mcp`,
header `X-Tenant-Id: desktop-demo` — then ask: *"Search my health records for lab results and explain
them in plain language."* One-command installs:
`gemini extensions install https://github.com/aks129/HealthClawGuardrails` ·
`claude plugin marketplace add aks129/HealthClawGuardrails` ·
skills on [ClawHub](https://clawhub.ai/aks129/skills/fhir-r6-guardrails)

**Non-developer?** Step-by-step guides for Claude (web/desktop/phone), Perplexity,
ChatGPT, and Telegram — plus a 10-minute demo script — in [docs/quickstarts/](docs/quickstarts/README.md).

**Listed in:** [Official MCP Registry](https://registry.modelcontextprotocol.io) (`io.github.aks129/healthclaw-guardrails`) ·
[Glama](https://glama.ai/mcp/servers/aks129/HealthClawGuardrails) ([hosted connector](https://glama.ai/mcp/connectors/io.github.aks129/healthclaw-guardrails)) ·
[ClawHub](https://clawhub.ai/aks129/skills/fhir-r6-guardrails) (14 skills) ·
Gemini CLI Extensions · agent-skills discovery at [`/.well-known/agent-skills/`](https://healthclaw.io/.well-known/agent-skills/index.json)

## Release highlights

Full notes live in **[Releases](https://github.com/aks129/HealthClawGuardrails/releases)**.

| Version | Highlights |
| --- | --- |
| **v1.8.0** | **Real-actions foundation** — an agent can *propose* a real-world action (call, SMS, form) but `commit` only *submits* it (HTTP 202); execution happens through a separate approval that requires a single-use step-up credential and an expiry-guarded atomic claim, so the agent's own toolchain can never approve its own action (the spoofable `X-Human-Confirmed` header is gone) · **`ActionExecutor` plugin registry** — add a real-world capability behind the full guardrail rail in ~50 lines, no core changes ([extend it](ROADMAP.md#extending-the-action-rail)) · mandatory red-flag emergency screen; fail-loud rails (no silent simulation) · **durable execution** — attempt ledger, provider reconciliation, external-tick reaper, append-only action-event log · **reliability floor** — config preflight (`GET /r6/ops/preflight`), Postgres CI lane, MCP fetch timeouts, poller 409-storm detection, source-aware resource identity `(tenant, type, id)`, Fasten hardening + zombie-job reaper · public [ROADMAP](ROADMAP.md) + contributor on-ramp · fixes: upstream FHIR error fidelity, quality measures default to current year |
| **v1.7.0** | Preventive care-gaps engine (`Patient/$care-gaps`, USPSTF/ACIP/ADA + eCQM crosswalk) · patient connect flow: identity-verified Fasten onboarding mints a webhook-gated, read-scoped 30-day agent token · prescription transfer requests (`rx_transfer_request`, Schedule II refused) — 29 MCP tools · [per-agent quickstarts](docs/quickstarts/) (Claude/Perplexity/ChatGPT/Telegram) · HBO export→FHIR converter + embedded-XML PHI scrubber · hardening: fail-closed webhook verify, scoped tokens, serverless write guard, live-path contract tests · clinical fixes: SNOMED diabetes detection, inclusive panic thresholds, one-sided-range honesty |
| **v1.6.0** | Lab reference-range interpreter (`Observation/$interpret`) · NQF 0018 quality measure (`Measure/$evaluate-measure`) · [any-agent-framework adapters](docs/recipes/any-agent-framework.md) (OpenAI/Gemini) · [Medplum-in-front recipe](docs/recipes/healthclaw-in-front-of-medplum.md) · SMBP triage on 2025 AHA/ACC · ruff lint gate · all dependency advisories remediated |
| v1.5.0 | Read-auth hardening (tenant reads authenticated, not just scoped) · HL7 SDC forms — `$populate` / `$extract` |
| v1.4.0 | Six health-data connectors (Fasten TEFCA, HealthEx, Health Bank One, Flexpa, Epic, MEDENT) behind one guardrail stack |
| v1.3.0 | Wearables → FHIR Observations (8 providers, LOINC/UCUM mapping, device Provenance) |
| v1.2.0 | Compiled Truth — current state + append-only Provenance trail per resource |

## What It Does

This is a **vendor-neutral guardrail proxy** that sits between any AI agent and any FHIR server. Every request passes through:

- **PHI redaction** — Names truncated to initials, identifiers masked, addresses stripped, birth dates truncated to year
- **Immutable audit trail** — Every read/write logged with tenant, agent, timestamp
- **Step-up authorization** — HMAC-SHA256 tokens required for writes
- **Human-in-the-loop** — Clinical writes blocked until a human confirms (HTTP 428); real-world actions (calls, SMS, forms) go further: `commit` only *submits*, and execution requires a **provably out-of-band** single-use approval the agent's own toolchain cannot satisfy
- **Tenant isolation** — Every query scoped to tenant, cross-tenant access blocked
- **Medical disclaimers** — Injected on all clinical resource reads
- **Compiled Truth** — Current state + append-only evidence trail for every resource

```text
AI Agent ──▶ MCP Server ──▶ Guardrail Proxy ──▶ Any FHIR Server
                              ↓                    (HAPI, Epic,
                         PHI redaction              Medplum, etc.)
                         Audit trail
                         Step-up auth
                         Human-in-the-loop
```

## Prove it: guardrail conformance

The guardrails are **verifiable, not marketing.** A runnable harness probes any
deployment with synthetic data and emits a scorecard across all six properties —
run it against your own instance (or ours):

```bash
python scripts/guardrail_conformance.py \
  --base-url https://app.healthclaw.io --tenant desktop-demo \
  --step-up-token "$(mint a token via POST /r6/fhir/internal/step-up-token)"
```

```text
HealthClaw Guardrail Conformance — https://app.healthclaw.io [tenant=desktop-demo]
  Grade: A   (6/6 properties)
  [PASS] PHI Redaction            [PASS] Human-in-the-Loop
  [PASS] Immutable Audit Trail    [PASS] Tenant Isolation
  [PASS] Step-Up Authorization    [PASS] Medical Disclaimers
```

Or hit the **one-URL self-test** on any running deployment — no token needed, it
self-tenants internally and returns 200 at Grade A (503 otherwise):

```bash
curl "https://app.healthclaw.io/r6/fhir/\$conformance?format=text"
```

The same harness runs against the Flask test client as a **CI gate** (`tests/test_guardrail_conformance.py`),
so a guardrail regression fails the build. `--json` emits a machine-readable
report. Library API: `from r6.conformance import LiveProbeClient, ProbeContext, run_conformance`.

## Install as a Claude Plugin

HealthClaw ships as a Claude Code plugin marketplace. Two plugins are available:

```bash
# Add the marketplace
claude plugin marketplace add aks129/HealthClawGuardrails

# Install the FHIR guardrail plugin (this repo)
claude plugin install healthclaw-guardrails@healthclaw-marketplace

# Install the personal-health companion plugin (SmartHealthConnect)
claude plugin install smarthealthconnect@healthclaw-marketplace
```

| Plugin | Skills | Source |
| --- | --- | --- |
| `healthclaw-guardrails` | curatr, fasten-connect, fhir-r6-guardrails, fhir-upstream-proxy, healthex-export, phi-redaction | [aks129/HealthClawGuardrails](https://github.com/aks129/HealthClawGuardrails) |
| `smarthealthconnect` | care-completion, diet-exercise, healthy-habits, kids-health, medication-refills, research-monitor | [aks129/SmartHealthConnect](https://github.com/aks129/SmartHealthConnect) |

Each skill is auto-discoverable — Claude loads it when your prompt matches the skill's trigger phrases (e.g. "check my care gaps", "redact this bundle", "run Curatr on my conditions").

**Not on Claude/MCP?** The same 28 guardrailed tools run on OpenAI, Gemini, LangChain, or plain HTTP via the framework-neutral bridge in [`adapters/`](adapters/) — see [Recipe: run HealthClaw tools on any agent framework](docs/recipes/any-agent-framework.md). Guardrails stay server-side, so no framework can bypass them.

## Quick Start

```bash
# Install dependencies
uv sync

# Run (local mode with SQLite)
STEP_UP_SECRET=your-secret python main.py

# Run with upstream FHIR server
FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4 STEP_UP_SECRET=your-secret python main.py

# Open browser
open http://localhost:5000            # Landing page with live demo
open http://localhost:5000/r6-dashboard  # Interactive dashboard
```

### Docker

```bash
docker-compose up -d --build

# macOS note: port 5000 conflicts with AirPlay Receiver — remap with:
# HOST_PORT=5050 docker-compose up -d --build

# Services:
# - fhir-mcp-guardrails (Flask, port 5000)
# - agent-orchestrator (MCP server, port 3001)
# - redis (port 6379)
```

## MCP Tools (29)

Tool names use underscores (not dots) for Claude Desktop / MCP client compatibility.

**Read tools** (no step-up for public tenants):

| Tool | Description |
| --- | --- |
| `context_get` | Retrieve pre-built context envelopes |
| `fhir_read` | Read a FHIR resource (redacted) |
| `fhir_search` | Search with patient, code, status, date filters |
| `fhir_validate` | Structural validation |
| `fhir_stats` | Observation statistics (count/min/max/mean) |
| `fhir_lastn` | Most recent N observations per code |
| `fhir_interpret_labs` | Lab reference-range interpretation (`$interpret`) — decision support, not diagnosis |
| `care_gaps` | Preventive-care gaps (`$care-gaps`) — screenings/immunizations that may be due, from the patient's own records |
| `guardrail_conformance` | Run the guardrail conformance self-test — graded A–F scorecard across all six properties |
| `fhir_permission_evaluate` | R6 Permission access control evaluation |
| `fhir_subscription_topics` | List available SubscriptionTopics |
| `questionnaire_populate` | SDC `$populate` — pre-fill a Questionnaire for a subject |
| `curatr_evaluate` | Evaluate a FHIR resource for data quality issues |
| `action_status` | Poll a real-world action (call/SMS) |
| `search` | ChatGPT-connector-compatible search — thin wrapper over `fhir_search`, returns compact `{id, title, url}` results |
| `fetch` | ChatGPT-connector-compatible fetch by `ResourceType/id` — thin wrapper over `fhir_read`, returns `{id, title, text, url, metadata}` |

**Write tools** (require step-up token):

| Tool | Description |
| --- | --- |
| `fhir_propose_write` | Validate + preview without committing |
| `fhir_commit_write` | Commit with step-up auth + human-in-the-loop |
| `questionnaire_extract` | SDC `$extract` — extract resources from a completed QuestionnaireResponse |
| `curatr_apply_fix` | Apply patient-approved fixes with Provenance tracking |
| `action_propose` / `action_commit` | Propose / commit a real-world phone call or SMS |
| `rx_transfer_request` | Draft a pharmacy-transfer request call from active meds (Schedule II refused); commit via `action_commit` |
| `shl_generate` | Generate an encrypted SMART Health Link (QR) |

**Utility tools:**

| Tool | Description |
| --- | --- |
| `fhir_get_token` | Issue a 5-minute step-up token (call before any write) |
| `fhir_seed` | Seed a tenant with demo Patient + Observations + Condition |
| `fhir_compiled_truth` | Current state + Provenance evidence timeline |

All tools add `_mcp_summary` with reasoning, clinical context, and limitations.

## Guardrail Demo

The 6-step demo at `/r6/fhir/demo/agent-loop` shows the full guardrail sequence:

1. **PHI Redaction** — Agent reads a patient, receives redacted data
2. **$validate Gate** — Agent proposes an Observation, validated before write
3. **Permission Deny** — No Permission rule exists, access denied with reasoning
4. **Permission Permit** — Permit rule created, re-evaluation succeeds
5. **Step-up + Human-in-the-loop** — Write requires both token and human confirmation
6. **Commit + Audit** — Write succeeds, full audit trail generated

## Comparison

| Feature | This Project | AWS HealthLake MCP | Medplum MCP | Raw FHIR API |
| --- | --- | --- | --- | --- |
| Works with any FHIR server | Yes | HealthLake only | Medplum only | N/A |
| PHI redaction on reads | Yes | No | No | No |
| Immutable audit trail | Yes | CloudTrail (separate) | Partial | No |
| Step-up auth for writes | Yes | IAM (separate) | Medplum auth | No |
| Human-in-the-loop | Yes | No | No | No |
| Permission $evaluate (R6) | Yes | No | No | No |
| Setup time | 10 seconds | 30+ minutes | 15+ minutes | Varies |

## FHIR Version Support

| Version | Profile | Status | Resources |
| --- | --- | --- | --- |
| R4 | US Core v9 | **Stable** | Patient, Condition, AllergyIntolerance, Immunization, MedicationRequest, Procedure, DiagnosticReport, CarePlan, CareTeam, Goal, DocumentReference, Coverage, ServiceRequest, Location, Organization, Practitioner, PractitionerRole, RelatedPerson, Specimen, FamilyMemberHistory |
| R6 | v6.0.0-ballot3 | Experimental | Permission, SubscriptionTopic, DeviceAlert, NutritionIntake, DeviceAssociation, NutritionProduct, Requirements, ActorDefinition |

Both R4 and R6 resources flow through the same guardrail stack (PHI redaction, audit, step-up auth, tenant isolation). R6 ballot resources may change before final release.

## Testing

```bash
# Python tests (1,170+ across 80+ files; includes action-rail, SDC, quality, labs, ops suites)
uv run python -m pytest tests/ -v
uv run python -m pytest tests/test_r6_routes.py::test_name -v   # single test

# MCP server tests
cd services/agent-orchestrator && npm ci && npm test

# Playwright end-to-end tests (UI + API, requires Flask on :5000)
cd e2e && npm ci && npx playwright install --with-deps chromium && npm test
cd e2e && npm run test:headed    # headed browser
cd e2e && npm run test:ui        # interactive UI mode
```

## API Endpoints

| Endpoint | Method | Description |
| --- | --- | --- |
| `/r6/fhir/metadata` | GET | CapabilityStatement |
| `/r6/fhir/health` | GET | Liveness probe (reports upstream status) |
| `/r6/fhir/{type}` | POST | Create resource (requires step-up) |
| `/r6/fhir/{type}` | GET | Search resources |
| `/r6/fhir/{type}/{id}` | GET | Read resource (redacted) |
| `/r6/fhir/{type}/{id}` | PUT | Update resource (requires step-up + ETag) |
| `/r6/fhir/{type}/$validate` | POST | Validate resource |
| `/r6/fhir/Questionnaire[/{id}]/$populate` | POST | SDC — pre-fill a QuestionnaireResponse from a subject |
| `/r6/fhir/QuestionnaireResponse/$extract` | POST | SDC — extract a transaction Bundle (`?dryRun=true` to preview) |
| `/r6/fhir/{type}/{id}/$deidentify` | GET | HIPAA Safe Harbor de-identification |
| `/r6/fhir/Observation/$stats` | GET | Observation statistics |
| `/r6/fhir/Observation/$lastn` | GET | Most recent observations |
| `/r6/fhir/Permission/$evaluate` | POST | R6 access control evaluation |
| `/r6/fhir/SubscriptionTopic/$list` | GET | Subscription topic discovery |
| `/r6/fhir/Bundle/$ingest-context` | POST | Bundle ingestion + context envelope |
| `/r6/fhir/context/{id}` | GET | Retrieve context envelope |
| `/r6/fhir/AuditEvent` | GET | Search audit events |
| `/r6/fhir/AuditEvent/$export` | GET | Export audit trail (NDJSON/Bundle) |
| `/r6/fhir/demo/agent-loop` | POST | 6-step guardrail demo |
| `/r6/fhir/oauth/*` | * | OAuth 2.1 + PKCE + SMART discovery |
| `/r6/fhir/{type}/{id}/$curatr-evaluate` | GET | Evaluate resource data quality (Curatr) |
| `/r6/fhir/{type}/{id}/$curatr-apply-fix` | POST | Apply patient-approved fixes with Provenance |

## Upstream Proxy

Connect to real FHIR servers while keeping all guardrails active:

```bash
FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4 python main.py
```

- **Reads**: Fetched from upstream, then redacted + audited + disclaimers added
- **Searches**: Forwarded with all query params, results redacted per entry
- **Writes**: Validated locally first, then forwarded with step-up auth check
- **URL rewriting**: Upstream URLs never leak to clients

Tested with: HAPI FHIR R4/R5, SMART Health IT, Epic Sandbox.

**Put the guardrails in front of your FHIR server** — recipe for running the
redaction + audit + step-up + human-in-the-loop stack in front of **Medplum**
(the same pattern works for Aidbox, Google Cloud Healthcare, or any FHIR R4
server): [docs/recipes/healthclaw-in-front-of-medplum.md](docs/recipes/healthclaw-in-front-of-medplum.md).
A repeatable integration test (`tests/test_medplum_in_front.py`) proves a
Medplum-returned Patient comes back redacted + audited and writes are step-up
gated before reaching Medplum.

## Curatr — Patient-Owned Data Quality

Curatr is a patient-facing data quality skill that evaluates FHIR health records for
coding issues and lets the patient decide how to resolve them.

```text
1. Patient connects data → HealthClaw Guardrails deidentifies and loads it
2. OpenClaw calls curatr.evaluate → checks codes against live terminology APIs
3. Issues presented in plain language with impact and fix suggestions
4. Patient approves fixes → curatr.apply_fix updates resource + creates Provenance
5. Optional: generate a structured correction request for the source provider
```

**What Curatr checks on a Condition:**

| Check | Service | Example |
| --- | --- | --- |
| Deprecated code system | Local lookup (no network) | ICD-9-CM → critical |
| ICD-10-CM code validity | NLM Clinical Tables API | Invalid code → warning |
| SNOMED CT / LOINC validity | tx.fhir.org (HL7 public) | Unknown code → warning |
| RxNorm drug code | RXNAV API (NLM) | Missing RXCUI → warning |
| Display name accuracy | Cross-checked with canonical term | Mismatch → suggestion |
| Missing required fields | Structural | No clinicalStatus → warning |

Every fix creates a linked **Provenance** resource recording patient intent, field
changes, and agent attribution. All changes are audited in the immutable trail.

**OpenClaw skill:** `skills/curatr/SKILL.md`

## SMART Health Links (Kill the Clipboard)

Patient-controlled encrypted record sharing via QR code, implemented on top of
**[jmandel/kill-the-clipboard-skill](https://github.com/jmandel/kill-the-clipboard-skill)**
(MIT, pinned `fa0020d`) — credit Josh Mandel. HealthClaw governs what enters the
bundle (step-up auth, profiles, guardrails, audit trail); KTC governs sharing
(zero-knowledge server-side storage, SHL STU 1 protocol, revocation, in-browser
viewer).

**What it does:** The `shl_generate` MCP tool (Write group, step-up required)
fetches the patient's guardrailed FHIR bundle, encrypts it client-side in the MCP
server (the SHL server never sees plaintext), uploads ciphertext, and returns:

- `shlink` — the `shlink:/` URI to encode in a QR (an encrypted pointer, not data)
- `viewer_link` — browser URL for clinic staff
- `manage_link` — patient-only revocation + access-log URL

**Security:** The QR encodes only the encrypted pointer. PHI never appears in the
QR image. The SHL server stores only ciphertext + `sha256(auth_token)`. Persona
hard rule: see `skills/share-health-qr/SKILL.md` — never direct-encode PHI into
QR images (incident 2026-06-12).

### Quick Start (local)

```bash
# Start the SHL storage server (profile `shl`)
docker-compose --profile shl up -d

# Tell the MCP server where the SHL server lives
# Add to services/agent-orchestrator/.env or export:
export SHL_SERVER_URL=http://localhost:8000
```

Without `SHL_SERVER_URL`, `shl_generate` returns an explicit simulation stub
(`simulated: true`) — never a fake link.

### Railway Deploy

```bash
# 1. Add the SHL service
railway add --service shl-server

# 2. Attach a persistent volume (SQLite lives here)
railway service shl-server && railway volume add --mount-path /data

# 3. Configure the SHL server
railway variables --service shl-server \
  --set BASE_URL=<public-url-of-shl-server> \
  --set DB_PATH=/data/db.sqlite

# 4. Expose a public domain
railway domain --service shl-server

# 5. Deploy — MUST run from the shl-server directory
cd services/shl-server && railway up --service shl-server

# 6. Wire the MCP server to the SHL server
railway variables --service mcp-server \
  --set SHL_SERVER_URL=<public-url-of-shl-server>
```

> **Caveat 1 — deploy from the right directory:** The repo-root `railway.toml`
> targets the Flask Dockerfile. If you run `railway up --service shl-server`
> from the repo root, Railway uses the wrong Dockerfile and the deploy fails.
> Always `cd services/shl-server` first — that directory has its own
> `railway.toml` that points to the correct image.
>
> **Caveat 2 — watchPatterns skip:** A service that inherited `watchPatterns`
> from the root config may silently skip Dockerfile-only deploys (no source
> file changes detected). The per-service `railway.toml` in `services/shl-server/`
> overrides this after the first successful build. If deploys are skipped, force
> one with `railway up --service shl-server` from the shl-server directory.
>
> **Caveat 3 — simulation mode:** Without `SHL_SERVER_URL` on the MCP server,
> `shl_generate` returns `{ simulated: true, note: "SHL_SERVER_URL not
> configured — returned stub." }`. Personas surface this note verbatim and
> never improvise an alternative.

**OpenClaw skill:** `skills/share-health-qr/SKILL.md`

## R6-Specific Resources (Experimental)

These resources are part of the FHIR R6 ballot3 specification and may change before final release.

| Resource | What's New in R6 |
| --- | --- |
| Permission | Access control (separate from Consent), `$evaluate` operation |
| SubscriptionTopic | Restructured pub/sub (introduced R5, maturing R6) |
| DeviceAlert | ISO/IEEE 11073 device alarms |
| NutritionIntake | Dietary consumption tracking |
| DeviceAssociation | Device-patient relationships |
| NutritionProduct | Nutritional product definitions |
| Requirements | Functional requirements tracking |
| ActorDefinition | Actor role definitions |

## US Core v9 R4 Resources (Stable)

Standard FHIR R4 resources conforming to US Core Implementation Guide v9.
These are widely deployed in US healthcare and stable for production use.

AllergyIntolerance, Immunization, MedicationRequest, Medication, MedicationDispense,
Procedure, DiagnosticReport, CarePlan, CareTeam, Goal, DocumentReference,
Location, Organization, Practitioner, PractitionerRole, RelatedPerson,
Coverage, ServiceRequest, Specimen, FamilyMemberHistory

## Environment Variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `STEP_UP_SECRET` | Production | — | HMAC-SHA256 signing secret |
| `FHIR_UPSTREAM_URL` | No | — | Upstream FHIR server (enables proxy mode) |
| `SQLALCHEMY_DATABASE_URI` | Production | `sqlite:///mcp_server.db` | Database connection |
| `SESSION_SECRET` | No | (dev key) | Flask session secret |
| `FHIR_UPSTREAM_TIMEOUT` | No | 15 | Upstream request timeout (seconds) |
| `FHIR_LOCAL_BASE_URL` | No | — | Local URL for response URL rewriting |

## Project Structure

```text
main.py                         Flask app entry point
app.py                          Web UI routes (landing, dashboard)
r6/
  routes.py                     R6 FHIR REST Blueprint (1,732 lines)
  models.py                     R6Resource, ContextEnvelope, AuditEventRecord
  validator.py                  FHIR R6 structural validation
  redaction.py                  PHI redaction (names, identifiers, addresses, DOB, telecom)
  audit.py                      Immutable AuditEvent recording
  stepup.py                     HMAC-SHA256 step-up token management
  oauth.py                      OAuth 2.1 + PKCE + SMART-on-FHIR discovery
  health_compliance.py          Disclaimers, HITL, HIPAA Safe Harbor, audit export
  context_builder.py            Bundle ingestion + context envelopes
  rate_limit.py                 Per-tenant rate limiting
  fhir_proxy.py                 Upstream FHIR server proxy with URL rewriting
  curatr.py                     Curatr data quality engine (terminology lookups + fix application)
services/agent-orchestrator/
  src/index.ts                  MCP server (Streamable HTTP + SSE)
  src/tools.ts                  12 tool definitions + executor (incl. curatr.evaluate, curatr.apply_fix)
e2e/                            Playwright end-to-end tests
templates/                      Jinja2 (landing page, dashboard)
static/                         CSS + JS for interactive dashboard
skills/curatr/                  Curatr OpenClaw skill definition
tests/                          266 pytest tests (8 files, incl. test_us_core_r4.py)
```

## Personal FHIR data store — patient import flow

This walkthrough shows how to go from a raw HealthEx export to querying your
own records through Claude Code's MCP tools.

### 1. Start the stack

```bash
uv sync
uv run python main.py                         # Flask on :5000
cd services/agent-orchestrator && npm ci && npm start  # MCP on :3001
```

### 2. Import your HealthEx / Flexpa / generic FHIR bundle

```bash
# Dry-run first to preview without writing
python scripts/import_healthex.py \
  --bundle-file ~/Downloads/my-records.json \
  --dry-run

# Real import — prints context_id on success
python scripts/import_healthex.py \
  --bundle-file ~/Downloads/my-records.json \
  --tenant-id my-patient \
  --step-up-secret "$STEP_UP_SECRET"
```

### 3. Connect Claude Code via MCP

`.mcp.json` in this repo auto-configures Claude Code when you open the project.
Update `X-Tenant-ID` to match your `--tenant-id`:

```json
{
  "mcpServers": {
    "healthclaw-local": {
      "type": "http",
      "url": "http://localhost:3001/mcp",
      "headers": { "X-Tenant-ID": "my-patient" }
    }
  }
}
```

Then in Claude Code:

```text
Use fhir_search to find all my Conditions
Use context_get with context_id <ctx-id> to get my full context envelope
Use curatr_evaluate on Condition/<id> to check data quality
```

### 4. Set up Fasten Connect (optional)

```bash
# .env additions
FASTEN_PUBLIC_KEY=<key>
FASTEN_PRIVATE_KEY=<key>
FASTEN_WEBHOOK_SECRET=<secret>
FASTEN_CURATR_SCAN=true    # auto-run Curatr after each import
```

Records arrive via webhook at `/r6/fasten/webhook` and are stored under the
patient's canonical tenant ID.

### 5. Deidentify for sharing

```bash
# HIPAA Safe Harbor
curl -H "X-Tenant-ID: my-patient" \
  http://localhost:5000/r6/fhir/Patient/pt-1/\$deidentify

# Patient-controlled (preserves birthDate, strips institutional identifiers)
curl -H "X-Tenant-ID: my-patient" \
  "http://localhost:5000/r6/fhir/Patient/pt-1/\$deidentify?mode=patient-controlled&patient_id=my-patient"
```

### 6. Telegram bot (optional)

```bash
TELEGRAM_BOT_TOKEN=<token> TENANT_ID=my-patient \
FHIR_BASE_URL=http://localhost:5000/r6/fhir \
python openclaw/bot.py
```

Commands: `/health`, `/conditions`, `/labs`, `/curatr`, `/curatr fix`, `/approve`.

Or via Docker Compose:

```bash
docker-compose --profile openclaw up -d
```

### 7. Use Medplum as the backing FHIR store (optional)

Set in `.env` (leave `FHIR_UPSTREAM_URL` empty):

```bash
MEDPLUM_BASE_URL=https://api.medplum.com/fhir/R4
MEDPLUM_CLIENT_ID=<id>
MEDPLUM_CLIENT_SECRET=<secret>
```

All guardrails apply to Medplum responses identically to local SQLite mode.
Access tokens are cached in Redis (key `medplum:access_token`; falls back to
in-process cache when Redis is unavailable).

---

## Known Limitations

- Local mode: JSON blob storage with table-scan search (no indexed fields)
- Structural validation only (no StructureDefinition conformance or terminology binding)
- SubscriptionTopic stored but notifications not dispatched
- Clinical FHIR writes gate human-in-the-loop with a header flag (`X-Human-Confirmed`), not cryptographic confirmation — a compensating control for the demo, not proof a human acted. Real-world actions (phone/SMS/etc.) no longer use that header: `commit` only submits the action for out-of-band approval (202 `awaiting_confirmation`), and the patient's Approve tap consumes a single-use `ActionConfirmation` credential server-side before anything executes.
- OAuth endpoints are for discovery/SMART advertisement; route enforcement is via step-up + read-auth tokens, and the auto-approve authorize flow is limited to public/demo tenants (no per-user consent screen)
- No historical versioning (version_id increments but old versions not retrievable)
- Upstream proxy: no response caching, no cross-version translation
- **Security is config-dependent — production requires** `READ_AUTH_ENABLED=true` (authenticate non-public reads), `INTERNAL_TOKEN_MINT_SECRET` (gate token mint/seed for non-public tenants; fail-closed in prod when unset), `PUBLIC_TENANTS` limited to synthetic demo tenants, a real `SESSION_SECRET`/`STEP_UP_SECRET`, and https-only upstreams
- Step-up tokens are valid for multiple writes within their 5-min TTL (not single-use); irreversible actions rely on state-machine idempotency (guarded `WHERE status='proposed'` claim) rather than nonce consumption

## Contributing — this is a community effort

HealthClaw Guardrails is developed in the open as a shared reference, not a commercial product.
The guardrail layer between AI agents and clinical data only gets trustworthy if a lot of people
with different vantage points pressure-test it. We especially want:

- **Implementers** building FHIR × MCP integrations — tell us where the patterns break in the real world.
- **Clinicians & compliance folks** — challenge the redaction profiles, audit model, and the documented HIPAA postures.
- **Standards people** (HL7 / SDC / SMART) — tell us where we've diverged from the spec, especially on `$populate`/`$extract`.
- **Anyone** — open an issue, file a "you got this wrong," or send a PR.

Start here: **[CONTRIBUTING.md](CONTRIBUTING.md)** · **[Roadmap](ROADMAP.md)** · **[Dev Guide](docs/development.md)** · **[Code of Conduct](CODE_OF_CONDUCT.md)** · **[CHANGELOG.md](CHANGELOG.md)** · **[Security policy](SECURITY.md)**

Good first contributions are labeled in the issue tracker. Contributions are DCO-signed (`git commit -s`) under the [MIT license](LICENSE) — see [LICENSING.md](LICENSING.md) for the project's licensing posture going forward.

### Community

- **[GitHub Discussions](https://github.com/aks129/HealthClawGuardrails/discussions)** — questions, ideas, show-and-tell.
- **[good first issues](https://github.com/aks129/HealthClawGuardrails/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)** — scoped, ~15-minute-to-start contributions.
- Building on OpenClaw or Hermes? The skills are on [ClawHub](https://clawhub.ai/aks129/skills/fhir-r6-guardrails); the MCP server is in the [Hermes catalog](https://github.com/NousResearch/hermes-agent/pull/59221).

## License

MIT — free to use, fork, and build on. See [LICENSE](LICENSE).
