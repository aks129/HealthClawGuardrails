"""
tests/test_export_healthex.py

Unit tests for export_healthex_mcp.py covering:
- TabularParser (dict refs, @N resolution, empty-cell inheritance)
- FHIR mappers (condition, observation, immunization, allergy, vital)
- CuratrPreTagger (contradiction, misleading flag, missing result, ICD-9, care gap)
- De-identification (patient-controlled mode)
- Bundle builder (entry count, idempotent IDs, PUT vs POST)
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from export_healthex_mcp import (
    CuratrPreTagger,
    TabularParser,
    build_bundle,
    deidentify_patient_controlled,
    map_allergy,
    map_condition,
    map_immunization,
    map_observation_lab,
    map_patient,
    map_vital,
    stable_id,
)


# ---------------------------------------------------------------------------
# TabularParser
# ---------------------------------------------------------------------------

class TestTabularParser:

    def test_parse_basic_condition(self):
        text = """#Conditions 3y|Total:1
D:1=2018-09-17|
S:1=active|
Sys:1=http://snomed.info/sct|
Date|Condition|ClinicalStatus|OnsetDate|AbatementDate|SNOMED|ICD10|PreferredCode|PreferredSystem
2018-09-17|Psoriasis|active|2017-02-13||9014002|L40.9|9014002|http://snomed.info/sct
"""
        rows = TabularParser.parse([text])
        assert len(rows) == 1
        assert rows[0]["Condition"] == "Psoriasis"
        assert rows[0]["SNOMED"] == "9014002"
        assert rows[0]["ICD10"] == "L40.9"

    def test_resolves_at_references(self):
        text = """D:1=2025-09-12|
S:1=active|
Date|Condition|ClinicalStatus
@1|Shoulder pain|@1
"""
        rows = TabularParser.parse([text])
        assert rows[0]["Date"] == "2025-09-12"
        assert rows[0]["ClinicalStatus"] == "active"

    def test_empty_cell_inherits_previous(self):
        text = """Date|Condition|ClinicalStatus|SNOMED|ICD10
2025-09-12|Impingement|active|118944007|M25.811
|Shoulder pain|active|1260146002|M25.511
"""
        rows = TabularParser.parse([text])
        assert rows[1]["Date"] == "2025-09-12"
        assert rows[1]["Condition"] == "Shoulder pain"

    def test_skips_pagination_hints(self):
        text = """Date|Condition|SNOMED|ICD10
2017-02-13|Psoriasis|9014002|L40.9

---
**Pagination Info:**
- Date Range: 2014-04-05 to 2017-04-05
\u26a0\ufe0f **IMPORTANT**: This response contains only partial data.
To retrieve the remaining data, you MUST call the get_conditions tool again with:
  - beforeDate: "2014-04-04"
