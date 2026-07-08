---
name: fhir-r6-guardrails
description: >
  HealthClaw Guardrails (healthclaw.io) — FHIR agent guardrails for clinical data
  access via MCP. Supports FHIR R4 US Core v9 (stable) and FHIR R6 ballot3
  (experimental). Use when: (1) Reading patient data through MCP tools with
  automatic PHI redaction, (2) Writing clinical resources with two-phase
  propose/commit and step-up authorization, (3) Querying observation statistics
  or recent lab results, (4) Evaluating R6 Permission resources for access control
  decisions, (5) Auditing agent access to healthcare data. 14 MCP tools.
---

# HealthClaw Guardrails

A [healthclaw.io](https://healthclaw.io) open source project. Reference implementation
of security and compliance patterns for AI agent access to FHIR data via MCP.

Supports **FHIR R4 US Core v9** (stable) and **FHIR R6 v6.0.0-ballot3** (experimental).

**This is a runtime guardrail layer, not a knowledge skill.** It sits between any AI
agent and FHIR data (local or upstream), enforcing PHI redaction, audit trails,
step-up authorization, and tenant isolation on every request.

## When to Use This Skill

- You need to read, search, or write FHIR clinical resources through MCP
- You need PHI to be automatically redacted before the agent sees it
- You need an immutable audit trail of all agent access
- You need step-up authorization gates on write operations
- You need to evaluate R6 Permission resources for access control

## MCP Tools Available (12)

### Read Tools (no step-up required)

| Tool | Purpose |
|------|---------|
| `context.get` | Retrieve a pre-built context envelope with patient-centric resources |
| `fhir.read` | Read a single FHIR resource by type and ID (auto-redacted) |
| `fhir.search` | Search resources with patient, code, status, date filters |
| `fhir.validate` | Structural validation of a proposed resource |
| `fhir.stats` | Observation statistics: count, min, max, mean over valueQuantity |
| `fhir.lastn` | Most recent N observations per code |
| `fhir.permission_evaluate` | Evaluate R6 Permission for permit/deny with reasoning |
| `fhir.subscription_topics` | List available SubscriptionTopics |
| `curatr.evaluate` | Evaluate a FHIR resource for data quality issues |

### Write Tools (require step-up token)

| Tool | Purpose |
|------|---------|
| `fhir.propose_write` | Validate and preview a write without committing |
| `fhir.commit_write` | Commit a proposed write (requires X-Step-Up-Token) |
| `curatr.apply_fix` | Apply patient-approved data quality fixes with Provenance |

## Two-Phase Write Pattern

Writes always follow propose-then-commit:

1. **Propose**: Call `fhir.propose_write` with the resource and operation (create/update).
   This validates the resource and returns a preview. No data is changed.

2. **Review**: Check the proposal response:
   - `proposal_status: "ready"` means validation passed
   - `requires_human_confirmation: true` for clinical resource types
   - `requires_step_up: true` always for commits

3. **Commit**: Call `fhir.commit_write` with the same resource. Include:
   - `X-Step-Up-Token` header (HMAC-SHA256 signed, 5-minute TTL)
   - `X-Human-Confirmed: true` header for clinical resources

### Clinical Resource Types (require human-in-the-loop)

Observation, Condition, MedicationRequest, DiagnosticReport, AllergyIntolerance,
Procedure, CarePlan, Immunization, NutritionIntake, DeviceAlert.

## Supported FHIR Resource Types

### FHIR R4 US Core v9 (Stable)

Patient, Encounter, Observation, AuditEvent, Consent, Condition, Provenance,
AllergyIntolerance, Immunization, MedicationRequest, Medication, MedicationDispense,
Procedure, DiagnosticReport, CarePlan, CareTeam, Goal, DocumentReference,
Location, Organization, Practitioner, PractitionerRole, RelatedPerson,
Coverage, ServiceRequest, Specimen, FamilyMemberHistory.

### FHIR R6 ballot3 (Experimental)

Permission, SubscriptionTopic, Subscription, NutritionIntake, NutritionProduct,
DeviceAlert, DeviceAssociation, Requirements, ActorDefinition.

- **Permission** — Access control (separate from Consent). `$evaluate` operation.
- **DeviceAlert** — ISO/IEEE 11073 device alarms.
- **NutritionIntake** — Dietary consumption tracking.
- **DeviceAssociation, NutritionProduct, Requirements, ActorDefinition** — CRUD only.

## Security Guardrails (Always Active)

### PHI Redaction
Applied on every read path. Names truncated to initials, identifiers masked to
last 4 characters, addresses stripped to city/state/country, birth dates truncated
to year, photos removed, notes replaced with [Redacted].

### Audit Trail
Append-only AuditEvent records for every resource access. Database-level immutability.
Logs agent ID, tenant ID, resource accessed, and outcome.

### Step-Up Authorization
HMAC-SHA256 tokens with 128-bit nonce, 5-minute TTL, tenant binding.
Required for all write operations.

### Tenant Isolation
`X-Tenant-Id` header enforced on every query at the database layer.

## Search Parameters (Local Mode)

Supported: `patient` (reference), `code` (token), `status` (token),
`_lastUpdated` (date with ge/le/gt/lt prefix), `_count` (1-200),
`_sort` (_lastUpdated/-_lastUpdated), `_summary` (count).

Not supported in local mode: chaining, _include, _revinclude, modifiers.

In upstream proxy mode, all query parameters are forwarded to the upstream server.

## Setup

```bash
# Docker Compose (recommended)
docker-compose up -d --build

# Manual
uv sync && python main.py &
cd services/agent-orchestrator && npm ci && npm start
```

Required environment variables:
- `STEP_UP_SECRET` — HMAC secret for step-up tokens

Optional:
- `FHIR_UPSTREAM_URL` — Connect to a real FHIR server (e.g., https://hapi.fhir.org/baseR4)
- `FHIR_BASE_URL` — Backend URL (default: http://localhost:5000/r6/fhir)
- `MCP_PORT` — MCP server port (default: 3001)

## Known Limitations

- Local mode uses SQLite JSON blob storage (not indexed search)
- Structural validation only (no StructureDefinition or terminology binding)
- SubscriptionTopic stored but notifications not dispatched
- Human-in-the-loop is header-based, not cryptographic
- No historical versioning (version_id increments but old versions not retrievable)
- Upstream proxy: no response caching, no cross-version translation
