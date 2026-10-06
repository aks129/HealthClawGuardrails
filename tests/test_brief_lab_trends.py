"""#867: the visit brief shows the creatinine trend in its own section.

The brief reads Observations straight from the store (r6/brief/routes.py),
not through $interpret, so the trend is computed there: for the one Patient
the care-gaps section already resolves, from that Patient's results only,
by the same engine `Observation/$interpret?subject=` uses. With no Patient
or more than one there is no trend section at all — absent, never a
sentence, because "no rise found" would be a claim about kidneys.

The label is the engine's name for the code, never the record's own
display or text. Synthetic data only.
"""

import json

from r6.seed import seed_demo_data
from tests.test_brief_routes import _fields, _section, _store, _URL

PROMPTLY = "Contact your clinician promptly."
RISE = "creatinine rose from 0.8 to 1.3 mg/dL in 6 days"


def _trend_fields(client, headers):
    r = client.get(_URL, headers=headers)
    assert r.status_code == 200
    section = _section(r.get_json(), "lab-trends")
    return r, [json.loads(f["valueString"])
               for f in (_fields(section) if section else [])]


def test_the_sample_patients_creatinine_rise_is_in_the_brief(
        app, client, tenant_id, tenant_headers):
    """MUTATION: drop the lab-trends section from r6/brief/routes.py -> red."""
    with app.app_context():
        seed_demo_data(tenant_id=tenant_id)
    r, fields = _trend_fields(client, tenant_headers)
    [field] = fields
    assert field["label"] == "Creatinine"
    assert RISE in field["value"] and PROMPTLY in field["value"]
    # Cites the latest result the sentence is about.
    assert field["sourceType"] == "Observation"
    assert field["sourceId"] == "demo-obs-creat-5"
    # Not mixed into the range-flag lab list.
    labs = [json.loads(f["valueString"])["value"]
            for f in _fields(_section(r.get_json(), "labs"))]
    assert not any(PROMPTLY in v for v in labs)


def _cr(rid, value, when, patient="p1", **extra):
    return {"resourceType": "Observation", "id": rid, "status": "final",
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": "2160-0", **extra.pop("coding", {})}],
                     **extra.pop("code", {})},
            "subject": {"reference": f"Patient/{patient}"},
            "effectiveDateTime": when,
            "valueQuantity": {"value": value, "unit": "mg/dL"}}


def _recent(days_ago):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_two_patients_get_no_trend_section_even_with_a_rise(
        app, client, tenant_id, tenant_headers):
    """Two people's results must not be read as one person's rise."""
    for pid in ("p1", "p2"):
        _store(app, {"resourceType": "Patient", "id": pid}, tenant_id)
    _store(app, _cr("a", 0.8, _recent(6), patient="p1"), tenant_id)
    _store(app, _cr("b", 1.3, _recent(0), patient="p2"), tenant_id)
    _, fields = _trend_fields(client, tenant_headers)
    assert fields == []


def test_no_patient_gets_no_trend_section(app, client, tenant_id,
                                          tenant_headers):
    _store(app, _cr("a", 0.8, _recent(6)), tenant_id)
    _store(app, _cr("b", 1.3, _recent(0)), tenant_id)
    _, fields = _trend_fields(client, tenant_headers)
    assert fields == []


def test_only_the_patients_own_results_are_compared(
        app, client, tenant_id, tenant_headers):
    """One Patient, but the low baseline belongs to nobody on file: it must
    not be the patient's baseline."""
    _store(app, {"resourceType": "Patient", "id": "p1"}, tenant_id)
    _store(app, _cr("a", 0.8, _recent(6), patient="someone-else"), tenant_id)
    _store(app, _cr("b", 1.3, _recent(0)), tenant_id)
    _, fields = _trend_fields(client, tenant_headers)
    assert fields == []


def test_the_label_never_comes_from_the_record(
        app, client, tenant_id, tenant_headers):
    _store(app, {"resourceType": "Patient", "id": "p1"}, tenant_id)
    leak = {"coding": {"display": "Jane Q Synthetic"},
            "code": {"text": "Jane Q Synthetic"}}
    _store(app, _cr("a", 0.8, _recent(6), **json.loads(json.dumps(leak))),
           tenant_id)
    _store(app, _cr("b", 1.3, _recent(0), **leak), tenant_id)
    r, [field] = _trend_fields(client, tenant_headers)
    assert field["label"] == "Creatinine"
    assert "Jane Q Synthetic" not in r.get_data(as_text=True)


def test_a_malformed_row_does_not_break_the_brief(
        app, client, tenant_id, tenant_headers):
    _store(app, {"resourceType": "Patient", "id": "p1"}, tenant_id)
    _store(app, _cr("a", 0.8, _recent(6)), tenant_id)
    _store(app, _cr("b", 1.3, _recent(0)), tenant_id)
    bad = _cr("c", 1.0, _recent(1))
    bad["subject"] = "Patient/p1"
    _store(app, bad, tenant_id)
    _, [field] = _trend_fields(client, tenant_headers)
    assert PROMPTLY in field["value"]
