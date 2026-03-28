# HealthClaw Guardrails

The security layer between AI agents and clinical data. A [healthclaw.io](https://healthclaw.io) open source project.

**v1.0.0** | 266 tests | 12 MCP tools | FHIR R4 US Core v9 + R6 v6.0.0-ballot3

> FHIR standardized how health data is structured. MCP standardized how AI connects to tools.
> Nobody standardized the guardrails in between. This project does.

## What It Does

This is a **vendor-neutral guardrail proxy** that sits between any AI agent and any FHIR server. Every request passes through:

- **PHI redaction** — Names truncated to initials, identifiers masked, addresses stripped, birth dates truncated to year
- **Immutable audit trail** — Every read/write logged with tenant, agent, timestamp
- **Step-up authorization** — HMAC-SHA256 tokens required for writes
- **Human-in-the-loop** — Clinical writes blocked until a human confirms (HTTP 428)
- **Tenant isolation** — Every query scoped to tenant, cross-tenant access blocked
- **Medical disclaimers** — Injected on all clinical resource reads

```text
AI Agent ──▶ MCP Server ──▶ Guardrail Proxy ──▶ Any FHIR Server
                              ↓                    (HAPI, Epic,
                         PHI redaction              Medplum, etc.)
                         Audit trail
                         Step-up auth
                         Human-in-the-loop
```

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

# Services:
# - fhir-mcp-guardrails (Flask, port 5000)
# - agent-orchestrator (MCP server, port 3001)
# - redis (port 6379)
```

## MCP Tools (12)

**Read tools** (no step-up required):

| Tool | Description |
| --- | --- |
| `context.get` | Retrieve pre-built context envelopes |
| `fhir.read` | Read a FHIR resource (redacted) |
| `fhir.search` | Search with patient, code, status, date filters |
| `fhir.validate` | Structural validation |
| `fhir.stats` | Observation statistics (count/min/max/mean) |
| `fhir.lastn` | Most recent N observations per code |
| `fhir.permission_evaluate` | R6 Permission access control evaluation |
| `fhir.subscription_topics` | List available SubscriptionTopics |
| `curatr.evaluate` | Evaluate a FHIR resource for data quality issues |

**Write tools** (require step-up token):

| Tool | Description |
| --- | --- |
| `fhir.propose_write` | Validate + preview without committing |
| `fhir.commit_write` | Commit with step-up auth + human-in-the-loop |
| `curatr.apply_fix` | Apply patient-approved fixes with Provenance tracking |

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
# Python tests (266 tests)
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

## Known Limitations

- Local mode: JSON blob storage with table-scan search (no indexed fields)
- Structural validation only (no StructureDefinition conformance or terminology binding)
- SubscriptionTopic stored but notifications not dispatched
- Human-in-the-loop is a header flag, not cryptographic confirmation
- OAuth endpoints implemented but not enforced on routes (demonstration only)
- No historical versioning (version_id increments but old versions not retrievable)
- Upstream proxy: no response caching, no cross-version translation

## License

MIT
