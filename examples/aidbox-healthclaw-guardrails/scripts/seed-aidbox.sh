#!/usr/bin/env bash
# Seed Aidbox with one synthetic patient: one Patient, three Observations,
# one Condition. Written straight to Aidbox, NOT through the guardrail proxy,
# because the point of the walkthrough is to compare the two paths over the
# same stored bytes.
#
# Everything here is synthetic. Maria Alvarez does not exist.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a

AIDBOX_URL="${AIDBOX_URL:-http://localhost:8080}"
AIDBOX_CLIENT="${AIDBOX_CLIENT:-root}"
AIDBOX_SECRET="${AIDBOX_SECRET:-qNbQS6sw82}"

echo "Seeding ${AIDBOX_URL} ..."

response=$(curl -sS -u "${AIDBOX_CLIENT}:${AIDBOX_SECRET}" \
  -X POST "${AIDBOX_URL}/fhir" \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "request": { "method": "PUT", "url": "/Patient/pt-demo" },
      "resource": {
        "resourceType": "Patient",
        "id": "pt-demo",
        "name": [{ "given": ["Maria"], "family": "Alvarez" }],
        "identifier": [{ "system": "urn:mrn", "value": "MRN-88214" }],
        "gender": "female",
        "birthDate": "1974-03-11",
        "telecom": [{ "system": "phone", "value": "555-867-5309" }],
        "address": [{ "line": ["221 Baker St"], "city": "Pittsburgh", "state": "PA", "postalCode": "15213" }]
      }
    },
    {
      "request": { "method": "PUT", "url": "/Condition/cond-demo" },
      "resource": {
        "resourceType": "Condition",
        "id": "cond-demo",
        "subject": { "reference": "Patient/pt-demo" },
        "clinicalStatus": {
          "coding": [{ "system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active" }]
        },
        "code": {
          "coding": [{ "system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "E11.9", "display": "Type 2 diabetes mellitus without complications" }]
        },
        "onsetDateTime": "2019-06-01"
      }
    },
    {
      "request": { "method": "PUT", "url": "/Observation/obs-a1c" },
      "resource": {
        "resourceType": "Observation",
        "id": "obs-a1c",
        "status": "final",
        "subject": { "reference": "Patient/pt-demo" },
        "effectiveDateTime": "2026-07-14",
        "category": [{ "coding": [{ "system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory" }] }],
        "code": { "coding": [{ "system": "http://loinc.org", "code": "4548-4", "display": "Hemoglobin A1c/Hemoglobin.total in Blood" }] },
        "valueQuantity": { "value": 8.1, "unit": "%", "system": "http://unitsofmeasure.org", "code": "%" }
      }
    },
    {
      "request": { "method": "PUT", "url": "/Observation/obs-glucose" },
      "resource": {
        "resourceType": "Observation",
        "id": "obs-glucose",
        "status": "final",
        "subject": { "reference": "Patient/pt-demo" },
        "effectiveDateTime": "2026-07-14",
        "category": [{ "coding": [{ "system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory" }] }],
        "code": { "coding": [{ "system": "http://loinc.org", "code": "2339-0", "display": "Glucose [Mass/volume] in Blood" }] },
        "valueQuantity": { "value": 180, "unit": "mg/dL", "system": "http://unitsofmeasure.org", "code": "mg/dL" }
      }
    },
    {
      "request": { "method": "PUT", "url": "/Observation/obs-ldl" },
      "resource": {
        "resourceType": "Observation",
        "id": "obs-ldl",
        "status": "final",
        "subject": { "reference": "Patient/pt-demo" },
        "effectiveDateTime": "2026-07-14",
        "category": [{ "coding": [{ "system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory" }] }],
        "code": { "coding": [{ "system": "http://loinc.org", "code": "13457-7", "display": "Cholesterol in LDL [Mass/volume] in Serum or Plasma by calculation" }] },
        "valueQuantity": { "value": 142, "unit": "mg/dL", "system": "http://unitsofmeasure.org", "code": "mg/dL" }
      }
    }
  ]
}
JSON
)

# A transaction Bundle returns 200 even when an entry failed, so check the
# entries rather than the status code. Reporting a seed that did not happen
# as a success is the failure mode this whole example is about.
python3 - "$response" <<'PY'
import json, sys
body = json.loads(sys.argv[1])
entries = body.get("entry", [])
if not entries:
    print("SEED FAILED: no entries in the response", file=sys.stderr)
    print(json.dumps(body, indent=2)[:2000], file=sys.stderr)
    sys.exit(1)
bad = [e for e in entries
       if not str((e.get("response") or {}).get("status", "")).startswith(("200", "201"))]
if bad:
    print(f"SEED FAILED: {len(bad)} of {len(entries)} entries rejected", file=sys.stderr)
    print(json.dumps(bad, indent=2)[:2000], file=sys.stderr)
    sys.exit(1)
print(f"Seeded {len(entries)} resources: 1 Patient, 1 Condition, 3 Observations.")
PY
