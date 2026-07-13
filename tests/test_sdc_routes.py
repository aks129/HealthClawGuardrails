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


def test_populate_auto_loads_medications_allergies_conditions(
        client, app, tenant_id, tenant_headers):
    """$populate over the API (not just the pure populate_questionnaire
    function) should auto-load a patient's MedicationRequest/
    AllergyIntolerance/Condition resources from the tenant store, the same
    way it already auto-loads Observations — the forms rail isn't complete
    if a caller has to hand-assemble a content Bundle just to see a
    patient's med list.
    """
    _store(app, {"resourceType": "Patient", "id": "p1",
                 "name": [{"given": ["Ada"]}]}, tenant_id)
    _store(app, {"resourceType": "MedicationRequest", "id": "m1",
                 "status": "active", "intent": "order",
                 "subject": {"reference": "Patient/p1"},
                 "medicationCodeableConcept": {"text": "Metformin"}},
           tenant_id)
    _store(app, {"resourceType": "AllergyIntolerance", "id": "a1",
                 "patient": {"reference": "Patient/p1"},
                 "code": {"text": "Penicillin"}}, tenant_id)
    _store(app, {"resourceType": "Condition", "id": "c1",
                 "subject": {"reference": "Patient/p1"},
                 "clinicalStatus": {"coding": [{"code": "active"}]},
                 "verificationStatus": {"coding": [{"code": "confirmed"}]},
                 "code": {"text": "Type 2 diabetes"}}, tenant_id)
    _store(app, {"resourceType": "Questionnaire", "id": "healthclaw-intake",
                 "status": "active",
                 "item": [
                     {"linkId": "medications", "type": "group", "item": [
                         {"linkId": "medications.item", "type": "group",
                          "repeats": True, "item": [
                              {"linkId": "medications.item.name",
                               "type": "string",
                               "definition":
                                   "http://hl7.org/fhir/StructureDefinition/"
                                   "MedicationRequest#MedicationRequest."
                                   "medicationCodeableConcept.text"}]}]},
                     {"linkId": "allergies", "type": "group", "item": [
                         {"linkId": "allergies.no-known-allergies",
                          "type": "boolean"},
                         {"linkId": "allergies.item", "type": "group",
                          "repeats": True, "item": [
                              {"linkId": "allergies.item.allergen",
                               "type": "string",
                               "definition":
                                   "http://hl7.org/fhir/StructureDefinition/"
                                   "AllergyIntolerance#AllergyIntolerance."
                                   "code.text"}]}]},
                     {"linkId": "conditions", "type": "group", "item": [
                         {"linkId": "conditions.item", "type": "group",
                          "repeats": True, "item": [
                              {"linkId": "conditions.item.name",
                               "type": "string",
                               "definition":
                                   "http://hl7.org/fhir/StructureDefinition/"
                                   "Condition#Condition.code.text"}]}]},
                 ]},
           tenant_id)

    resp = client.post(
        "/r6/fhir/Questionnaire/healthclaw-intake/$populate",
        headers=tenant_headers,
        json={"resourceType": "Parameters",
              "parameter": [{"name": "subject",
                             "valueReference": {"reference": "Patient/p1"}}]},
    )

    assert resp.status_code == 200
    qr = _param(resp.get_json(), "response")

    def by_link_id(items, link_id):
        for item in items:
            if item["linkId"] == link_id:
                return item
            if "item" in item:
                found = by_link_id(item["item"], link_id)
                if found:
                    return found
        return None

    med_name = by_link_id(qr["item"], "medications.item.name")
    assert med_name["answer"][0]["valueString"] == "Metformin"
    allergen = by_link_id(qr["item"], "allergies.item.allergen")
    assert allergen["answer"][0]["valueString"] == "Penicillin"
    nka = by_link_id(qr["item"], "allergies.no-known-allergies")
    assert "answer" not in nka
    condition_name = by_link_id(qr["item"], "conditions.item.name")
    assert condition_name["answer"][0]["valueString"] == "Type 2 diabetes"


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
