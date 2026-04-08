---
name: quickstart
description: Set up HealthClaw Guardrails end-to-end and test every guardrail feature with real or sample FHIR health data. Walks through PHI redaction, Curatr data quality checks, step-up auth, MCP tools, and the Fasten Connect demo.
argument-hint: [fhir-json-file-or-leave-blank-for-sample-data]
disable-model-invocation: true
allowed-tools: Bash Read Write Grep Glob
---

# HealthClaw Guardrails — Quick Start

You are setting up HealthClaw Guardrails and walking the user through every major feature end-to-end. Work through each phase below in order. Show real output at each step; don't skip ahead.

## Phase 0 — Determine data source

Check if the user passed a FHIR JSON file path as an argument: `$ARGUMENTS`

- If `$ARGUMENTS` is a file path, read that file. It may be a single FHIR resource (Patient, Observation, Condition, MedicationRequest) or a Bundle. Confirm what resource types are present.
- If `$ARGUMENTS` is blank, tell the user you'll use built-in sample data and proceed.

## Phase 1 — Environment setup

Run these in order and confirm each succeeds before continuing:

```bash
cd "$(git rev-parse --show-toplevel)"
uv sync
```

Check that the venv is healthy:
```bash
uv run python -c "import flask, sqlalchemy; print('deps OK')"
```

Generate a STEP_UP_SECRET for this session:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Save the printed secret — you'll export it as `STEP_UP_SECRET` for all subsequent curl calls.

Check if port 5000 is already in use:
```bash
curl -s http://localhost:5000/r6/fhir/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "NOT_RUNNING"
```

If Flask is not running, instruct the user:
> **Open a second terminal and run:**
> ```
> STEP_UP_SECRET=<secret-from-above> uv run python main.py
> ```
> Then tell me when it's up (or I'll poll for it).

Poll until Flask is up (retry up to 20 times, 2-second sleep):
```bash
for i in $(seq 1 20); do
  STATUS=$(curl -s http://localhost:5000/r6/fhir/health 2>/dev/null)
  echo "$STATUS" | python3 -m json.tool 2>/dev/null && break
  echo "Waiting... ($i/20)" && sleep 2
done
```

Show the health response. Confirm `"status": "healthy"` before continuing.

---

## Phase 2 — Load health data

### 2a — Choose a tenant ID

Generate a test tenant:
```bash
TENANT_ID="quickstart-$(date +%s)"
echo "Tenant: $TENANT_ID"
```

### 2b — Load data

**If using sample data**, POST these four resources. For each, capture the returned `id`:

**Patient:**
```bash
curl -s -X POST http://localhost:5000/r6/fhir/Patient \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{
    "resourceType": "Patient",
    "name": [{"use": "official", "family": "Rivera", "given": ["Maria", "Elena"]}],
    "birthDate": "1985-03-15",
    "address": [{"line": ["123 Clinical Ave"], "city": "Boston", "state": "MA", "postalCode": "02101"}],
    "telecom": [{"system": "phone", "value": "617-555-0198"}],
    "identifier": [{"system": "http://example.org/mrn", "value": "MRN-2026-4471"}]
  }'
```

**Condition (with deprecated ICD-9 code — triggers Curatr warning):**
```bash
curl -s -X POST http://localhost:5000/r6/fhir/Condition \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d "{
    \"resourceType\": \"Condition\",
    \"subject\": {\"reference\": \"Patient/$PATIENT_ID\"},
    \"code\": {\"coding\": [{\"system\": \"http://hl7.org/fhir/sid/icd-9-cm\", \"code\": \"250.00\", \"display\": \"Diabetes mellitus without mention of complication\"}]},
    \"clinicalStatus\": {\"coding\": [{\"system\": \"http://terminology.hl7.org/CodeSystem/condition-clinical\", \"code\": \"active\"}]},
    \"verificationStatus\": {\"coding\": [{\"system\": \"http://terminology.hl7.org/CodeSystem/condition-ver-status\", \"code\": \"confirmed\"}]}
  }"
```

