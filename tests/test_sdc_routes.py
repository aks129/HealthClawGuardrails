import json

from r6.models import R6Resource, db

INITIAL_EXPR_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-initialExpression"
)


def _store(app, resource, tenant_id):
    with app.app_context():
        r = R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource["id"],
            tenant_id=tenant_id,
        )
        db.session.add(r)
        db.session.commit()


def test_populate_by_id(client, app, tenant_id, tenant_headers):
    _store(app, {"resourceType": "Patient", "id": "p1",
                 "name": [{"given": ["Ada"]}]}, tenant_id)
    _store(app, {"resourceType": "Questionnaire", "id": "q1",
                 "status": "active",
                 "item": [{"linkId": "fn", "type": "string",
                           "extension": [{"url": INITIAL_EXPR_URL,
                                          "valueExpression": {
                                              "language": "text/fhirpath",
                                              "expression":
                                                  "%patient.name.given.first()"}}]}]},
           tenant_id)

    resp = client.post(
        "/r6/fhir/Questionnaire/q1/$populate",
        headers=tenant_headers,
        json={"resourceType": "Parameters",
              "parameter": [{"name": "subject",
                             "valueReference": {"reference": "Patient/p1"}}]},
    )

    assert resp.status_code == 200
    params = resp.get_json()
    qr = _param(params, "response")
    assert qr["item"][0]["answer"][0]["valueString"] == "Ada"


def test_extract_requires_step_up(client, tenant_headers):
    resp = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract",
        headers=tenant_headers,  # no X-Step-Up-Token
        json={"resourceType": "Parameters",
              "parameter": [{"name": "questionnaire-response",
                             "resource": {"resourceType":
                                          "QuestionnaireResponse",
                                          "status": "completed"}}]},
    )
    assert resp.status_code == 401


def test_extract_dry_run_requires_read_auth(client, monkeypatch):
    """Flag on + non-public tenant + no token: dryRun $extract is rejected.

    Regression for the read-auth bypass: a dryRun extract reflected stored
    QuestionnaireResponse/Questionnaire contents (PHI-bearing) without the
    read-auth gate a normal GET requires. $extract must enforce the same floor.
    """
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_TENANTS", "")
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed"}
    resp = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract?dryRun=true",
        headers={"X-Tenant-Id": "private-tenant"},  # no token
        json={"resourceType": "Parameters",
              "parameter": [{"name": "questionnaire-response",
                             "resource": qr}]},
    )
    assert resp.status_code == 401
    assert resp.get_json()["issue"][0]["code"] == "security"


def test_extract_dry_run_returns_bundle(client, auth_headers):
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "subject": {"reference": "Patient/p1"},
          "contained": [],
          "item": [{"linkId": "weight",
                    "answer": [{"valueQuantity": {"value": 70}}]}]}
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "weight", "type": "quantity",
                   "code": [{"system": "http://loinc.org", "code": "29463-7"}],
                   "extension": [{"url":
                                  "http://hl7.org/fhir/uv/sdc/"
                                  "StructureDefinition/"
                                  "sdc-questionnaire-observationExtract",
                                  "valueBoolean": True}]}]}
    resp = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract?dryRun=true",
        headers=auth_headers,
        json={"resourceType": "Parameters",
              "parameter": [
                  {"name": "questionnaire-response", "resource": qr},
                  {"name": "questionnaire", "resource": q}]},
    )
    assert resp.status_code == 200
    bundle = _param_resource(resp.get_json(), "return")
    assert bundle["resourceType"] == "Bundle"
    assert bundle["entry"][0]["resource"]["resourceType"] == "Observation"


def _param(params, name):
    for p in params.get("parameter", []):
        if p["name"] == name:
            return p.get("resource") or p.get("part")
    return None


def _param_resource(params, name):
    for p in params.get("parameter", []):
        if p["name"] == name:
            return p.get("resource")
    return None
