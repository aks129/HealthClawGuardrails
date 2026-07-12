import json
import datetime as dt

import r6.quality.routes as quality_routes
from r6.models import R6Resource, db
from r6.quality.routes import _default_measurement_period


def _store(app, resource, tenant_id):
    with app.app_context():
        db.session.add(R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource.get("id"),
            tenant_id=tenant_id))
        db.session.commit()


def _seed_controlled_patient(app, tenant_id, pid="p1", sys_v=132, dia_v=82,
                             observed_on="2026-09-01"):
    _store(app, {"resourceType": "Patient", "id": pid, "birthDate": "1970-06-01"},
           tenant_id)
    _store(app, {"resourceType": "Condition", "id": f"c-{pid}",
                 "clinicalStatus": {"coding": [{"code": "active"}]},
                 "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm",
                                      "code": "I10"}]},
                 "subject": {"reference": f"Patient/{pid}"}}, tenant_id)
    _store(app, {"resourceType": "Observation", "id": f"o-{pid}", "status": "final",
                 "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
                 "subject": {"reference": f"Patient/{pid}"},
                 "effectiveDateTime": observed_on,
                 "component": [
                     {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                      "valueQuantity": {"value": sys_v}},
                     {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
                      "valueQuantity": {"value": dia_v}}]}, tenant_id)


def _pop(report, code):
    for g in report["group"]:
        for p in g["population"]:
            if p["code"]["coding"][0]["code"] == code:
                return p["count"]
    return None


def test_default_measurement_period_uses_current_calendar_year():
    assert _default_measurement_period(dt.date(2027, 6, 15)) == (
        "2027-01-01", "2027-12-31")


def test_measure_resource_endpoint(client, tenant_headers):
    resp = client.get("/r6/fhir/Measure/nqf0018-controlling-high-bp",
                      headers=tenant_headers)
    assert resp.status_code == 200
    assert resp.get_json()["resourceType"] == "Measure"


def test_individual_evaluate_controlled(client, app, tenant_id, tenant_headers):
    _seed_controlled_patient(app, tenant_id)
    resp = client.post(
        "/r6/fhir/Measure/nqf0018-controlling-high-bp/$evaluate-measure",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "periodStart", "valueDate": "2026-01-01"},
            {"name": "periodEnd", "valueDate": "2026-12-31"},
            {"name": "subject", "valueReference": {"reference": "Patient/p1"}}]})
    assert resp.status_code == 200
    rep = resp.get_json()
    assert rep["resourceType"] == "MeasureReport"
    assert rep["type"] == "individual"
    assert _pop(rep, "denominator") == 1
    assert _pop(rep, "numerator") == 1


def test_individual_evaluate_uncontrolled(client, app, tenant_id, tenant_headers):
    _seed_controlled_patient(app, tenant_id, sys_v=150, dia_v=95)
    resp = client.get(
        "/r6/fhir/Measure/nqf0018-controlling-high-bp/$evaluate-measure"
        "?subject=Patient/p1&periodStart=2026-01-01&periodEnd=2026-12-31",
        headers=tenant_headers)
    assert resp.status_code == 200
    assert _pop(resp.get_json(), "numerator") == 0


def test_population_evaluate_rate(client, app, tenant_id, tenant_headers):
    _seed_controlled_patient(app, tenant_id, pid="ctl", sys_v=128, dia_v=78)
    _seed_controlled_patient(app, tenant_id, pid="unc", sys_v=150, dia_v=96)
    resp = client.post(
        "/r6/fhir/Measure/nqf0018-controlling-high-bp/$evaluate-measure",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "periodStart", "valueDate": "2026-01-01"},
            {"name": "periodEnd", "valueDate": "2026-12-31"}]})
    assert resp.status_code == 200
    rep = resp.get_json()
    assert rep["type"] == "summary"
    assert _pop(rep, "denominator") == 2
    assert _pop(rep, "numerator") == 1
    assert rep["group"][0]["measureScore"]["value"] == 0.5


def test_population_evaluate_defaults_to_current_year(client, app, tenant_id,
                                                      tenant_headers,
                                                      monkeypatch):
    monkeypatch.setattr(quality_routes, "_default_measurement_period",
                        lambda: ("2027-01-01", "2027-12-31"))
    _seed_controlled_patient(app, tenant_id, observed_on="2027-09-01")
    resp = client.post(
        "/r6/fhir/Measure/nqf0018-controlling-high-bp/$evaluate-measure",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": []})
    assert resp.status_code == 200
    rep = resp.get_json()
    assert rep["period"] == {"start": "2027-01-01", "end": "2027-12-31"}
    assert _pop(rep, "denominator") == 1
    assert _pop(rep, "numerator") == 1


def test_evaluate_requires_read_auth_nonpublic(client, app, monkeypatch):
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_TENANTS", "")
    resp = client.get(
        "/r6/fhir/Measure/nqf0018-controlling-high-bp/$evaluate-measure"
        "?subject=Patient/p1",
        headers={"X-Tenant-Id": "private-q"})
    assert resp.status_code == 401


def test_evaluate_post_requires_read_auth_nonpublic(client, app, monkeypatch):
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_TENANTS", "")
    resp = client.post(
        "/r6/fhir/Measure/nqf0018-controlling-high-bp/$evaluate-measure",
        headers={"X-Tenant-Id": "private-q"},
        json={"resourceType": "Parameters", "parameter": []})
    assert resp.status_code == 401
