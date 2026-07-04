# tests/test_labs_routes.py
import json


def _obs(loinc, value, unit):
    return {"resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
            "valueQuantity": {"value": value, "unit": unit}}


def _resp_param(body, name):
    for p in body["parameter"]:
        if p["name"] == name:
            return p
    return None


def test_interpret_single_observation(client, tenant_headers):
    r = client.post("/r6/fhir/Observation/$interpret",
                    headers=tenant_headers, json=_obs("2823-3", 7.0, "mmol/L"))
    assert r.status_code == 200
    body = r.get_json()
    assert body["resourceType"] == "Parameters"
    bundle = _resp_param(body, "return")["resource"]
    interp = bundle["entry"][0]["resource"]["interpretation"][0]["coding"][0]
    assert interp["code"] == "HH"
    assert _resp_param(body, "consumerSummary") is not None
    assert _resp_param(body, "disclaimer") is not None


def test_interpret_bundle(client, tenant_headers):
    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [{"resource": _obs("2823-3", 4.2, "mmol/L")},
                        {"resource": _obs("2345-7", 520, "mg/dL")}]}
    r = client.post("/r6/fhir/Observation/$interpret",
                    headers=tenant_headers, json=bundle)
    assert r.status_code == 200
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    assert summary["total"] == 2 and summary["critical"] == 1


def test_interpret_requires_tenant(client):
    r = client.post("/r6/fhir/Observation/$interpret", json=_obs("2823-3", 4.2, "mmol/L"))
    assert r.status_code == 400


def test_interpret_empty_input_is_ok(client, tenant_headers):
    r = client.post("/r6/fhir/Observation/$interpret",
                    headers=tenant_headers, json={"resourceType": "Bundle", "entry": []})
    assert r.status_code == 200
    assert json.loads(_resp_param(r.get_json(), "summary")["valueString"])["total"] == 0


def test_interpret_json_array_body_is_graceful(client, tenant_headers):
    r = client.post("/r6/fhir/Observation/$interpret",
                    headers=tenant_headers, json=[1, 2, 3])
    assert r.status_code == 200
    assert json.loads(_resp_param(r.get_json(), "summary")["valueString"])["total"] == 0


def test_interpret_bundle_with_non_dict_entry_is_graceful(client, tenant_headers):
    bundle = {"resourceType": "Bundle",
              "entry": ["oops", {"resource": _obs("2823-3", 4.2, "mmol/L")}]}
    r = client.post("/r6/fhir/Observation/$interpret", headers=tenant_headers, json=bundle)
    assert r.status_code == 200
    s = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    assert s["total"] == 1 and s["ignored"] == 1


def test_interpret_parameters_subject_string_is_graceful(client, tenant_headers):
    params = {"resourceType": "Parameters",
              "parameter": [{"name": "subject", "valueReference": "Patient/x"}]}
    r = client.post("/r6/fhir/Observation/$interpret", headers=tenant_headers, json=params)
    assert r.status_code == 200
