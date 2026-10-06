"""#869: one stored Observation with a non-object `subject` or `code` made
`Observation/$interpret` return 500 for every patient in the tenant.

The write API stores what it is given, so these rows can arrive through the
product's own write path or an upstream feed. They are inserted directly
here, which is the shape the bug needs and does not depend on the write
path's validation. Every value is synthetic.
"""

import json

import pytest


def _store(app, tenant_id, obs):
    from r6.models import R6Resource, db
    with app.app_context():
        db.session.add(R6Resource(
            resource_type="Observation", resource_json=json.dumps(obs),
            resource_id=obs["id"], tenant_id=tenant_id))
        db.session.commit()


def _obs(rid, *, subject=None, code=None, value=0.9):
    return {
        "resourceType": "Observation", "id": rid, "status": "final",
        "code": code if code is not None else {
            "coding": [{"system": "http://loinc.org", "code": "2160-0"}]},
        "subject": subject if subject is not None else {
            "reference": "Patient/p1"},
        "effectiveDateTime": "2026-09-01T08:00:00+00:00",
        "valueQuantity": {"value": value, "unit": "mg/dL"},
    }


def _summary(body):
    p = next(p for p in body["parameter"] if p["name"] == "summary")
    return json.loads(p["valueString"])


#: Each one alone made the call 500 before #869.
MALFORMED = [
    pytest.param({"subject": "Patient/p10"}, id="subject-string"),
    pytest.param({"subject": ["Patient/p1"]}, id="subject-list"),
    pytest.param({"subject": {"reference": 7}}, id="subject-reference-int"),
    pytest.param({"code": "creatinine"}, id="code-string"),
    pytest.param({"code": ["2160-0"]}, id="code-list"),
    pytest.param({"code": {"coding": None}}, id="coding-null"),
    pytest.param({"code": {"coding": [None, "x"]}}, id="coding-holds-junk"),
    # #878 QA F1: the range falls back to the table, and annotating the
    # result inserted the table range into a referenceRange that was not a
    # list (r6/labs/report.py annotate_observation).
    pytest.param({"referenceRange": "0.6-1.3"}, id="referenceRange-string"),
    pytest.param({"referenceRange": {"low": {"value": 0.6}}},
                 id="referenceRange-object"),
]


@pytest.mark.parametrize("bad", MALFORMED)
@pytest.mark.parametrize("path", [
    "/r6/fhir/Observation/$interpret?subject=Patient/p1",
    "/r6/fhir/Observation/$interpret",
])
def test_one_malformed_row_does_not_break_the_call(
        app, client, tenant_headers, tenant_id, bad, path):
    _store(app, tenant_id, _obs("good-1"))
    _store(app, tenant_id, {**_obs("bad-1"), **bad})
    r = client.post(path, headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    # The good row is still interpreted.
    summary = _summary(r.get_json())
    assert summary["total"] >= 1


@pytest.mark.parametrize("subject", ["Patient/p10", ["Patient/p1"],
                                     {"reference": 7}, None])
def test_a_row_whose_subject_cannot_be_read_is_counted_as_ignored(
        app, client, tenant_headers, tenant_id, subject):
    """On the ?subject branch a row whose subject is not a readable reference
    cannot be shown to belong to this patient, so it is skipped and counted,
    not interpreted and not silently dropped."""
    _store(app, tenant_id, _obs("good-1"))
    bad = _obs("bad-1")
    if subject is None:
        del bad["subject"]
    else:
        bad["subject"] = subject
    _store(app, tenant_id, bad)
    r = client.post("/r6/fhir/Observation/$interpret?subject=Patient/p1",
                    headers=tenant_headers)
    assert r.status_code == 200
    summary = _summary(r.get_json())
    assert summary["total"] == 1
    # A missing subject is simply someone else's (or no one's) row; an
    # unreadable one is malformed and counted.
    assert summary["ignored"] == (0 if subject is None else 1)


def test_interpret_observation_reads_a_malformed_code_as_unknown():
    from r6.labs.interpret import UNKNOWN_ANALYTE, interpret_observation
    for code in ("creatinine", ["2160-0"], {"coding": None},
                 {"coding": [None, "x", {"system": "http://loinc.org",
                                         "code": ["2160-0"]}]}):
        res = interpret_observation(_obs("x", code=code))
        assert res["flag"] is None
        assert res["indeterminate_reason"] == UNKNOWN_ANALYTE


@pytest.mark.parametrize("field,value", [
    ("valueQuantity", "0.9 mg/dL"),
    ("referenceRange", "0.6-1.3"),
    ("referenceRange", [None, "x", {"low": "0.6", "high": [1.3]}]),
])
def test_interpret_observation_survives_other_non_object_fields(field, value):
    from r6.labs.interpret import interpret_observation
    obs = _obs("x")
    obs[field] = value
    res = interpret_observation(obs)
    assert "flag" in res


@pytest.mark.parametrize("stored", ["0.6-1.3", {"low": {"value": 0.6}}, None])
def test_annotating_replaces_a_reference_range_that_is_not_a_list(stored):
    """The stored value is upstream junk: it is replaced by the table range
    we used, never kept beside it and never a 500."""
    from r6.labs.interpret import interpret_observation
    from r6.labs.report import annotate_observation
    obs = _obs("x", value=0.9)
    obs["valueQuantity"]["code"] = "mg/dL"
    obs["referenceRange"] = stored
    res = interpret_observation(obs)
    assert res["range_source"] == "table"
    out = annotate_observation(obs, res)
    assert isinstance(out["referenceRange"], list)
    assert len(out["referenceRange"]) == 1
    assert "population default" in out["referenceRange"][0]["text"]