**Observation (blood glucose):**
```bash
curl -s -X POST http://localhost:5000/r6/fhir/Observation \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d "{
    \"resourceType\": \"Observation\",
    \"status\": \"final\",
    \"subject\": {\"reference\": \"Patient/$PATIENT_ID\"},
    \"code\": {\"coding\": [{\"system\": \"http://loinc.org\", \"code\": \"2339-0\", \"display\": \"Glucose [Mass/volume] in Blood\"}]},
    \"valueQuantity\": {\"value\": 180, \"unit\": \"mg/dL\", \"system\": \"http://unitsofmeasure.org\", \"code\": \"mg/dL\"},
    \"effectiveDateTime\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
  }"
```

**MedicationRequest (for Curatr check):**
```bash
curl -s -X POST http://localhost:5000/r6/fhir/MedicationRequest \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d "{
    \"resourceType\": \"MedicationRequest\",
    \"status\": \"active\",
    \"intent\": \"order\",
    \"subject\": {\"reference\": \"Patient/$PATIENT_ID\"},
    \"medicationCodeableConcept\": {\"coding\": [{\"system\": \"http://www.nlm.nih.gov/research/umls/rxnorm\", \"code\": \"860975\", \"display\": \"Metformin 500 MG Oral Tablet\"}]}
  }"
```

**If using user-supplied FHIR JSON**, POST whatever resources are in `$ARGUMENTS` to the appropriate endpoint (`/r6/fhir/<ResourceType>`). Extract resource IDs from the responses.

Capture the patient ID from the POST response and set `PATIENT_ID=<id>`.

---

## Phase 3 — PHI redaction

Read back the patient and show what the AI agent sees vs. what was stored:

```bash
curl -s http://localhost:5000/r6/fhir/Patient/$PATIENT_ID \
  -H "X-Tenant-ID: $TENANT_ID" | python3 -m json.tool
```

Point out exactly which fields were redacted:
- `name` → initials only (e.g. `M. E. Rivera`)
- `identifier` → masked (e.g. `***4471`)
- `telecom` → `[Redacted]`
- `address` → city/state only, street stripped
- `birthDate` → year only (`1985`)

Explain: *This is what every AI agent connected via MCP receives. The full data is stored; agents never see it.*

---

## Phase 4 — Curatr data quality evaluation

Run Curatr on the Condition with the ICD-9 code:

```bash
curl -s -X POST "http://localhost:5000/r6/fhir/Condition/$CONDITION_ID/\$curatr-evaluate" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{}' | python3 -m json.tool
```

Show the `issues` array and explain each finding (deprecated_code_system, invalid_icd10, missing_rxnorm, display_mismatch, etc.).

Then run Curatr on the MedicationRequest too:
```bash
curl -s -X POST "http://localhost:5000/r6/fhir/MedicationRequest/$MED_ID/\$curatr-evaluate" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{}' | python3 -m json.tool
```

---

## Phase 5 — Step-up authorization (write gate)

### 5a — Attempt a write without a step-up token (should get 401):
```bash
curl -s -X PUT "http://localhost:5000/r6/fhir/Condition/$CONDITION_ID" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{"resourceType":"Condition","id":"'$CONDITION_ID'","status":"inactive"}' \
  -w "\nHTTP %{http_code}"
```

Expected: `401 Unauthorized`. Explain: *All write operations require an HMAC step-up token.*

### 5b — Get a step-up token:
```bash
curl -s -X POST http://localhost:5000/r6/fhir/auth/stepup \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{"reason": "quickstart demo — correcting deprecated ICD-9 code", "resource_type": "Condition"}' \
  | python3 -m json.tool
```

Capture the token from the response. Set `STEPUP_TOKEN=<token>`.

