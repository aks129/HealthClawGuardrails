---
name: fhir-r6-guardrails
version: 1.0.0
description: >
  FHIR R6 agent guardrails for secure clinical data access via MCP. Use when:
  (1) Reading patient data through MCP with automatic PHI redaction,
  (2) Writing clinical resources with two-phase propose/commit and step-up auth,
  (3) Proxying requests to real FHIR servers (HAPI, SMART Health IT, Epic),
  (4) Auditing AI agent access to healthcare data,
  (5) Evaluating R6 Permission resources for access control decisions.
  Provides 10 MCP tools with guardrail enforcement on every request.
metadata:
  openclaw:
    requires:
      env:
        - STEP_UP_SECRET
      bins:
        - node
        - python3
    install:
      - kind: node
        packages:
          - "@modelcontextprotocol/sdk"
          - express
          - node-fetch
      - kind: uv
        packages:
          - flask
          - flask-sqlalchemy
          - httpx
    primaryEnv: STEP_UP_SECRET
---

# FHIR R6 MCP Guardrail Patterns

A runtime security layer for AI agent access to FHIR clinical data via Model Context
Protocol (MCP). This is NOT a knowledge skill — it runs an MCP server that enforces
PHI redaction, step-up authorization, immutable audit trails, and tenant isolation
on every request.

## Setup

### Docker Compose (recommended)

```bash
# Clone the repository
git clone https://github.com/aks129/fhir-mcp-guardrails.git
cd fhir-mcp-guardrails

# Set required secret
export STEP_UP_SECRET=$(openssl rand -hex 32)

# Start all services
docker-compose up -d --build
```

### Manual Setup

```bash
# Python backend (Flask)
uv sync
export STEP_UP_SECRET=$(openssl rand -hex 32)
python main.py &

# MCP server (Node.js)
cd services/agent-orchestrator
npm ci
npm start
```

### Connect to a Real FHIR Server

```bash
export FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4
# All guardrails apply to upstream responses automatically
```

## MCP Server Endpoints

- **Streamable HTTP**: `POST http://localhost:3001/mcp` (preferred)
- **SSE**: `GET http://localhost:3001/sse` (legacy MCP transport)
- **HTTP bridge**: `POST http://localhost:3001/mcp/rpc` (non-MCP clients)

## Available MCP Tools

### Read Tools (no step-up required)

**`context.get`** — Retrieve a pre-built context envelope with patient-centric FHIR
resources. Returns bounded, policy-stamped, time-limited context.

**`fhir.read`** — Read a single FHIR resource by type and ID. Response is automatically
redacted (names to initials, identifiers masked, dates truncated).

**`fhir.search`** — Search FHIR resources with filters: patient, code, status,
_lastUpdated, _count (max 50), _sort. Returns a redacted Bundle.

**`fhir.validate`** — Validate a proposed FHIR resource against structural rules.
Returns OperationOutcome with errors and warnings.

**`fhir.stats`** — Compute count/min/max/mean over Observation valueQuantity values.
Standard FHIR $stats operation (since R4). Filter by patient and/or LOINC code.

**`fhir.lastn`** — Get the last N observations per code. Standard FHIR $lastn.
Sorted by storage order, not effectiveDateTime.

**`fhir.permission_evaluate`** — Evaluate R6 Permission resources for access control.
Returns permit/deny with reasoning. Separates access control from consent records.

**`fhir.subscription_topics`** — List available SubscriptionTopics for event-driven
subscriptions.

### Write Tools (require step-up token)

**`fhir.propose_write`** — Validate and preview a write without committing. Identifies
clinical resource types that require human-in-the-loop confirmation.

**`fhir.commit_write`** — Commit a proposed write. Requires `X-Step-Up-Token` header
(HMAC-SHA256, 5-min TTL). Clinical resources also require `X-Human-Confirmed: true`.

## Write Workflow

Always use the two-phase pattern:

1. Call `fhir.propose_write` with the resource and operation type
2. Check `proposal_status` — must be `"ready"` to proceed
3. Note `requires_human_confirmation` for clinical types (Observation, Condition, etc.)
4. Call `fhir.commit_write` with step-up token and human confirmation headers

## Security Guardrails

All guardrails are enforced automatically on every request:

- **PHI Redaction**: Names → initials, identifiers → last 4 chars, addresses stripped,
  birth dates → year only, photos removed, notes → [Redacted]
- **Audit Trail**: Append-only AuditEvent for every resource access
- **Step-Up Auth**: HMAC-SHA256 tokens with nonce, TTL, tenant binding for writes
- **Tenant Isolation**: X-Tenant-Id enforced at database layer on every query
- **URL Rewriting**: Upstream server URLs never leak to clients
- **Medical Disclaimers**: Injected on clinical resource reads

## Supported Resource Types

Patient, Encounter, Observation, AuditEvent, Consent, Permission, SubscriptionTopic,
Subscription, NutritionIntake, NutritionProduct, DeviceAlert, DeviceAssociation,
Requirements, ActorDefinition.

## R6-Specific Resources

- **Permission** — Access control resource with $evaluate operation (separate from Consent)
- **DeviceAlert** — ISO/IEEE 11073 device alarms
- **NutritionIntake** — Dietary consumption tracking
- **SubscriptionTopic** — Restructured pub/sub (introduced R5, maturing R6)

## Limitations

- Local mode: SQLite JSON blob storage, table scans for filtering
- Structural validation only (no StructureDefinition or terminology binding)
- Human-in-the-loop is header-based, not cryptographic
- Upstream proxy: no response caching, no cross-version translation
- SubscriptionTopic stored but notifications not dispatched
