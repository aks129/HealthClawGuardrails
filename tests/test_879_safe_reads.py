"""#879 / #869: one stored resource with a malformed field never makes an
engine route return 500, and no engine response carries a bare NaN or
Infinity token.

The write API stores what it is given, so `subject`, `code`, `coding`,
`component` or a value can hold a string, a list, a number or null where
FHIR says object. Each route below reads such rows from the tenant's store.
Before this change one bad row turned the whole call into a 500 for every
patient in the tenant. Expected now: the bad row is skipped, the rest is
read, and the answer is 200 (or an explicit 4xx).

Rows are inserted straight into the store, the way an upstream feed or an
old write lands them. `json.dumps` writes a non-finite float as a bare
`NaN` / `Infinity` token, which is exactly the stored state #869 found.

Synthetic data only.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_labs_subject_matching import PID, _row

SUBJ = f"Patient/{PID}"
LOINC = "http://loinc.org"
_NONFINITE = ("NaN", "Infinity", "-Infinity")


def _when(days_ago=1, hour=8):
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def _strict(text):
    """json.loads that refuses NaN/Infinity, as a browser's JSON.parse does."""
    def refuse(token):
        raise AssertionError(f"bare {token} token in response")
    return json.loads(text, parse_constant=refuse)


def _strict_body(resp):
    """Parse the body strictly, then every valueString that is JSON."""
    body = _strict(resp.get_data(as_text=True))

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "valueString" and isinstance(v, str) \
                        and v[:1] in ("{", "["):
                    _strict(v)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(body)
    return body


def _patient(app, tenant_id, **extra):
    _row(app, tenant_id, {"resourceType": "Patient", "id": PID,
                          "gender": "female", "birthDate": "1960-05-01",
                          **extra})


def _bp(rid, systolic=128, diastolic=78, *, days_ago=1, **override):
    obs = {"resourceType": "Observation", "id": rid, "status": "final",
           "subject": {"reference": SUBJ},
           "effectiveDateTime": _when(days_ago),
           "code": {"coding": [{"system": LOINC, "code": "85354-9"}]},
           "component": [
               {"code": {"coding": [{"system": LOINC, "code": "8480-6"}]},
                "valueQuantity": {"value": systolic, "unit": "mm[Hg]"}},
               {"code": {"coding": [{"system": LOINC, "code": "8462-4"}]},
                "valueQuantity": {"value": diastolic, "unit": "mm[Hg]"}}]}
    obs.update(override)
    return obs


def _lab(rid, value=0.9, **override):
    obs = {"resourceType": "Observation", "id": rid, "status": "final",
           "subject": {"reference": SUBJ}, "effectiveDateTime": _when(),
           "code": {"coding": [{"system": LOINC, "code": "2160-0"}]},
           "valueQuantity": {"value": value, "unit": "mg/dL"}}
    obs.update(override)
    return obs


#: Observation shapes the write API accepts and FHIR does not.
BAD_OBSERVATIONS = {
    "subject-string": {"subject": SUBJ},
    "subject-list": {"subject": [SUBJ]},
    "subject-null": {"subject": None},
    "reference-number": {"subject": {"reference": 7}},
    "reference-dict": {"subject": {"reference": {"x": 1}}},
    "code-string": {"code": "blood pressure"},
    "code-list": {"code": [{"coding": []}]},
    "coding-null": {"code": {"coding": None}},
    "coding-string": {"code": {"coding": "85354-9"}},
    "coding-holds-null": {"code": {"coding": [None]}},
    "coding-code-number": {"code": {"coding": [{"system": 5, "code": 5}]}},
    "coding-code-list": {"code": {"coding": [{"system": LOINC,
                                              "code": ["85354-9"]}]}},
    "component-string": {"component": "120/80"},
    "component-holds-junk": {"component": [None, "x", 5]},
    "component-code-string": {"component": [
        {"code": "8480-6", "valueQuantity": {"value": 120}},
        {"code": {"coding": []}, "valueQuantity": "80"}]},
    "component-value-string": {"component": [
        {"code": {"coding": [{"code": "8480-6"}]},
         "valueQuantity": {"value": "120"}},
        {"code": {"coding": [{"code": "8462-4"}]},
         "valueQuantity": {"value": "80"}}]},
    "component-value-nan": {"component": [
        {"code": {"coding": [{"code": "8480-6"}]},
         "valueQuantity": {"value": float("nan")}},
        {"code": {"coding": [{"code": "8462-4"}]},
         "valueQuantity": {"value": float("inf")}}]},
    "component-value-huge-int": {"component": [
        {"code": {"coding": [{"code": "8480-6"}]},
         "valueQuantity": {"value": 10**400}},
        {"code": {"coding": [{"code": "8462-4"}]},
         "valueQuantity": {"value": -10**400}}]},
    "value-huge-int": {"valueQuantity": {"value": 10**400}},
    "effective-number": {"effectiveDateTime": 20260101},
    "effective-list": {"effectiveDateTime": ["2026-01-01"]},
    "value-quantity-string": {"valueQuantity": "0.9 mg/dL"},
    "value-infinity": {"valueQuantity": {"value": float("inf")}},
    "value-nan": {"valueQuantity": {"value": float("nan")}},
}