### 5c — Propose a write (MCP workflow):
```bash
curl -s -X POST http://localhost:5000/r6/fhir/Condition/$CONDITION_ID/propose \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -H "X-Step-Up-Token: $STEPUP_TOKEN" \
  -d "{
    \"resourceType\": \"Condition\",
    \"id\": \"$CONDITION_ID\",
    \"subject\": {\"reference\": \"Patient/$PATIENT_ID\"},
    \"code\": {\"coding\": [{\"system\": \"http://hl7.org/fhir/sid/icd-10-cm\", \"code\": \"E11.9\", \"display\": \"Type 2 diabetes mellitus without complications\"}]},
    \"clinicalStatus\": {\"coding\": [{\"system\": \"http://terminology.hl7.org/CodeSystem/condition-clinical\", \"code\": \"active\"}]},
    \"verificationStatus\": {\"coding\": [{\"system\": \"http://terminology.hl7.org/CodeSystem/condition-ver-status\", \"code\": \"confirmed\"}]}
  }" | python3 -m json.tool
```

If the response is `428 Precondition Required`, explain: *This is the human-in-the-loop gate. Clinical resource writes require explicit human confirmation.*

### 5d — Commit with human confirmation:
```bash
curl -s -X POST http://localhost:5000/r6/fhir/Condition/$CONDITION_ID/propose \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -H "X-Step-Up-Token: $STEPUP_TOKEN" \
  -H "X-Human-Confirmed: true" \
  -d "{
    \"resourceType\": \"Condition\",
    \"id\": \"$CONDITION_ID\",
    \"subject\": {\"reference\": \"Patient/$PATIENT_ID\"},
    \"code\": {\"coding\": [{\"system\": \"http://hl7.org/fhir/sid/icd-10-cm\", \"code\": \"E11.9\", \"display\": \"Type 2 diabetes mellitus without complications\"}]},
    \"clinicalStatus\": {\"coding\": [{\"system\": \"http://terminology.hl7.org/CodeSystem/condition-clinical\", \"code\": \"active\"}]},
    \"verificationStatus\": {\"coding\": [{\"system\": \"http://terminology.hl7.org/CodeSystem/condition-ver-status\", \"code\": \"confirmed\"}]}
  }" | python3 -m json.tool
```

Expected: `200 OK`. The ICD-9 code is now replaced with ICD-10.

---

## Phase 6 — Audit trail

Show every action recorded for this tenant:

```bash
curl -s "http://localhost:5000/r6/fhir/AuditEvent?tenant=$TENANT_ID&_count=20" \
  -H "X-Tenant-ID: $TENANT_ID" | python3 -m json.tool
```

Point out: each entry has `action` (C/R/U/D), `recorded` timestamp, `agent` (the requester), and `entity` (the resource touched). This log is append-only — no entry can be deleted or modified.

---

## Phase 7 — Fasten Connect end-to-end demo

Trigger the 5-step animated demo (simulates: patient auth → webhook → NDJSON ingest → PHI redact → audit):

```bash
curl -s -X POST http://localhost:5000/fasten/demo \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{"tenant_id": "'$TENANT_ID'"}' | python3 -m json.tool
```

Walk through each of the 5 steps in the response and explain what happened.

---

## Phase 8 — MCP tools (optional, if Node.js available)

Check if the MCP orchestrator dependencies are installed:
```bash
ls services/agent-orchestrator/node_modules/.bin/ts-node 2>/dev/null && echo "READY" || echo "RUN: cd services/agent-orchestrator && npm ci"
```

If ready, start the MCP server (instruct user to run in a third terminal):
```
cd services/agent-orchestrator && MCP_PORT=3001 FHIR_BASE_URL=http://localhost:5000/r6/fhir npm start
```

Then demonstrate the `fhir_search` tool via the HTTP bridge:
```bash
curl -s -X POST http://localhost:3001/mcp/rpc \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{"tool": "fhir_search", "params": {"resourceType": "Condition", "patient": "'$PATIENT_ID'"}}' \
  | python3 -m json.tool
```

The response will include a `_mcp_summary` block with clinical context and limitations — that's what Claude Desktop sees when it calls this tool.

