import json

from r6.models import R6Resource, db

INITIAL_EXPR_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-initialExpression"
)
OBS_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-observationExtract"
)
DEF_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-definitionExtract"
)


def _demo_questionnaire():
    return {
        "resourceType": "Questionnaire",
        "id": "healthclaw-intake",
        "url": "https://healthclaw.io/Questionnaire/healthclaw-intake",
        "version": "1.0.0",
        "status": "active",
        "title": "HealthClaw Demo Intake",
        "extension": [{"url": DEF_EXTRACT_URL, "valueCode": "Patient"}],
        "item": [
            {"linkId": "given-name", "type": "string", "text": "First name",
             "definition": "http://hl7.org/fhir/StructureDefinition/"
                           "Patient#Patient.name.given",
             "extension": [{"url": INITIAL_EXPR_URL,
                            "valueExpression": {"language": "text/fhirpath",
                                                "expression":
                                                    "%patient.name.given.first()"}}]},
            {"linkId": "family-name", "type": "string", "text": "Last name",
             "definition": "http://hl7.org/fhir/StructureDefinition/"
                           "Patient#Patient.name.family",
             "extension": [{"url": INITIAL_EXPR_URL,
                            "valueExpression": {"language": "text/fhirpath",
                                                "expression":
                                                    "%patient.name.family"}}]},
            {"linkId": "body-weight", "type": "quantity", "text": "Body weight",
             "code": [{"system": "http://loinc.org", "code": "29463-7"}],
             "extension": [{"url": OBS_EXTRACT_URL, "valueBoolean": True}]},
        ],
    }


def _store(app, resource, tenant_id, resource_id=None):
    with app.app_context():
        r = R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource_id or resource.get("id"),
            tenant_id=tenant_id,
        )
        db.session.add(r)
        db.session.commit()


def test_full_populate_then_extract_roundtrip(client, app, tenant_id,
                                              tenant_headers, auth_headers):
    _store(app, {"resourceType": "Patient", "id": "p1",
                 "name": [{"given": ["Ada"], "family": "Lovelace"}],
                 "birthDate": "1815-12-10"}, tenant_id)
    _store(app, {"resourceType": "Observation", "id": "o1", "status": "final",
                 "code": {"coding": [{"system": "http://loinc.org",
                                      "code": "29463-7"}]},
                 "subject": {"reference": "Patient/p1"},
                 "effectiveDateTime": "2026-06-01",
                 "valueQuantity": {"value": 70, "unit": "kg"}}, tenant_id)
    _store(app, _demo_questionnaire(), tenant_id)

    # 1. Populate from the seeded Questionnaire.
    pop = client.post(
        "/r6/fhir/Questionnaire/healthclaw-intake/$populate",
        headers=tenant_headers,
        json={"resourceType": "Parameters",
              "parameter": [{"name": "subject",
                             "valueReference": {"reference": "Patient/p1"}}]})
    assert pop.status_code == 200
    qr = _response_param(pop.get_json())
    # populate should fill the name from the patient and the weight from the obs
    given = _answer(qr, "given-name")
    assert given["valueString"] == "Ada"
    weight = _answer(qr, "body-weight")
    assert weight["valueQuantity"]["value"] == 70
    qr["status"] = "completed"

    # 2. Extract the completed response (dry run — assert the Bundle shape).
    ext = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract?dryRun=true",
        headers=auth_headers,
        json={"resourceType": "Parameters",
              "parameter": [
                  {"name": "questionnaire-response", "resource": qr},
                  {"name": "questionnaire", "resource": _demo_questionnaire()}]})
    assert ext.status_code == 200
    bundle = _return_param(ext.get_json())
    types = {e["resource"]["resourceType"] for e in bundle["entry"]}
    # observation-based extract -> Observation; definition-based -> Patient
    assert "Observation" in types
    assert "Patient" in types
    patient = next(e["resource"] for e in bundle["entry"]
                   if e["resource"]["resourceType"] == "Patient")
    assert patient["name"][0]["family"] == "Lovelace"


def _answer(qr, link_id):
    for item in qr["item"]:
        if item["linkId"] == link_id and item.get("answer"):
            return item["answer"][0]
    raise AssertionError(f"no answer for {link_id}")


def _response_param(params):
    for p in params["parameter"]:
        if p["name"] == "response":
            return p["resource"]
    raise AssertionError("no response param")


def _return_param(params):
    for p in params["parameter"]:
        if p["name"] == "return":
            return p["resource"]
    raise AssertionError("no return param")
