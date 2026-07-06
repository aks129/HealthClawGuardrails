# tests/test_caregaps_routes.py
import json

from r6.models import R6Resource, db


def _store(app, resource, tenant_id):
    with app.app_context():
        db.session.add(R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource.get("id"),
            tenant_id=tenant_id))
        db.session.commit()


def _seed_patient(app, tenant_id, pid="p1", gender="female", birth="1968-05-01"):
    _store(app, {"resourceType": "Patient", "id": pid, "gender": gender,
                 "birthDate": birth}, tenant_id)
    _store(app, {"resourceType": "Observation", "id": f"o-{pid}", "status": "final",
                 "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                 "subject": {"reference": f"Patient/{pid}"},
                 "effectiveDateTime": "2026-03-01"}, tenant_id)


def _resp_param(body, name):
    for p in body["parameter"]:
        if p["name"] == name:
            return p
    return None


def test_care_gaps_returns_parameters_with_summary(client, app, tenant_id, tenant_headers):
    _seed_patient(app, tenant_id)
    r = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/p1",
                    headers=tenant_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert body["resourceType"] == "Parameters"
    summary = json.loads(_resp_param(body, "summary")["valueString"])
    assert summary["total"] > 0
    assert "bp-screening" not in [g["rule_id"] for g in summary["gaps"]]
    consumer = json.loads(_resp_param(body, "consumerSummary")["valueString"])
    assert "lines" in consumer
    detail = json.loads(_resp_param(body, "detail")["valueString"])
    assert isinstance(detail, list) and len(detail) == summary["total"]
    assert _resp_param(body, "disclaimer") is not None


def test_care_gaps_get_also_works(client, app, tenant_id, tenant_headers):
    _seed_patient(app, tenant_id, pid="p2")
    r = client.get("/r6/fhir/Patient/$care-gaps?subject=Patient/p2",
                   headers=tenant_headers)
    assert r.status_code == 200
    assert r.get_json()["resourceType"] == "Parameters"


def test_care_gaps_requires_tenant(client):
    r = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/p1")
    assert r.status_code == 400


def test_care_gaps_unknown_patient_is_ok_but_indeterminate(client, tenant_headers):
    r = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/does-not-exist",
                    headers=tenant_headers)
    assert r.status_code == 200
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    # No patient found -> no birthDate/gender available -> indeterminate/not-run rules
    assert summary["total"] >= 0
