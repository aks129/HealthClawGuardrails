# tests/test_labs_routes.py
import json

from r6.labs.routes import STORED_OBSERVATION_CAP as _STORED_CAP


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


# --- the CareAgents call shape -------------------------------------------
#
# careagents/healthclaw.py:interpret_labs posts an EMPTY body and no ?subject:
#
#     self.http.post(f"{self.fhir}/Observation/$interpret", json={}, ...)
#
# Reported live: an agent greeted the user with "I found ... 50 lab results"
# and then answered "What do my labs say?" with "I don't see any recent blood
# work results". Both statements came from the same tenant in the same minute.
# The greeting counts a Observation search; get_labs posts this empty body,
# which matched no branch of _observations_from_request and interpreted
# nothing. The tenant held 186 Observations, 179 with a valueQuantity and 4
# carrying LOINC 2093-3 (total cholesterol) — a code the interpreter knows.

def _stored_obs(app, tenant_id, rid, loinc, value, unit):
    from r6.models import R6Resource, db
    with app.app_context():
        db.session.add(R6Resource(
            resource_type="Observation",
            resource_json=json.dumps({
                "resourceType": "Observation", "id": rid, "status": "final",
                "code": {"coding": [{"system": "http://loinc.org",
                                     "code": loinc}]},
                "subject": {"reference": "Patient/p1"},
                "valueQuantity": {"value": value, "unit": unit}}),
            resource_id=rid, tenant_id=tenant_id))
        db.session.commit()


def test_empty_body_interprets_the_tenants_stored_observations(
        app, client, tenant_headers, tenant_id):
    """MUTATION: drop the no-input fallback -> total 0 -> red.

    This is the exact call CareAgents makes. Before the fix it returned
    total=0 with records sitting in the tenant, and the agent told the
    patient their cholesterol results were not there.
    """
    _stored_obs(app, tenant_id, "chol-1", "2093-3", 244, "mg/dL")
    _stored_obs(app, tenant_id, "k-1", "2823-3", 4.2, "mmol/L")

    r = client.post("/r6/fhir/Observation/$interpret",
                    headers=tenant_headers, json={})

    assert r.status_code == 200
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    assert summary["total"] == 2, (
        "an empty $interpret body interpreted nothing while the tenant held "
        "stored Observations — this is the live get_labs bug")
    assert "2093-3" not in json.dumps(summary), "summary must not echo raw codes"


def test_no_body_at_all_interprets_the_tenants_stored_observations(
        app, client, tenant_headers, tenant_id):
    """A POST with no JSON body at all must behave like an empty one."""
    _stored_obs(app, tenant_id, "chol-2", "2093-3", 244, "mg/dL")
    r = client.post("/r6/fhir/Observation/$interpret", headers=tenant_headers)
    assert r.status_code == 200
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    assert summary["total"] == 1


def test_the_stored_fallback_is_bounded(app, client, tenant_headers, tenant_id):
    """MUTATION: remove the cap -> red.

    The fallback reads every Observation the tenant owns. Unbounded, one chat
    message would load a full import into memory; the live tenant already
    holds 186 and an Epic tenant holds far more.
    """
    for i in range(_STORED_CAP + 5):
        _stored_obs(app, tenant_id, f"cap-{i}", "2823-3", 4.2, "mmol/L")
    r = client.post("/r6/fhir/Observation/$interpret",
                    headers=tenant_headers, json={})
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    assert summary["total"] == _STORED_CAP


def test_an_explicit_body_still_wins_over_the_stored_fallback(
        app, client, tenant_headers, tenant_id):
    """The fallback must not silently widen an explicit request."""
    _stored_obs(app, tenant_id, "stored-1", "2823-3", 4.2, "mmol/L")
    r = client.post("/r6/fhir/Observation/$interpret", headers=tenant_headers,
                    json={"resourceType": "Bundle", "type": "collection",
                          "entry": [{"resource": _obs("2345-7", 520, "mg/dL")}]})
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    assert summary["total"] == 1 and summary["critical"] == 1


def test_the_stored_fallback_is_tenant_scoped(
        app, client, tenant_headers, tenant_id):
    """The fallback selects by tenant; another tenant's rows must not appear."""
    _stored_obs(app, "someone-elses-tenant", "theirs-1", "2093-3", 244, "mg/dL")
    _stored_obs(app, tenant_id, "mine-1", "2823-3", 4.2, "mmol/L")
    r = client.post("/r6/fhir/Observation/$interpret",
                    headers=tenant_headers, json={})
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    assert summary["total"] == 1


def test_an_unparseable_body_does_not_widen_into_the_stored_fallback(
        app, client, tenant_headers, tenant_id):
    """MUTATION: treat get_json(silent=True) is None as "nothing sent" -> red.

    get_json answers None for BOTH an absent body and one that would not
    parse. Conflating them means a caller error (broken JSON) silently
    becomes the widest possible read. The caller is already read-authorized
    for these rows, so this is an honesty invariant, not an auth boundary —
    but the docstring promises it, so the code must hold it.
    """
    _stored_obs(app, tenant_id, "chol-3", "2093-3", 244, "mg/dL")
    r = client.post("/r6/fhir/Observation/$interpret", headers=tenant_headers,
                    data="{not json", content_type="application/json")
    assert r.status_code == 200
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    assert summary["total"] == 0, (
        "a body that failed to parse fell through to the whole record set")
    assert summary["ignored"] == 1


def test_a_non_json_content_type_with_a_body_is_ignored_not_widened(
        app, client, tenant_headers, tenant_id):
    _stored_obs(app, tenant_id, "chol-4", "2093-3", 244, "mg/dL")
    r = client.post("/r6/fhir/Observation/$interpret", headers=tenant_headers,
                    data="subject=me", content_type="text/plain")
    assert r.status_code == 200
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    assert summary["total"] == 0
    assert summary["ignored"] == 1
