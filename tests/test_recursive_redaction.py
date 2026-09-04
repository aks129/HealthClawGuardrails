"""PHI redaction must cover nested FHIR structures, not only Patient roots.

## The name branch had no observer (#630 F2)

The canary loop in the first test below is the whole measurement, and until
2026-09 no canary in it could see a name. Its contained RelatedPerson is
`{"family": "Secret", "given": ["Janet"]}` and the canary is `"Jane Secret"` —
two strings that are never adjacent in the serialized JSON, so the name branch
of `_redact_fields` was unobserved by the file whose one-line summary is that
names below the root get redacted.

Scoping that branch to the root Patient —

    if resource.get('resourceType') == 'Patient' and 'name' in resource ...

— hands back contained RelatedPerson and Practitioner names in full, and the
whole suite stays green: 3157 passed, byte-identical to baseline
(2026-09-04). Deleting the branch outright IS caught, seven tests over; the
half-measure was not, which is the more likely edit.

`test_names_below_the_root_are_truncated_too` and
`test_a_root_resource_that_is_not_a_patient_is_redacted_too` below are the
observers. They assert the truncated SHAPE, not the absence of a phrase: an
absence assertion passes just as happily when the field was dropped, renamed,
or never reached.
"""

import json

from r6.redaction import apply_redaction
from r6.health_compliance import deidentify_resource


def test_recursive_redaction_removes_nested_phi_and_preserves_clinical_values():
    resource = {
        "resourceType": "Observation",
        "id": "obs-1",
        "status": "final",
        "code": {
            "coding": [{
                "system": "http://loinc.org",
                "code": "2339-0",
                "display": "Glucose for Jane Secret",
            }],
            "text": "Jane Secret glucose",
        },
        "subject": {
            "reference": "Patient/patient-1",
            "display": "Jane Secret",
            "identifier": {"system": "urn:mrn", "value": "MRN-SECRET-1234"},
        },
        "performer": [{
            "reference": "Practitioner/practitioner-1",
            "display": "Dr Secret",
        }],
        "extension": [{
            "url": "https://example.org/fhir/StructureDefinition/private-note",
            "valueString": "Jane Secret lives at 123 Main Street",
        }, {
            "url": "https://example.org/fhir/StructureDefinition/scan",
            "valueAttachment": {
                "contentType": "image/jpeg",
                "url": "https://signed.example/secret?token=abc",
                "data": "base64-secret-image",
                "title": "Jane Secret insurance card",
            },
        }, {
            "url": "https://example.org/fhir/StructureDefinition/private-date",
            "valueDate": "1984-06-17",
        }],
        "valueQuantity": {"value": 126, "unit": "mg/dL"},
        "contained": [{
            "resourceType": "RelatedPerson",
            "name": [{"family": "Secret", "given": ["Janet"]}],
            "telecom": [{"system": "phone", "value": "+1-555-0100"}],
            "address": [{
                "line": ["123 Main Street"],
                "city": "Secretville",
                "postalCode": "12345",
                "state": "NY",
                "country": "US",
            }],
        }],
    }

    output = apply_redaction(resource)
    serialized = json.dumps(output)

    for canary in (
        "Jane Secret", "Dr Secret", "123 Main Street", "Secretville",
        "base64-secret-image", "signed.example", "+1-555-0100",
        "MRN-SECRET-1234", "1984-06-17",
    ):
        assert canary not in serialized

    assert output["code"]["coding"][0]["code"] == "2339-0"
    assert output["valueQuantity"] == {"value": 126, "unit": "mg/dL"}
    assert output["subject"]["reference"] == "Patient/patient-1"
    assert output["extension"][2]["valueDate"] == "1984"


def test_names_below_the_root_are_truncated_too():
    """A contained RelatedPerson and Practitioner are people, and named.

    `_redact_recursive` walks into `contained[]` and calls `_redact_fields`
    on each entry, so a nested person's name is truncated exactly like the
    root's. This row is what notices if that stops being true.

    MUTATION (run 2026-09-04, both directions, see PR): scope the
    name-truncation block in `r6/redaction.py::_redact_fields` to
    `resource.get('resourceType') == 'Patient'` -> red, both names in full.
    """
    resource = {
        "resourceType": "Observation",
        "id": "obs-contained-names",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0"}]},
        "subject": {"reference": "Patient/patient-1"},
        "contained": [{
            "resourceType": "RelatedPerson",
            "id": "rp-1",
            "name": [{"family": "Marguerite", "given": ["Josephine"]}],
        }, {
            "resourceType": "Practitioner",
            "id": "prac-1",
            "name": [{"family": "Quintaviou", "given": ["Bartholomew"]}],
        }],
        "valueQuantity": {"value": 126, "unit": "mg/dL"},
    }

    output = apply_redaction(resource)
    contained = output.get("contained") or []

    # Non-vacuity: the entries and their name arrays must still be there. An
    # absence check over a tree that dropped `contained` proves nothing.
    assert [c.get("resourceType") for c in contained] == [
        "RelatedPerson", "Practitioner"]
    assert all(c.get("name") for c in contained), (
        "a contained name array vanished, so the truncation assertions below "
        "would pass without measuring truncation: " + json.dumps(contained))

    assert contained[0]["name"][0] == {"family": "M.", "given": ["J."]}
    assert contained[1]["name"][0] == {"family": "Q.", "given": ["B."]}

    serialized = json.dumps(output)
    for canary in ("Marguerite", "Josephine", "Quintaviou", "Bartholomew"):
        assert canary not in serialized, (
            "a contained resource's name survived redaction: " + canary)


def test_a_root_resource_that_is_not_a_patient_is_redacted_too():
    """The same rule one level up: a Practitioner ROOT is a person too.

    Scoping the name branch to Patient de-redacts this as well as the
    contained case above, and a reader of #630 F2 would only look at
    `contained`. Pinned here so the blast radius has an observer of its own.
    """
    output = apply_redaction({
        "resourceType": "Practitioner",
        "id": "prac-root-1",
        "name": [{"family": "Vasilakopoulos", "given": ["Anastasia"],
                  "text": "Dr Anastasia Vasilakopoulos"}],
        "birthDate": "1971-11-02",
    })

    assert output.get("name"), (
        "the name array is gone, so an absence check would be vacuous: "
        + json.dumps(output))
    assert output["name"][0] == {"family": "V.", "given": ["A."]}

    serialized = json.dumps(output)
    for canary in ("Vasilakopoulos", "Anastasia"):
        assert canary not in serialized


def test_deidentification_preview_recurses_through_nested_fhir_values():
    resource = {
        "resourceType": "DiagnosticReport",
        "id": "report-secret",
        "subject": {
            "reference": "Patient/patient-secret",
            "display": "Jane Secret",
            "identifier": {"value": "MRN-SECRET"},
        },
        "presentedForm": [{
            "contentType": "application/pdf",
            "url": "https://signed.example/report?token=secret",
            "data": "base64-secret-report",
            "title": "Jane Secret report",
        }],
        "extension": [{
            "url": "https://example.org/private-note",
            "valueString": "Jane Secret private note",
        }],
        "effectiveDateTime": "2025-04-03T12:30:00Z",
        "code": {"coding": [{"system": "http://loinc.org", "code": "58410-2"}]},
    }

    output = deidentify_resource(resource)
    serialized = json.dumps(output)

    for canary in (
        "Jane Secret", "MRN-SECRET", "signed.example", "base64-secret-report",
        "patient-secret", "2025-04-03",
    ):
        assert canary not in serialized
    assert output["effectiveDateTime"] == "2025"
    assert output["code"]["coding"][0]["code"] == "58410-2"