"""
        rows = TabularParser.parse([text])
        assert len(rows) == 1

    def test_multipage_deduplication_not_done_in_parser(self):
        page1 = "Date|Condition|SNOMED|ICD10\n2025-01-01|Flu|6142004|J11.1\n"
        page2 = "Date|Condition|SNOMED|ICD10\n2024-01-01|Cold|82272006|J00\n"
        rows = TabularParser.parse([page1, page2])
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# FHIR mappers
# ---------------------------------------------------------------------------

class TestMapCondition:

    def test_basic_condition(self):
        row = {
            "Condition": "Psoriasis",
            "ClinicalStatus": "active",
            "OnsetDate": "2017-02-13",
            "SNOMED": "9014002",
            "ICD10": "L40.9",
            "Date": "2018-09-17",
        }
        r = map_condition(row)
        assert r is not None
        assert r["resourceType"] == "Condition"
        codes = {c["code"] for c in r["code"]["coding"]}
        assert "9014002" in codes
        assert "L40.9" in codes
        assert r["onsetDateTime"] == "2017-02-13"

    def test_returns_none_if_no_codes(self):
        row = {"Condition": "Unknown", "ClinicalStatus": "active", "Date": "2025-01-01"}
        assert map_condition(row) is None

    def test_returns_none_if_no_name(self):
        row = {"Condition": "", "SNOMED": "9014002", "ICD10": "L40.9", "Date": "2025-01-01"}
        assert map_condition(row) is None

    def test_deterministic_id(self):
        row = {"Condition": "Psoriasis", "SNOMED": "9014002", "ICD10": "L40.9", "OnsetDate": "2017-02-13", "Date": "2018-09-17"}
        r1 = map_condition(row)
        r2 = map_condition(row)
        assert r1["id"] == r2["id"]

    def test_encounter_reference(self):
        row = {
            "Condition": "Shoulder pain", "SNOMED": "118944007", "ICD10": "M25.811",
            "Date": "2025-09-12", "Encounter": "enc-abc123",
        }
        r = map_condition(row)
        assert r["encounter"]["identifier"]["value"] == "enc-abc123"


class TestMapObservation:

    def test_lab_with_quantity(self):
        row = {"Code": "22322-2", "Test": "Hep B S Ab", "Result": "59.38", "Date": "2014-10-17", "Flag": "H"}
        r = map_observation_lab(row)
        assert r is not None
        assert r["valueQuantity"]["value"] == 59.38
        assert r["interpretation"][0]["coding"][0]["code"] == "H"

    def test_lab_positive_negative(self):
        row = {"Code": "", "Test": "Hepatitis C Ab", "Result": "NEGATIVE", "Date": "2014-10-17", "Flag": ""}
        r = map_observation_lab(row)
        assert r["valueCodeableConcept"]["coding"][0]["code"] == "260385009"

    def test_smoking_status_mapped_to_snomed(self):
        row = {"Code": "72166-2", "Test": "Smoking History", "Result": "Former", "Date": "2025-09-22", "Flag": ""}
        r = map_observation_lab(row)
        assert r["category"][0]["coding"][0]["code"] == "social-history"
        codes = {c["code"] for c in r["valueCodeableConcept"]["coding"]}
        assert "8517006" in codes  # Ex-smoker

    def test_smoking_never_mapped(self):
        row = {"Code": "72166-2", "Test": "Smoking History", "Result": "Never", "Date": "2017-02-13", "Flag": ""}
        r = map_observation_lab(row)
        codes = {c["code"] for c in r["valueCodeableConcept"]["coding"]}
        assert "266919005" in codes  # Never smoked

    def test_returns_none_without_date(self):
        row = {"Code": "22322-2", "Test": "Hep B", "Result": "59.38", "Date": "", "Flag": ""}
        assert map_observation_lab(row) is None

    def test_deterministic_id(self):
        row = {"Code": "22322-2", "Test": "Hep B S Ab", "Result": "59.38", "Date": "2014-10-17", "Flag": "H"}
        r1 = map_observation_lab(row)
        r2 = map_observation_lab(row)
        assert r1["id"] == r2["id"]


class TestMapImmunization:

    def test_flu_with_lot(self):
        row = {
            "Immunization": "Influenza Quad", "CVX": "150", "OccurrenceDate": "2022-10-05",
            "Status": "completed", "LotNumber": "AJ3JX", "ExpirationDate": "2023-06-30",
            "Dose": "0.5 mL", "Site": "Left arm", "Performers": "Valerie C",
        }
        r = map_immunization(row)
        assert r is not None
        assert r["lotNumber"] == "AJ3JX"
        assert r["doseQuantity"]["value"] == 0.5
        assert r["site"]["text"] == "Left arm"

    def test_covid_dose_number(self):
        row = {"Immunization": "Sars-Cov-2 (Moderna)", "CVX": "207", "OccurrenceDate": "2021-01-22", "Status": "completed", "LotNumber": "012L20A", "Dose": "0.5 mL", "Site": "Right arm"}
        r = map_immunization(row)
        assert r["occurrenceDateTime"] == "2021-01-22"
        assert r["vaccineCode"]["coding"][0]["code"] == "207"

    def test_returns_none_without_date(self):
        row = {"Immunization": "Flu", "CVX": "150", "OccurrenceDate": "", "Status": "completed"}
        assert map_immunization(row) is None

    def test_deterministic_id(self):
        row = {"Immunization": "Flu", "CVX": "150", "OccurrenceDate": "2022-10-05", "Status": "completed"}
        assert map_immunization(row)["id"] == map_immunization(row)["id"]


class TestMapAllergy:

    def test_nka(self):
        row = {
            "Allergy": "No Known Allergies", "SNOMED": "716186003",
            "ClinicalStatus": "active", "VerificationStatus": "confirmed",
            "Date": "2017-02-13",
        }
        r = map_allergy(row)
        assert r is not None
        assert r["code"]["coding"][0]["code"] == "716186003"
        assert r["clinicalStatus"]["coding"][0]["code"] == "active"

    def test_returns_none_without_name(self):
        assert map_allergy({"Allergy": "", "Date": "2017-02-13"}) is None


class TestMapVital:

    def test_pain_score(self):
        row = {"Type": "Pain Score", "Value": "0-No pain", "Unit": "", "Date": "2022-10-05", "Flag": ""}
        r = map_vital(row)
        assert r is not None
        assert r["valueString"] == "0-No pain"

    def test_returns_none_without_date(self):
        assert map_vital({"Type": "Heart Rate", "Value": "72", "Unit": "bpm", "Date": "", "Flag": ""}) is None


# ---------------------------------------------------------------------------
# Curatr pre-tagger
# ---------------------------------------------------------------------------

def _smoking_obs(obs_id: str, result_snomed: str, result_text: str, date: str) -> dict:
    return {
        "resourceType": "Observation",
        "id": obs_id,
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "social-history"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "72166-2"}]},
        "effectiveDateTime": date,
        "valueCodeableConcept": {
            "coding": [{"system": "http://snomed.info/sct", "code": result_snomed}],
            "text": result_text,
        },
    }


def _hepb_obs() -> dict:
    return {
        "resourceType": "Observation",
        "id": "obs-hepb",
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "22322-2"}], "text": "Hep B S Ab"},
        "effectiveDateTime": "2014-10-17",
        "valueQuantity": {"value": 59.38, "unit": "mIU/mL"},
        "interpretation": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation", "code": "H"}]}],
    }


def _lab_no_result() -> dict:
    return {
        "resourceType": "Observation",
        "id": "obs-mumps",
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "7966-5"}], "text": "Mumps IgG"},
        "effectiveDateTime": "2014-10-17",
    }


def _icd9_condition() -> dict:
    return {
        "resourceType": "Condition",
        "id": "cond-icd9",
        "clinicalStatus": {"coding": [{"system": "...", "code": "active"}]},
        "code": {
            "coding": [
                {"system": "http://hl7.org/fhir/sid/icd-9-cm", "code": "250.00", "display": "Diabetes mellitus"},
            ]
        },
        "subject": {"reference": "Patient/pt-ev-hclaw"},
    }


def _psoriasis_condition() -> dict:
    return {
        "resourceType": "Condition",
        "id": "cond-psoriasis",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "9014002"}]},
        "subject": {"reference": "Patient/pt-ev-hclaw"},
    }


class TestCuratrPreTagger:

    def test_smoking_contradiction_detected(self):
        resources = [
            _smoking_obs("obs-a", "266919005", "Never", "2017-02-13"),
            _smoking_obs("obs-b", "8517006", "Former", "2025-09-22"),
        ]
        tagger = CuratrPreTagger(resources)
        tagger.run()
        issue_codes = [i["code"] for i in tagger.issues]
        assert issue_codes.count("contradiction") == 2

    def test_no_contradiction_single_smoking_obs(self):
        resources = [_smoking_obs("obs-a", "8517006", "Former", "2025-09-22")]
        tagger = CuratrPreTagger(resources)
        tagger.run()
        assert not any(i["code"] == "contradiction" for i in tagger.issues)

    def test_misleading_hepb_flag(self):
        resources = [_hepb_obs()]
        tagger = CuratrPreTagger(resources)
        tagger.run()
        assert any(i["code"] == "misleading-interpretation" for i in tagger.issues)

    def test_missing_lab_result_flagged(self):
        resources = [_lab_no_result()]
        tagger = CuratrPreTagger(resources)
        tagger.run()
        assert any(i["code"] == "missing-result" for i in tagger.issues)
        # Resource should have been updated
        assert resources[0]["status"] == "unknown"
        assert "dataAbsentReason" in resources[0]

    def test_icd9_code_flagged(self):
        resources = [_icd9_condition()]
        tagger = CuratrPreTagger(resources)
        tagger.run()
        assert any(i["code"] == "icd9-deprecated" for i in tagger.issues)
        assert tagger.issues[0]["severity"] == "CRITICAL"

    def test_care_gap_psoriasis_no_treatment(self):
        resources = [_psoriasis_condition()]
        tagger = CuratrPreTagger(resources)
        tagger.run()
        assert any(i["code"] == "care-gap:no-treatment" for i in tagger.issues)

    def test_no_care_gap_when_med_present(self):
        resources = [
            _psoriasis_condition(),
            {"resourceType": "MedicationRequest", "id": "med-1", "status": "active",
             "intent": "order", "medicationCodeableConcept": {"text": "Methotrexate"},
             "subject": {"reference": "Patient/pt-ev-hclaw"}},
        ]
        tagger = CuratrPreTagger(resources)
        tagger.run()
        assert not any(i["code"] == "care-gap:no-treatment" for i in tagger.issues)

    def test_curatr_tag_added_to_resource_meta(self):
        resources = [_hepb_obs()]
        tagger = CuratrPreTagger(resources)
        tagger.run()
        tags = resources[0].get("meta", {}).get("tag", [])
        assert any(t["system"] == "https://healthclaw.io/curatr" for t in tags)

    def test_note_added_to_resource(self):
        resources = [_lab_no_result()]
        tagger = CuratrPreTagger(resources)
        tagger.run()
        notes = resources[0].get("note", [])
        assert any("CURATR" in n.get("text", "") for n in notes)


# ---------------------------------------------------------------------------
# De-identification
# ---------------------------------------------------------------------------

class TestDeidentify:

    def test_phi_removed(self):
        patient = {
            "resourceType": "Patient",
            "id": "pt-ev-hclaw",
            "name": [{"family": "Vestel", "given": ["Eugene"]}],
            "telecom": [{"system": "phone", "value": "412-555-0100"}],
            "address": [{"line": ["123 Main St"], "city": "Pittsburgh", "state": "PA"}],
            "gender": "male",
            "birthDate": "1981-05-29",
            "photo": [{"url": "http://example.com/photo.jpg"}],
            "identifier": [{"system": "urn:mrn", "value": "MRN12345"}],
        }
        result = deidentify_patient_controlled(patient, "ev-2026")
        assert "name" not in result
        assert "telecom" not in result
        assert "address" not in result
        assert "photo" not in result

    def test_clinical_elements_preserved(self):
        patient = {
            "resourceType": "Patient",
            "id": "pt-ev-hclaw",
            "name": [{"family": "Vestel"}],
            "gender": "male",
            "birthDate": "1981-05-29",
        }
        result = deidentify_patient_controlled(patient, "ev-2026")
        assert result["gender"] == "male"
        assert result["birthDate"] == "1981-05-29"

    def test_healthclaw_identifier_injected(self):
        patient = {"resourceType": "Patient", "id": "pt-ev-hclaw", "gender": "male", "birthDate": "1981-05-29"}
        result = deidentify_patient_controlled(patient, "ev-2026")
        ids = result.get("identifier", [])
        assert any(i.get("system") == "https://healthclaw.io/patients" for i in ids)

    def test_mri_identifier_removed(self):
        patient = {
            "resourceType": "Patient",
            "id": "pt-ev-hclaw",
            "identifier": [
                {"system": "urn:mrn", "value": "MRN12345"},
                {"system": "urn:ssn", "value": "123-45-6789"},
            ],
            "gender": "male",
            "birthDate": "1981-05-29",
        }
        result = deidentify_patient_controlled(patient, "ev-2026")
        for idf in result.get("identifier", []):
            assert idf.get("system") == "https://healthclaw.io/patients"

    def test_non_patient_resource_unchanged(self):
        obs = {"resourceType": "Observation", "id": "obs-1", "status": "final"}
        result = deidentify_patient_controlled(obs, "ev-2026")
        assert result == obs


# ---------------------------------------------------------------------------
# Bundle builder
# ---------------------------------------------------------------------------

class TestBuildBundle:

    def test_entry_count(self):
        resources = [
            map_patient("ev-2026", "male", "1981-05-29"),
            map_condition({"Condition": "Psoriasis", "SNOMED": "9014002", "ICD10": "L40.9", "Date": "2018-09-17"}),
        ]
        bundle = build_bundle(resources, "ev-2026")
        assert len(bundle["entry"]) == 2

    def test_bundle_type(self):
        bundle = build_bundle([map_patient("ev-2026", "male", "1981-05-29")], "ev-2026")
        assert bundle["resourceType"] == "Bundle"
        assert bundle["type"] == "transaction"

    def test_patient_uses_put(self):
        resources = [map_patient("ev-2026", "male", "1981-05-29")]
        bundle = build_bundle(resources, "ev-2026")
        assert bundle["entry"][0]["request"]["method"] == "PUT"

    def test_condition_uses_post(self):
        resources = [
            map_condition({"Condition": "Psoriasis", "SNOMED": "9014002", "ICD10": "L40.9", "Date": "2018-09-17"})
        ]
        bundle = build_bundle(resources, "ev-2026")
        assert bundle["entry"][0]["request"]["method"] == "POST"

    def test_meta_tags_present(self):
        bundle = build_bundle([], "ev-2026")
        tag_codes = {t["code"] for t in bundle["meta"]["tag"]}
        assert "patient-controlled" in tag_codes
        assert "deidentified" in tag_codes

    def test_bundle_is_valid_json(self):
        resources = [map_patient("ev-2026", "male", "1981-05-29")]
        bundle = build_bundle(resources, "ev-2026")
        assert json.loads(json.dumps(bundle)) == bundle


# ---------------------------------------------------------------------------
# Stable ID
# ---------------------------------------------------------------------------

class TestStableId:

    def test_deterministic(self):
        assert stable_id("Psoriasis", "9014002", "L40.9", "2017-02-13") == \
               stable_id("Psoriasis", "9014002", "L40.9", "2017-02-13")

    def test_different_inputs_different_ids(self):
        assert stable_id("Psoriasis", "9014002") != stable_id("Flu", "6142004")

    def test_length_is_12(self):
        assert len(stable_id("a", "b", "c")) == 12
