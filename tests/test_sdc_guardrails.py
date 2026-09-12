"""SDC $populate / $extract guardrail, audit, and negative-path coverage.

Exercises the real Flask routes via the `client` fixture — no mocks. Asserts
on real HTTP responses and real DB state (R6Resource / AuditEventRecord).
Complements tests/test_sdc_routes.py (happy paths) and tests/test_sdc_roundtrip.py.
"""

import json

from r6.models import R6Resource, AuditEventRecord, db

OBSERVATION_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-observationExtract"
)
DEFINITION_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-definitionExtract"
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


def _count(app, resource_type, tenant_id):
    with app.app_context():
        return R6Resource.query.filter_by(
            resource_type=resource_type, tenant_id=tenant_id).count()


def _param_resource(params, name):
    for p in params.get("parameter", []):
        if p["name"] == name:
            return p.get("resource")
    return None


# --- $populate negative paths -------------------------------------------

def test_populate_empty_body_unresolvable_is_404(client, tenant_headers):
    """No questionnaire param, no path id -> nothing to resolve -> 404 OO."""
    resp = client.post(
        "/r6/fhir/Questionnaire/$populate",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": []},
    )
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["resourceType"] == "OperationOutcome"


def test_populate_no_body_unresolvable_is_404(client, tenant_headers):
    """Completely empty/malformed body still resolves to nothing -> 404."""
    resp = client.post(
        "/r6/fhir/Questionnaire/$populate",
        headers=tenant_headers,
        data="",
        content_type="application/json",
    )
    assert resp.status_code == 404
    assert resp.get_json()["resourceType"] == "OperationOutcome"


def test_populate_unresolvable_id_is_404(client, tenant_headers):
    """A questionnaire id that is not in the store -> 404."""
    resp = client.post(
        "/r6/fhir/Questionnaire/does-not-exist/$populate",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": []},
    )
    assert resp.status_code == 404
    assert resp.get_json()["resourceType"] == "OperationOutcome"


def test_populate_cross_tenant_isolation(client, app, tenant_id,
                                         other_tenant_headers):
    """A Questionnaire stored for tenant_id is invisible to another tenant."""
    _store(app, {"resourceType": "Questionnaire", "id": "q-iso",
                 "status": "active",
                 "item": [{"linkId": "fn", "type": "string"}]}, tenant_id)
    resp = client.post(
        "/r6/fhir/Questionnaire/q-iso/$populate",
        headers=other_tenant_headers,  # DIFFERENT tenant
        json={"resourceType": "Parameters", "parameter": []},
    )
    assert resp.status_code == 404
    assert resp.get_json()["resourceType"] == "OperationOutcome"


# --- $extract negative paths --------------------------------------------

def test_extract_missing_qr_param_is_400(client, auth_headers):
    """No questionnaire-response param -> 400 OperationOutcome."""
    resp = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract",
        headers=auth_headers,
        json={"resourceType": "Parameters", "parameter": []},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["resourceType"] == "OperationOutcome"


def test_extract_invalid_step_up_token_is_401(client, tenant_id):
    """Commit-mode extract with a bogus step-up token -> 401."""
    resp = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract",
        headers={"X-Tenant-Id": tenant_id,
                 "X-Step-Up-Token": "not-a-valid-token"},
        json={"resourceType": "Parameters",
              "parameter": [{"name": "questionnaire-response",
                             "resource": {"resourceType":
                                          "QuestionnaireResponse",
                                          "status": "completed"}}]},
    )
    assert resp.status_code == 401
    assert resp.get_json()["resourceType"] == "OperationOutcome"


def test_extract_validation_failure_does_not_commit(client, app, auth_headers,
                                                    tenant_id):
    """A definitionExtract targeting an unsupported resourceType fails
    $validate -> 422, and NOTHING is committed for that type."""
    # valueCode 'Foobar' is not in R6_RESOURCE_TYPES -> structural error.
    q = {"resourceType": "Questionnaire", "status": "active",
         "extension": [{"url": DEFINITION_EXTRACT_URL, "valueCode": "Foobar"}],
         "item": [{"linkId": "x", "type": "string",
                   "definition": "http://example.org/SD#Foobar.name.family"}]}
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "item": [{"linkId": "x",
                    "answer": [{"valueString": "Doe"}]}]}

    before = _count(app, "Foobar", tenant_id)
    resp = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract",
        headers=auth_headers,
        json={"resourceType": "Parameters",
              "parameter": [
                  {"name": "questionnaire-response", "resource": qr},
                  {"name": "questionnaire", "resource": q}]},
    )
    assert resp.status_code == 422
    assert resp.get_json()["resourceType"] == "OperationOutcome"
    after = _count(app, "Foobar", tenant_id)
    assert after == before  # no commit on validation failure