#: Condition shapes, for the two routes that read Conditions.
BAD_CONDITIONS = {
    "subject-string": {"subject": SUBJ},
    "code-string": {"code": "hypertension"},
    "coding-null": {"code": {"coding": None}},
    "coding-holds-null": {"code": {"coding": [None]}},
    "coding-code-number": {"code": {"coding": [{"system": 5, "code": 5}]}},
    "coding-code-list": {"code": {"coding": [{"code": ["E11.9"]}]}},
    "clinical-status-string": {"clinicalStatus": "active"},
    "clinical-status-holds-null": {"clinicalStatus": {"coding": [None]}},
}


def _condition(rid, **override):
    cond = {"resourceType": "Condition", "id": rid,
            "subject": {"reference": SUBJ},
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm",
                                 "code": "I10"}]}}
    cond.update(override)
    return cond


def _seed(app, tenant_id, bad, *, bad_condition=None):
    """One good BP reading, one good lab, one good Condition, plus `bad`
    applied to a BP reading and a lab (and `bad_condition` to a Condition)."""
    _patient(app, tenant_id)
    _row(app, tenant_id, _bp("bp-good"))
    _row(app, tenant_id, _lab("lab-good"))
    _row(app, tenant_id, _condition("cond-good"))
    _row(app, tenant_id, _bp("bp-bad", days_ago=2, **bad))
    _row(app, tenant_id, _lab("lab-bad", **bad))
    if bad_condition is not None:
        _row(app, tenant_id, _condition("cond-bad", **bad_condition))


OBS_CASES = [pytest.param(v, id=k) for k, v in BAD_OBSERVATIONS.items()]
COND_CASES = [pytest.param(v, id=k) for k, v in BAD_CONDITIONS.items()]


# --- $care-gaps (r6/caregaps/evaluate.py) -------------------------------------

@pytest.mark.parametrize("bad", OBS_CASES)
def test_care_gaps_survives_a_malformed_observation(
        app, client, tenant_id, tenant_headers, bad):
    _seed(app, tenant_id, bad)
    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    _strict_body(r)


@pytest.mark.parametrize("bad", COND_CASES)
def test_care_gaps_survives_a_malformed_condition(
        app, client, tenant_id, tenant_headers, bad):
    _seed(app, tenant_id, {}, bad_condition=bad)
    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    _strict_body(r)


def test_care_gaps_still_reads_the_good_rows_beside_a_bad_one(
        app, client, tenant_id, tenant_headers):
    """The bad row is skipped, not the record: a diabetes Condition next to a
    string-coded one still makes the A1c rule applicable."""
    _patient(app, tenant_id)
    _row(app, tenant_id, _condition(
        "dm", code={"coding": [{"system": "http://snomed.info/sct",
                                "code": "44054006"}]}))
    _row(app, tenant_id, _condition("bad", code="diabetes"))
    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers)
    assert r.status_code == 200
    params = {p["name"]: p for p in _strict_body(r)["parameter"]}
    detail = json.loads(params["detail"]["valueString"])
    a1c = [d for d in detail if "a1c" in json.dumps(d).lower()]
    assert a1c and all(d.get("status") != "not_applicable" for d in a1c), a1c


# --- $evaluate-measure (r6/quality/routes.py, measures.py) -------------------

_MEASURE = "/r6/fhir/Measure/nqf0018-controlling-high-bp/$evaluate-measure"


