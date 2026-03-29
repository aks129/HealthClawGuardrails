---
name: curatr
description: >
  HealthClaw Curatr (healthclaw.io) — patient-facing FHIR data quality evaluation
  and correction. Use when: (1) Evaluating a patient's health record for coding
  issues (deprecated code systems, invalid codes, missing required fields),
  (2) Presenting issues in plain language with clinical impact, (3) Applying
  patient-approved corrections with full Provenance tracking, (4) Preparing a
  structured correction request for the patient's healthcare provider. Supports
  FHIR R4 US Core v9 resources: Condition, AllergyIntolerance, MedicationRequest,
  Immunization, Procedure, DiagnosticReport — with ICD-10-CM, SNOMED CT, LOINC,
  CVX, and RxNorm validation via public terminology APIs.
metadata: {"openclaw":{"requires":{"env":["STEP_UP_SECRET"],"bins":["node","python3"]},"install":[{"kind":"node","packages":["@modelcontextprotocol/sdk","express","node-fetch"]},{"kind":"uv","packages":["flask","flask-sqlalchemy","requests"]}],"primaryEnv":"STEP_UP_SECRET"}}
---

# HealthClaw Curatr

A patient-facing data quality skill that evaluates FHIR health records for coding
issues, presents them in plain language, and lets the patient decide how to fix them.

Your health data belongs to you. Curatr helps you understand what's in it, spot
problems, and take action — whether that's correcting your personal record or
requesting your provider fix the source.

## Supported FHIR R4 US Core v9 Resource Types

| Resource | Checks |
|---|---|
| Condition | ICD-9 detection, ICD-10-CM/SNOMED validity, clinicalStatus, verificationStatus |
| AllergyIntolerance | clinicalStatus, patient link, allergen code (RxNorm/SNOMED) |
| MedicationRequest | status, intent, medication code (RxNorm) |
| Immunization | status, vaccineCode (CVX/SNOMED), occurrenceDateTime |
| Procedure | status, procedure code (SNOMED/CPT) |
| DiagnosticReport | status, report code (LOINC) |

For all other resource types, Curatr runs a generic coding scan checking all `coding[]`
elements for deprecated systems.

## What Curatr Checks

### Code System Quality

- **Deprecated systems**: ICD-9-CM codes (retired October 2015) flagged as critical
- **Code validity**: Live lookups against public terminology services — no account needed
- **Display name accuracy**: Checks whether the written description matches the official term

### Structural Completeness

- Missing required fields (varies by resource type — see table above)
- Invalid status values (e.g. unrecognized clinicalStatus codes)
- Missing standard medical codes

### Terminology Services Used (all public, no auth required)