def test_extract_dry_run_does_not_persist(client, app, auth_headers,
                                          tenant_id):
    """dryRun=true returns a Bundle but writes nothing to the store."""
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "subject": {"reference": "Patient/p1"},
          "item": [{"linkId": "weight",
                    "answer": [{"valueQuantity": {"value": 70}}]}]}
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "weight", "type": "quantity",
                   "code": [{"system": "http://loinc.org", "code": "29463-7"}],
                   "extension": [{"url": OBSERVATION_EXTRACT_URL,
                                  "valueBoolean": True}]}]}
    before = _count(app, "Observation", tenant_id)
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
    after = _count(app, "Observation", tenant_id)
    assert after == before  # dryRun must not persist


def test_extract_commit_refuses_the_observation(client, app, auth_headers,
                                                tenant_id):
    """Commit mode does not write the Observation: no type is cleared to
    commit on a step-up token alone (#679; the class around #668). The
    row stays previewable with dryRun=true, tested in
    tests/test_extract_refuses_clinical_rows_on_step_up_alone.py."""
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "subject": {"reference": "Patient/p1"},
          "item": [{"linkId": "weight",
                    "answer": [{"valueQuantity": {"value": 70}}]}]}
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "weight", "type": "quantity",
                   "code": [{"system": "http://loinc.org", "code": "29463-7"}],
                   "extension": [{"url": OBSERVATION_EXTRACT_URL,
                                  "valueBoolean": True}]}]}
    before = _count(app, "Observation", tenant_id)
    resp = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract",
        headers=auth_headers,
        json={"resourceType": "Parameters",
              "parameter": [
                  {"name": "questionnaire-response", "resource": qr},
                  {"name": "questionnaire", "resource": q}]},
    )
    assert resp.status_code == 422
    assert _count(app, "Observation", tenant_id) == before


# --- Audit coverage ------------------------------------------------------

def test_populate_emits_audit_event(client, app, tenant_id, tenant_headers):
    """A successful $populate writes a read AuditEvent for Questionnaire."""
    _store(app, {"resourceType": "Patient", "id": "pa",
                 "name": [{"given": ["Ada"]}]}, tenant_id)
    _store(app, {"resourceType": "Questionnaire", "id": "qa",
                 "status": "active",
                 "item": [{"linkId": "fn", "type": "string"}]}, tenant_id)
    resp = client.post(
        "/r6/fhir/Questionnaire/qa/$populate",
        headers=tenant_headers,
        json={"resourceType": "Parameters",
              "parameter": [{"name": "subject",
                             "valueReference": {"reference": "Patient/pa"}}]},
    )
    assert resp.status_code == 200
    with app.app_context():
        events = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type="Questionnaire",
            event_type="read").all()
        assert any(e.resource_id == "qa" for e in events)


def test_extract_dry_run_emits_read_audit(client, app, auth_headers,
                                          tenant_id):
    """dryRun $extract writes a read AuditEvent for QuestionnaireResponse."""
    qr = {"resourceType": "QuestionnaireResponse", "id": "qr-dry",
          "status": "completed",
          "item": [{"linkId": "weight",
                    "answer": [{"valueQuantity": {"value": 70}}]}]}
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "weight", "type": "quantity",
                   "code": [{"system": "http://loinc.org", "code": "29463-7"}],
                   "extension": [{"url": OBSERVATION_EXTRACT_URL,
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
    with app.app_context():
        events = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type="QuestionnaireResponse",
            event_type="read").all()
        assert len(events) >= 1


def test_extract_commit_emits_create_audit(client, app, auth_headers,
                                           tenant_id):
    """Commit-mode $extract writes a create AuditEvent for the QR, even
    when the bundle has nothing cleared to write (#679: today that is the
    only commit that gets past the refusal — a plain item with no extract
    extension yields an empty bundle)."""
    qr = {"resourceType": "QuestionnaireResponse", "id": "qr-commit",
          "status": "completed",
          "subject": {"reference": "Patient/p1"},
          "item": [{"linkId": "note",
                    "answer": [{"valueString": "nothing to extract"}]}]}
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "note", "type": "string"}]}
    resp = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract",
        headers=auth_headers,
        json={"resourceType": "Parameters",
              "parameter": [
                  {"name": "questionnaire-response", "resource": qr},
                  {"name": "questionnaire", "resource": q}]},
    )
    assert resp.status_code == 200
    assert resp.get_json()["parameter"][0]["resource"]["entry"] == []
    with app.app_context():
        events = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type="QuestionnaireResponse",
            event_type="create").all()
        assert len(events) >= 1