@pytest.mark.parametrize("subject", [None, SUBJ], ids=["summary", "individual"])
@pytest.mark.parametrize("bad", OBS_CASES)
def test_evaluate_measure_survives_a_malformed_observation(
        app, client, tenant_id, tenant_headers, bad, subject):
    _seed(app, tenant_id, bad)
    url = _MEASURE + (f"?subject={subject}" if subject else "")
    r = client.post(url, headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    _strict_body(r)


@pytest.mark.parametrize("subject", [None, SUBJ], ids=["summary", "individual"])
@pytest.mark.parametrize("bad", COND_CASES)
def test_evaluate_measure_survives_a_malformed_condition(
        app, client, tenant_id, tenant_headers, bad, subject):
    _seed(app, tenant_id, {}, bad_condition=bad)
    url = _MEASURE + (f"?subject={subject}" if subject else "")
    r = client.post(url, headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    _strict_body(r)


def test_evaluate_measure_still_scores_the_good_reading(
        app, client, tenant_id, tenant_headers):
    """A malformed BP row beside a good one: the good one still decides.
    128/78 is controlled, so the patient is in the numerator (score 1.0)."""
    _seed(app, tenant_id, {"component": "120/80"})
    r = client.post(_MEASURE + f"?subject={SUBJ}", headers=tenant_headers)
    assert r.status_code == 200
    assert r.get_json()["group"][0]["measureScore"]["value"] == 1.0


# --- SMBP: trend page, clinician report, reminders ---------------------------

def _session(app, tenant_id):
    from models import db
    from r6.smbp.models import SMBPSession
    with app.app_context():
        s = SMBPSession(tenant_id=tenant_id, patient_ref=SUBJ, days=14)
        db.session.add(s)
        db.session.commit()
        return s.id


@pytest.mark.parametrize("bad", OBS_CASES)
def test_smbp_trend_survives_a_malformed_observation(
        app, client, tenant_id, tenant_headers, bad):
    _seed(app, tenant_id, bad)
    r = client.get(f"/r6/smbp/trend?subject={SUBJ}", headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert "<b>1</b> readings" in r.get_data(as_text=True) \
        or "<b>2</b> readings" in r.get_data(as_text=True)


@pytest.mark.parametrize("bad", OBS_CASES)
def test_smbp_report_survives_a_malformed_observation(
        app, client, tenant_id, tenant_headers, bad):
    _seed(app, tenant_id, bad)
    sid = _session(app, tenant_id)
    r = client.get(f"/r6/smbp/report/{sid}", headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_data(as_text=True)
    assert "128/78" in body
    assert "nan" not in body.lower().replace("nanos", "") \
        and "inf/" not in body.lower()


@pytest.mark.parametrize("bad", OBS_CASES)
def test_smbp_reminders_survive_a_malformed_observation(
        app, client, tenant_id, tenant_headers, bad):
    _seed(app, tenant_id, bad)
    _session(app, tenant_id)
    r = client.get("/r6/smbp/reminders/due", headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    _strict_body(r)


# --- $ingest-context (r6/context_builder.py) ---------------------------------

@pytest.mark.parametrize("entry", [
    pytest.param({"resource": {"resourceType": "Observation", "id": "o1",
                               "status": "final", "code": {"text": "x"},
                               "subject": {"reference": 7}}},
                 id="reference-number"),
    pytest.param({"resource": {"resourceType": "Observation", "id": "o1",
                               "status": "final", "code": {"text": "x"},
                               "subject": {"reference": None}}},
                 id="reference-null"),
    pytest.param({"resource": {"resourceType": "Observation", "id": "o1",
                               "status": "final", "code": {"text": "x"},
                               "subject": {"reference": ["Patient/p"]}}},
                 id="reference-list"),
    pytest.param({"resource": "Observation/o1"}, id="resource-string"),
    pytest.param("not-an-entry", id="entry-string"),
    pytest.param(None, id="entry-null"),
])
def test_ingest_context_survives_a_malformed_entry(
        client, auth_headers, entry):
    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [entry]}
    r = client.post("/r6/fhir/Bundle/$ingest-context",
                    data=json.dumps(bundle), content_type="application/json",
                    headers=auth_headers)
    assert r.status_code < 500, r.get_data(as_text=True)[:300]
    _strict_body(r)


# --- NaN / Infinity never leave as bare tokens --------------------------------

def _nonfinite_tenant(app, tenant_id):
    _patient(app, tenant_id)
    for i, v in enumerate((float("inf"), float("-inf"), float("nan"))):
        _row(app, tenant_id, _lab(f"lab-{i}", value=v))
        _row(app, tenant_id, _bp(f"bp-{i}", systolic=v, diastolic=v,
                                 days_ago=i + 1))
    _row(app, tenant_id, _lab("lab-ok", value=0.9))


@pytest.mark.parametrize("method,url", [
    ("post", "/r6/fhir/Observation/$interpret"),
    ("post", f"/r6/fhir/Observation/$interpret?subject={SUBJ}"),
    ("post", "/r6/fhir/Patient/$care-gaps"),
    ("post", _MEASURE),
    ("post", _MEASURE + f"?subject={SUBJ}"),
    ("get", "/r6/smbp/reminders/due"),
    ("get", "/r6/fhir/Observation/lab-0"),
    ("get", "/r6/fhir/Observation/bp-2"),
    ("get", "/r6/fhir/Observation"),
    ("get", f"/r6/fhir/Observation?subject={SUBJ}"),
])
def test_no_engine_response_carries_a_bare_nan_or_infinity(
        app, client, tenant_id, tenant_headers, method, url):
    _nonfinite_tenant(app, tenant_id)
    r = getattr(client, method)(url, headers=tenant_headers)
    assert r.status_code < 500, r.get_data(as_text=True)[:300]
    text = r.get_data(as_text=True)
    for token in _NONFINITE:
        assert f": {token}" not in text and f":{token}" not in text, token
    _strict_body(r)


def test_the_json_provider_is_strict_and_never_raises_on_a_non_finite(app):
    """A non-finite float anywhere in a jsonify payload is dropped: the key
    from an object, the element from an array. A null property is invalid
    FHIR JSON, so null is not the answer either. The provider serializes
    with allow_nan=False."""
    import json as _json
    from flask import jsonify
    from r6.safe_read import StrictJSONProvider
    with app.test_request_context():
        resp = jsonify({"a": float("nan"), "b": [1.5, float("inf")],
                        "c": {"d": float("-inf"), "e": "NaN"}})
    assert _strict(resp.get_data(as_text=True)) == {
        "b": [1.5], "c": {"e": "NaN"}}
    assert isinstance(app.json, StrictJSONProvider)
    seen = {}
    real = _json.dumps

    def spy(obj, **kwargs):
        seen.update(kwargs)
        return real(obj, **kwargs)
    _json.dumps, saved = spy, _json.dumps
    try:
        app.json.dumps({"x": 1})
    finally:
        _json.dumps = saved
    assert seen.get("allow_nan") is False


def test_finite_numbers_and_fhir_output_are_unchanged(
        app, client, tenant_id, tenant_headers):
    _patient(app, tenant_id)
    _row(app, tenant_id, _lab("lab-ok", value=0.9))
    r = client.get("/r6/fhir/Observation/lab-ok", headers=tenant_headers)
    assert r.status_code == 200
    body = _strict_body(r)
    assert body["valueQuantity"]["value"] == 0.9
    assert body["resourceType"] == "Observation"


def test_a_stored_non_finite_value_is_dropped_from_the_echo_not_nulled(
        app, client, tenant_id, tenant_headers):
    """The echoed Observation keeps its valueQuantity, minus the value: no
    bare token, and no null property either."""
    _patient(app, tenant_id)
    _row(app, tenant_id, _lab("lab-inf", value=float("inf")))
    r = client.get("/r6/fhir/Observation/lab-inf", headers=tenant_headers)
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    assert "null" not in text
    body = _strict_body(r)
    assert "value" not in body["valueQuantity"]
    assert body["valueQuantity"]["unit"] == "mg/dL"


def test_finite_drops_at_every_depth():
    from r6.safe_read import finite, strict_dumps
    assert finite({"a": [float("nan"), {"b": float("inf"), "c": 1}],
                   "d": (2.0, float("-inf"))}) == {"a": [{"c": 1}], "d": [2.0]}
    assert strict_dumps({"x": float("nan"), "y": [float("inf")]}) == \
        '{"y": []}'