| Service | Systems Validated |
|---|---|
| [tx.fhir.org](https://tx.fhir.org) — HL7 public FHIR terminology server | SNOMED CT, LOINC |
| [NLM Clinical Tables API](https://clinicaltables.nlm.nih.gov) | ICD-10-CM |
| [RXNAV API](https://rxnav.nlm.nih.gov) | RxNorm |

## MCP Tools

### `curatr.evaluate` (read-only)

Evaluate a FHIR resource for data quality issues.

**Input:**
```json
{
  "resource_type": "Condition",
  "resource_id": "cond-001"
}
```

**Output:** Issues list with plain-language descriptions, impact, and suggestions.
Each issue includes `severity` (critical / warning / info / suggestion), `field_path`,
`plain_language`, `impact`, and `suggestion`.

**No step-up required.** Safe to call at any time.

---

### `curatr.apply_fix` (write — requires step-up + human confirmation)

Apply patient-approved fixes to a FHIR resource. Creates a linked Provenance record.

**Input:**
```json
{
  "resource_type": "Condition",
  "resource_id": "cond-001",
  "fixes": [
    {
      "field_path": "Condition.code.coding[0].system",
      "new_value": "http://hl7.org/fhir/sid/icd-10-cm"
    },
    {
      "field_path": "Condition.code.coding[0].code",
      "new_value": "E11.9"
    },
    {
      "field_path": "Condition.code.coding[0].display",
      "new_value": "Type 2 diabetes mellitus without complications"
    }
  ],
  "patient_intent": "Updating from retired ICD-9 to ICD-10-CM equivalent"
}
```

**Requires:** `X-Step-Up-Token` header + `X-Human-Confirmed: true`

**Output:** Updated resource + linked Provenance resource documenting the change.

---

## Conversation Flow (OpenClaw Messenger)

Always follow this sequence — never apply fixes without patient approval:

```
1. Call curatr.evaluate to get issues
2. For each issue, present:
   - issue.title (bold headline)
   - issue.plain_language (what the problem is, in plain English)
   - issue.impact (why it matters to the patient)
   - issue.suggestion (what to do about it)
3. Ask the patient which fixes they approve
4. Confirm their intent in their own words (used in Provenance)
5. Call curatr.apply_fix with ONLY the approved fixes
6. Confirm completion and show the Provenance record summary
```

### Example Messenger Presentation

```
Curatr found 2 issues in your Diabetes condition record:

─────────────────────────────────────────────────────────
CRITICAL: Outdated code system
Your record uses ICD-9-CM — a code system retired in October 2015.
Most US health systems and insurers no longer accept ICD-9 codes.

Impact: This record may not match your condition when shared with
providers or insurance systems, potentially causing delays.

Suggested fix: Update to ICD-10-CM code E11.9 — "Type 2 diabetes
mellitus without complications"
─────────────────────────────────────────────────────────
INFO: Missing verification status
No indication of whether this diagnosis has been confirmed.

Impact: Without a verification status, AI tools and care summaries
may not know how to weight this condition.

Suggested fix: Add verificationStatus = "confirmed" (or "provisional"
if still being evaluated by your provider)
─────────────────────────────────────────────────────────

What would you like to do?
  [A] Fix both in my personal health record
  [B] Fix the code system only
  [C] Prepare a correction request for my provider
  [D] Keep as-is for now
```

## Severity Levels

| Level | Meaning | Example |
|---|---|---|
| `critical` | Code system deprecated or structurally broken | ICD-9 code in 2026 |
| `warning` | Code invalid or display name wrong | Invalid clinicalStatus value |
| `info` | Recommended field missing | No verificationStatus |
| `suggestion` | A better value exists | Display name differs from canonical |

## Provenance — What Gets Recorded

Every fix creates a FHIR Provenance resource linked to the updated resource:
- **Who**: "Patient via HealthClaw Curatr"
- **When**: ISO timestamp
- **What changed**: Field-level change summary
- **Why**: Patient's stated intent (verbatim)
- **Activity code**: `UPDATE / revise` from HL7 v3 DataOperation
- **Reason code**: `PATADMIN` (patient administration)

The original source record is preserved in the AuditEvent trail. Provenance
does not erase history — it adds attribution.

## Provider Correction Request

If the patient wants their provider to fix the source record:
1. Summarize the issues in a human-readable format
2. Reference the FHIR resource ID and the suggested corrections
3. Note the public terminology service that flagged the issue
4. The patient can bring this summary to their provider portal, a patient
   advocate appointment, or submit via the provider's patient access process

## Setup

This skill runs on top of the HealthClaw Guardrails stack.
See the `fhir-r6-guardrails` skill for installation instructions.

```bash
# Curatr requires the same stack — no additional services needed
export STEP_UP_SECRET=$(openssl rand -hex 32)
docker-compose up -d --build

# MCP tools available at:
# POST http://localhost:3001/mcp  (Streamable HTTP, preferred)
# GET  http://localhost:3001/sse  (SSE, legacy)
```

## Limitations

- Terminology lookups require internet access to tx.fhir.org, NLM, and RXNAV
- Live lookups may be skipped if a public service is unavailable (timeouts handled gracefully)
- Condition is the primary supported resource type; other resource types receive generic coding scans
- Fixes are applied at read time — the original ingested data is unchanged in upstream sources
- Provider correction requests are generated as structured summaries only; submission to EHR is out of scope
