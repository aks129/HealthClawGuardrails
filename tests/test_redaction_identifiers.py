"""Identifier values are removed, not shortened.

HIPAA Safe Harbor §164.514(b)(2)(i) lists Social Security numbers (G),
medical record numbers (H) and account numbers (J) among the identifiers
that must be REMOVED. r6/redaction.py kept the last four characters of
every identifier value, and SECURITY.md said so
(docs/2026-08-16-hard-truths.md §4). A last-four SSN is a recognised
re-identification vector, so the truncation was a compensating control
wearing the wrong label.

`system` and `type` stay: a reader can still see that an MRN existed and
which system issued it, without learning its value.

MUTATION: restore `'***' + val[-4:]` in _redact_fields -> red.
"""

import json

from r6.redaction import apply_redaction, apply_patient_controlled_redaction

SSN = "000-00-9999"
MRN = "MRN-7749-XYZ"

PATIENT = {
    "resourceType": "Patient",
    "id": "p1",
    "identifier": [
        {"system": "http://hl7.org/fhir/sid/us-ssn", "value": SSN,
         "type": {"coding": [{"system": "http://terminology.hl7.org/"
                                        "CodeSystem/v2-0203",
                              "code": "SS"}]}},
        # No keyword in the system: the patient-controlled heuristic used
        # to let this one through with its value intact.
        {"system": "https://fhir.example-health.test/ids", "value": MRN,
         "type": {"coding": [{"system": "http://terminology.hl7.org/"
                                        "CodeSystem/v2-0203",
                              "code": "MR"}]}},
    ],
    "name": [{"family": "Rivera", "given": ["Marisol"]}],
}

OBSERVATION = {
    "resourceType": "Observation",
    "id": "o1",
    "status": "final",
    "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0"}]},
    # A dict-shaped identifier inside a Reference.
    "subject": {"reference": "Patient/p1",
                "identifier": {"system": "urn:mrn", "value": MRN}},
    "identifier": {"system": "urn:accession", "value": "ACC-2024-0042"},
    "valueQuantity": {"value": 126, "unit": "mg/dL"},
}


def _values(obj):
    """Every identifier value anywhere in a resource, list or dict shaped."""
    found = []
    if isinstance(obj, dict):
        idents = obj.get("identifier")
        if isinstance(idents, dict):
            idents = [idents]
        for ident in idents or []:
            if isinstance(ident, dict) and "value" in ident:
                found.append(ident["value"])
        for value in obj.values():
            found += _values(value)
    elif isinstance(obj, list):
        for item in obj:
            found += _values(item)
    return found


def test_standard_redaction_removes_every_identifier_value():
    out = apply_redaction(PATIENT)
    assert _values(out) == []
    blob = json.dumps(out)
    assert SSN not in blob and MRN not in blob
    assert "9999" not in blob, "no last-four suffix survives either"


def test_standard_redaction_keeps_identifier_system_and_type():
    """The kind of identifier is not PHI; the value is."""
    out = apply_redaction(PATIENT)
    assert [i["system"] for i in out["identifier"]] == [
        "http://hl7.org/fhir/sid/us-ssn",
        "https://fhir.example-health.test/ids"]
    assert [i["type"]["coding"][0]["code"] for i in out["identifier"]] == [
        "SS", "MR"]


def test_standard_redaction_reaches_dict_shaped_and_nested_identifiers():
    out = apply_redaction(OBSERVATION)
    assert _values(out) == []
    assert out["subject"]["reference"] == "Patient/p1"
    assert out["subject"]["identifier"]["system"] == "urn:mrn"
    assert out["identifier"]["system"] == "urn:accession"
    assert out["valueQuantity"] == {"value": 126, "unit": "mg/dL"}


def test_patient_controlled_leaves_only_the_healthclaw_identifier():
    """The docstring always said the healthclaw id is the SOLE identifier.
    The keyword filter it replaced let any identifier whose system did not
    contain 'mrn' / 'facility' / ... through, value and all.

    MUTATION: restore the keyword filter -> red (the example-health MRN
    survives)."""
    out = apply_patient_controlled_redaction(PATIENT, "hc-123")
    assert out["identifier"] == [
        {"system": "https://healthclaw.io/patient-id", "value": "hc-123"}]
    blob = json.dumps(out)
    assert SSN not in blob and MRN not in blob


def test_patient_controlled_still_preserves_birthdate_and_codes():
    """Unchanged posture: the patient keeps their own DOB and clinical codes."""
    resource = dict(PATIENT, birthDate="1984-07-02")
    out = apply_patient_controlled_redaction(resource, "hc-123")
    assert out["birthDate"] == "1984-07-02"
    obs = apply_patient_controlled_redaction(OBSERVATION, "hc-123")
    assert obs["code"]["coding"][0]["code"] == "2339-0"
