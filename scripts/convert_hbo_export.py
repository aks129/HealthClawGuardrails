#!/usr/bin/env python3
"""Convert a Health Bank One MCP export into a FHIR R4 transaction Bundle.

HBO's MCP tools return XML-ish tool output wrapping JSON tables
({"columns": [...], "rows": [...]}) — NOT FHIR resources. This script maps
those tables onto FHIR R4 (Observation / Condition / Immunization /
MedicationRequest / AllergyIntolerance) suitable for
POST /r6/fhir/Bundle/$ingest-context.

PHI posture (deliberate): the Patient resource is built REDACTED from the
start — no name, no telecom, no address, birthDate coarsened to the year.
The clinical rows carry codes/values/dates only. The output file therefore
never holds direct identifiers, regardless of what the raw export contains.

Usage:
    python scripts/convert_hbo_export.py exports/hbo-2026-07-06.json \
        --out exports/hbo-fhir-bundle.json [--patient-id hbo-member]
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# code_and_system entries look like: "2093-3 (@loinc)" / "44054006 (@sct)" /
# "140 (@cvx)" / "310798 (@ontology)" — pick by preferred system per resource.
_SYSTEM_URLS = {
    "loinc": "http://loinc.org",
    "sct": "http://snomed.info/sct",
    "cvx": "http://hl7.org/fhir/sid/cvx",
    "cpt": "http://www.ama-assn.org/go/cpt",
    "icd9cm": "http://hl7.org/fhir/sid/icd-9-cm",
    "icd10cm": "http://hl7.org/fhir/sid/icd-10-cm",
    "rxnorm": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "ontology": "http://www.nlm.nih.gov/research/umls/rxnorm",  # HBO's RxNorm alias
}

_QTY_RE = re.compile(r"^\s*([<>]?=?)\s*([\d]+(?:\.\d+)?)\s*([^\d\s].*)?$")


def _codes(code_str, prefer):
    """Parse 'code (@system)' pairs; return codings ordered by `prefer`."""
    pairs = re.findall(r"([\w\-\. ]+?) \(@([a-z0-9:.]+)\)", code_str or "")
    out = []
    for system in prefer:
        for code, sys_key in pairs:
            if sys_key == system and system in _SYSTEM_URLS:
                out.append({"system": _SYSTEM_URLS[system], "code": code.strip()})
    return out


def _tables(result_str):
    dec = json.JSONDecoder()
    i, out = 0, []
    while True:
        j = result_str.find('{"columns"', i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(result_str[j:])
            out.append(obj)
            i = j + end
        except ValueError:
            i = j + 1
    return out


def _rows(export, tool):
    rec = export.get("records", {}).get(tool)
    if not isinstance(rec, dict):
        return []
    rows = []
    for t in _tables(rec.get("result", "")):
        cols = t.get("columns", [])
        for row in t.get("rows", []):
            rows.append(dict(zip(cols, row)))
    return rows


def _value(result):
    """'277 mg/dL' -> valueQuantity; free text -> valueString."""
    if result is None:
        return {}
    m = _QTY_RE.match(str(result))
    if m and m.group(3):
        unit = m.group(3).strip()
        if not re.search(r"\d{4}", unit):  # avoid ids like '90189283'
            return {"valueQuantity": {"value": float(m.group(2)), "unit": unit}}
    return {"valueString": str(result)}


def _redacted_patient(export, patient_id):
    """Build the Patient REDACTED: year-only DOB + gender, nothing else."""
    s = export.get("records", {}).get("get_patient_basic_info", {}).get("result", "")
    gender = (re.search(r"<gender>([^<]*)<", s) or [None, ""])[1].strip().lower()
    dob = (re.search(r"<date_of_birth>([^<]*)<", s) or [None, ""])[1].strip()
    year = dob[:4] if re.match(r"\d{4}", dob) else None
    patient = {"resourceType": "Patient", "id": patient_id}
    if gender in ("male", "female", "other", "unknown"):
        patient["gender"] = gender
    if year:
        patient["birthDate"] = year  # coarsened — matches HIPAA Safe Harbor posture
    return patient


def convert(export, patient_id="hbo-member"):
    subject = {"reference": f"Patient/{patient_id}"}
    resources = [_redacted_patient(export, patient_id)]

    for r in _rows(export, "get_lab_results"):
        codings = _codes(r.get("code_and_system"), ["loinc"])
        if not codings:
            continue
        resources.append({
            "resourceType": "Observation", "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                      "code": "laboratory"}]}],
            "code": {"coding": codings[:1], "text": (r.get("test") or "").strip()},
            "subject": subject,
            "effectiveDateTime": r.get("effective_date"),
            **_value(r.get("result")),
        })

    for r in _rows(export, "get_conditions"):
        codings = _codes(r.get("code_and_system"), ["sct", "icd10cm", "icd9cm"])
        resources.append({
            "resourceType": "Condition",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"coding": codings, "text": (r.get("condition") or "").strip()},
            "subject": subject,
            "recordedDate": r.get("recorded_date"),
        })

    for r in _rows(export, "get_immunizations"):
        codings = _codes(r.get("code_and_system"), ["cvx"])
        resources.append({
            "resourceType": "Immunization",
            "status": r.get("status") or "completed",
            "vaccineCode": {"coding": codings,
                            "text": (r.get("immunization_name") or "").strip()},
            "patient": subject,
            "occurrenceDateTime": r.get("occurrence_date"),
        })

    for r in _rows(export, "get_medications"):
        codings = _codes(r.get("code_and_system"), ["rxnorm", "ontology"])
        resources.append({
            "resourceType": "MedicationRequest",
            "status": (r.get("status") or "unknown").lower(),
            "intent": (r.get("intent") or "order").lower(),
            "medicationCodeableConcept": {
                "coding": codings[:1],
                "text": (r.get("medication_display") or r.get("medication_text") or "").strip()},
            "subject": subject,
            "authoredOn": r.get("occurrence_date"),
        })

    for r in _rows(export, "get_allergies"):
        codings = _codes(r.get("code_and_system"), ["sct"])
        resources.append({
            "resourceType": "AllergyIntolerance",
            "code": {"coding": codings, "text": (r.get("allergy_name") or "").strip()},
            "patient": subject,
            "recordedDate": r.get("recorded_date"),
        })

    entries = []
    for res in resources:
        req = {"method": "PUT", "url": f"Patient/{patient_id}"} \
            if res["resourceType"] == "Patient" \
            else {"method": "POST", "url": res["resourceType"]}
        entries.append({"resource": res, "request": req})
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export_file")
    ap.add_argument("--out", required=True)
    ap.add_argument("--patient-id", default="hbo-member")
    args = ap.parse_args()

    with open(args.export_file) as f:
        export = json.load(f)
    bundle = convert(export, patient_id=args.patient_id)

    counts = {}
    for e in bundle["entry"]:
        rt = e["resource"]["resourceType"]
        counts[rt] = counts.get(rt, 0) + 1
    with open(args.out, "w") as f:
        json.dump(bundle, f, indent=1)
    print(f"wrote {args.out}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