---

## Phase 9 — Re-run Curatr after the fix

Run Curatr on the Condition again to show the ICD-9 warning is gone:

```bash
curl -s -X POST "http://localhost:5000/r6/fhir/Condition/$CONDITION_ID/\$curatr-evaluate" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{}' | python3 -m json.tool
```

Expected: `"issues": []` or only non-critical findings. The fix worked.

---

## Phase 10 — Summary

Print a concise table of everything demonstrated:

| Feature | Endpoint | Result |
| --- | --- | --- |
| PHI Redaction | `GET /r6/fhir/Patient/:id` | Name/DOB/address/phone stripped |
| Curatr ICD-9 detection | `POST /Condition/:id/$curatr-evaluate` | `deprecated_code_system` issue found |
| Step-up token gate | `PUT /r6/fhir/...` without token | `401 Unauthorized` |
| Human-in-the-loop gate | `propose` without `X-Human-Confirmed` | `428 Precondition Required` |
| Confirmed write | `propose` + `X-Human-Confirmed: true` | `200 OK`, resource updated |
| Audit trail | `GET /r6/fhir/AuditEvent` | All actions recorded, append-only |
| Fasten Connect demo | `POST /fasten/demo` | 5-step EHR ingestion flow |
| Curatr after fix | `POST /Condition/:id/$curatr-evaluate` | Issues cleared |

Then ask: *What would you like to explore next — try with a real FHIR server upstream, connect via Claude Desktop MCP, or test the OAuth 2.1 SMART flow?*

---

## Appendix — Claude Desktop MCP setup

To run this same demo interactively in **Claude Desktop** (so Claude can call the MCP tools directly, not just via curl):

### Step 1 — Build the MCP server

```bash
cd services/agent-orchestrator
npm ci
npm run build          # compiles TypeScript → dist/
```

### Step 2 — Edit Claude Desktop config

Open the Claude Desktop config file:

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Add this block (replace `<REPO_PATH>` with the absolute path to this repo, and `<STEP_UP_SECRET>` with your generated secret):

```json
{
  "mcpServers": {
    "healthclaw-guardrails": {
      "command": "node",
      "args": ["<REPO_PATH>/services/agent-orchestrator/dist/index.js"],
      "env": {
        "MCP_PORT": "3001",
        "FHIR_BASE_URL": "http://localhost:5000/r6/fhir",
        "STEP_UP_SECRET": "<STEP_UP_SECRET>",
        "ALLOWED_ORIGINS": "https://claude.ai"
      }
    }
  }
}
```

**Windows path example:**

```json
"args": ["C:\\Users\\YourName\\Documents\\HealthClawGuardrails\\services\\agent-orchestrator\\dist\\index.js"]
```

### Step 3 — Start the Flask backend

In a terminal (keep it running):

```bash
STEP_UP_SECRET=<same-secret> uv run python main.py
```

### Step 4 — Restart Claude Desktop

Quit and relaunch Claude Desktop. The **healthclaw-guardrails** MCP server appears in the tools panel (hammer icon).

### Step 5 — End-to-end demo prompt

Paste this into Claude Desktop to run the full demo:

```text
I want to test HealthClaw Guardrails. I have a patient named Maria Rivera (DOB 1985-03-15) with a diabetes diagnosis coded as ICD-9 250.00 and a blood glucose reading of 180 mg/dL.

Please:
1. Use fhir_search to check what Patient resources exist
2. Create the patient record and condition using fhir_propose_write (use tenant "desktop-demo")
3. Read back the patient with fhir_read and show me what PHI was redacted
4. Run curatr_evaluate on the Condition and explain any data quality issues found
5. Propose a fix (ICD-10 E11.9) using curatr_apply_fix — walk me through the step-up auth and human confirmation steps
6. Show the audit trail with fhir_search on AuditEvent
```

Claude will use the 12 MCP tools, hit the real Flask guardrail stack, and walk you through each guardrail as it triggers.
