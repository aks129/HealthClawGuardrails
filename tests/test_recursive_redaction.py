"""PHI redaction must cover nested FHIR structures, not only Patient roots."""

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
