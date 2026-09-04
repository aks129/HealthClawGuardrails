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


def test_patient_controlled_recurses_into_contained_and_display():
    """#617: this function used to stop at the top level. A Bundle stamped
    ANONYED still carried a contained RelatedPerson's name and phone, the
    patient's own name via subject.display, and a clinician's name via
    generalPractitioner[].display — three independent escapes, none of them
    hypothetical (this is the commonest real-feed shape: a subject reference
    carries both an id and a human-readable display).

    Property asserted on the actual output, at any depth — not on whether a
    specific top-level field was touched.

    MUTATION: skip the `_redact_recursive(result)` call at the top of
    apply_patient_controlled_redaction -> red, all five values below survive.
    """
    resource = {
        "resourceType": "Patient",
        "id": "pt-1",
        "name": [{"family": "Rivera", "given": ["Marisol"]}],
        "subject": {"display": "Marisol Rivera"},
        "generalPractitioner": [{"display": "Dr. Alice Nguyen"}],
        "contained": [{
            "resourceType": "RelatedPerson",
            "name": [{"family": "Rivera", "given": ["Esteban"]}],
            "telecom": [{"system": "phone", "value": "617-555-0199"}],
        }],
    }
    out = apply_patient_controlled_redaction(resource, "hc-1")
    blob = json.dumps(out)
    for leaked in ("Marisol", "Esteban", "617-555-0199", "Alice Nguyen",
                   "Rivera"):
        assert leaked not in blob, f"{leaked!r} survived: {out!r}"

    # The base pass is the standard profile, not full removal, on nested
    # structures — a contained resource still carries a stripped shape
    # (initials, [Redacted]) rather than vanishing outright. Only the TOP
    # level gets this function's stronger, verbatim-removal policy.
    contained_name = out["contained"][0]["name"][0]
    assert contained_name["family"] == "R." and contained_name["given"] == ["E."]
    assert out["contained"][0]["telecom"][0]["value"] == "[Redacted]"

    # The top-level policy is unaffected by running the base pass first —
    # name is still fully absent, not merely initialed, at the top level.
    assert "name" not in out
