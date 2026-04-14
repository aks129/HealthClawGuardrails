---
name: healthex-export
description: >
  HealthClaw HealthEx Export (healthclaw.io) — automated personal health record
  export from the local HealthClaw FHIR store. Use when: (1) The patient wants to
  export all their health data from the HealthClaw local store as a portable FHIR
  bundle, (2) Migrating health data to a new tenant or archive, (3) Creating a
  de-identified snapshot for sharing with a provider or second opinion,
  (4) Pre-screening records for Curatr quality issues before a full evaluation,
  (5) Automating the HealthEx → local FHIR store ingestion pipeline.
metadata: {"openclaw":{"requires":{"bins":["python3"]},"install":[{"kind":"uv","packages":["requests"]}]}}
---

# HealthClaw HealthEx Export

`scripts/export_healthex.py` automates the full pipeline from local HealthClaw
FHIR store → de-identified, Curatr-pre-tagged FHIR R4 transaction Bundle.

Replaces the manual Claude session pull for personal health data export.

## Quick Start

```bash
# Basic export — all clinical resource types for a tenant
python scripts/export_healthex.py --tenant-id ev-personal

# Export + immediately import into a second tenant
python scripts/export_healthex.py \
    --tenant-id ev-personal \
    --import \
    --import-tenant ev-archive \
    --step-up-secret $STEP_UP_SECRET

# Preview what would be exported (no file written)
python scripts/export_healthex.py --tenant-id ev-personal --dry-run

# Export specific resource types
python scripts/export_healthex.py \
    --tenant-id ev-personal \
    --types Condition Observation AllergyIntolerance Immunization MedicationRequest
```

## Output

Bundle written to `exports/healthex-<YYYY-MM-DD>.json` by default.
Override with `--output /path/to/file.json`.

Format: FHIR R4 transaction Bundle (`"type": "transaction"`) with PUT entries,
ready for `POST /Bundle/$ingest-context` or direct use with `import_healthex.py`.

## Resource Types Exported (default)

| Type | Notes |
|---|---|
| Patient | De-identified: name/address/telecom removed, EHR identifiers stripped |
| Condition | Full — diagnoses, clinical/verification status, onset/abatement |
| Observation | Full — labs, vitals, social history (smoking, BMI, etc.) |
| AllergyIntolerance | Full |
| Immunization | Full |
| MedicationRequest | Full |
| Procedure | Full |
| DiagnosticReport | Full |
| CarePlan | Full |
| Coverage | Full |
| Encounter | Full |

Pass `--types <ResourceType> ...` to limit to a subset.

## De-identification

Applied automatically (skip with `--no-deidentify`).

**Removed from Patient:**
- `name` — full name
- `address` — street, city, state, zip
- `telecom` — phone, email
- `photo` — base64 images
- `contact` — emergency contacts
- OID-namespaced identifiers (EHR-internal MRNs, Epic IDs, CEID, EID, EMPI, etc.)

**Preserved:**
- `birthDate` — full date (patient-controlled export; use `$deidentify` for Safe Harbor)
- `gender`
- `communication` — language preferences
- A synthetic `urn:healthclaw:patient/<uuid>` identifier is injected

**Other resource types** are exported as-is — they contain clinical observations
and codes, not patient demographics.

> For HIPAA Safe Harbor de-identification (birthDate truncated to year, zip to 3 digits),
> call `POST /r6/fhir/Patient/:id/$deidentify` before export, or use the
> `phi-redaction` skill after import.

## Curatr Pre-Tags

Applied automatically (skip with `--no-pretag`).

Scans the export bundle for known data quality patterns and injects FHIR
extensions — **without modifying stored resources**. The tags exist only in the
exported bundle and serve as hints for a subsequent Curatr evaluation.

| Tag code | Trigger | Action recommended |
|---|---|---|
| `smoking_contradiction` | LOINC 72166-2 observations with conflicting SNOMED codes (e.g. "Never smoked" + "Ex-smoker" in same tenant) | Run `curatr_apply_fix` with patient attestation |
| `h_flag_titer` | Observation with `interpretation.code` of H/HH/HU, or valueString starting with "H" | Clinical review; consider flagging for provider |
| `missing_result` | Observation with no `value[x]` element | Verify whether result is pending, cancelled, or truly missing |

Pre-tags are FHIR extensions at:
`https://healthclaw.example.org/fhir/StructureDefinition/curatr-pretag`

Each extension carries a `valueCodeableConcept` with the tag code and display text.

### Reading Pre-Tags After Import

```bash
# Find all observations with Curatr pre-tags
curl -s "http://localhost:5000/r6/fhir/Observation?_count=200" \
  -H "X-Tenant-ID: ev-archive" \
  | python3 -c "
import json, sys
b = json.load(sys.stdin)
url = 'https://healthclaw.example.org/fhir/StructureDefinition/curatr-pretag'
for e in b.get('entry', []):
    r = e['resource']
    tags = [
        ext['valueCodeableConcept']['coding'][0]['code']
        for ext in r.get('extension', [])
        if ext.get('url') == url
    ]
    if tags:
        print(r['id'][:40], tags)
"
```

## Full Pipeline: HealthEx → Local FHIR Store

The complete automated flow replaces the previous manual Claude session pull:

```text
1. Patient data arrives via Fasten Connect webhook → ingested to tenant T
2. export_healthex.py --tenant-id T --import --import-tenant T-archive
   ├── Fetches all resources via REST API (paginated)
   ├── Strips EHR identifiers + PII (de-identification)
   ├── Pre-tags Curatr issue patterns
   └── POSTs bundle to /Bundle/$ingest-context for tenant T-archive
3. AI agent (MCP) queries T-archive for Curatr evaluation
4. curatr_apply_fix applied for patient-approved corrections
5. AuditEvent trail shows full chain: ingest → redact → evaluate → fix
```

## Arguments Reference

| Flag | Default | Description |
|---|---|---|
| `--tenant-id` | `desktop-demo` | Source tenant to export from |
| `--base-url` | `http://localhost:5000/r6/fhir` | HealthClaw FHIR base URL |
| `--types` | Full clinical set | Space-separated list of FHIR resource types |
| `--output` | `exports/healthex-<date>.json` | Output bundle file path |
| `--import` | off | Run `import_healthex.py` after export |
| `--import-tenant` | same as `--tenant-id` | Tenant to import the bundle into |
| `--step-up-secret` | `$STEP_UP_SECRET` env | HMAC secret (required with `--import`) |
| `--no-deidentify` | off | Skip de-identification |
| `--no-pretag` | off | Skip Curatr pre-tagging |
| `--dry-run` | off | Print counts only, do not write file |

## Related Scripts

| Script | Purpose |
|---|---|
| `scripts/import_healthex.py` | Import a FHIR bundle into HealthClaw with step-up auth |
| `scripts/convert_fasten.py` | Convert Fasten Health export format to FHIR transaction Bundle |

## Related Skills

- `fhir-r6-guardrails` — Stack setup and MCP tool reference
- `curatr` — Evaluate and fix data quality issues in exported records
- `phi-redaction` — HIPAA Safe Harbor de-identification (stricter than patient-controlled)
- `fasten-connect` — Patient-authorized EHR ingestion (upstream of this export)
