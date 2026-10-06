"""#885: a `Coding.system` that is not a string (a list, an object, a
number) made `apply_redaction` raise in `canonical_system`. Every read that
redacts the row then returned 500 for the whole tenant: Observation search,
read and $interpret. The write committed the row and then returned 500.

Expected now:
  - redaction reads a non-string system as absent and drops it from the
    output, so whatever was packed into it never reaches a reader;
  - the write is refused with 422 before anything is stored, by the
    validator that already runs before commit, with or without an external
    validator configured;
  - a row that is already stored is read, redacted and returned.

Synthetic data only (canary strings).
"""

import json

import pytest

from tests.test_879_safe_reads import _strict_body
from tests.test_labs_subject_matching import PID, _patient, _row

LOINC = "http://loinc.org"
CANARY = "CANARY-JANE-DOE-885"

#: System values the write API used to accept and redaction choked on.
BAD_SYSTEMS = [
    pytest.param([LOINC], id="list"),
    pytest.param({"url": LOINC, "display": CANARY}, id="object"),
    pytest.param({"name": CANARY}, id="object-name"),
    pytest.param([CANARY], id="list-canary"),
    pytest.param(5, id="int"),
    pytest.param(1.5, id="float"),
    pytest.param(True, id="bool"),
]


def _obs(rid, system, where="code"):
    obs = {"resourceType": "Observation", "id": rid, "status": "final",
           "subject": {"reference": f"Patient/{PID}"},
           "effectiveDateTime": "2026-09-01T08:00:00Z",
           "code": {"coding": [{"system": LOINC, "code": "2160-0"}]},
           "valueQuantity": {"value": 0.9, "unit": "mg/dL"}}
    bad = {"coding": [{"system": system, "code": "2160-0",
                       "display": CANARY}], "text": CANARY}
    if where == "code":
        obs["code"] = bad
    elif where == "category":
        obs["category"] = [bad]
    else:
        obs.pop("valueQuantity")
        obs["valueCodeableConcept"] = bad
    return obs


WHERE = ["code", "category", "valueCodeableConcept"]


# --- the engine: canonical_system, lookup, label_codings, apply_redaction -----

@pytest.mark.parametrize("system", BAD_SYSTEMS)
def test_canonical_system_reads_a_non_string_as_absent(system):
    from r6.terminology import canonical_system, lookup
    assert canonical_system(system) == ""
    # A label is keyed by a real system; a wrong-shaped one finds none.
    assert lookup(system, "2160-0") is None


@pytest.mark.parametrize("code", [["2160-0"], {"code": "2160-0"}, True, None])
def test_lookup_reads_a_non_string_code_as_absent(code):
    from r6.terminology import lookup, reset_unlabelled, unlabelled_codes
    reset_unlabelled()
    assert lookup(LOINC, code) is None
    # Nothing stringified from a list or object is recorded as a miss.
    assert unlabelled_codes() == []


def test_an_int_code_still_looks_up_as_its_string():
    from r6.terminology import CVX, lookup
    assert lookup(CVX, 3) == lookup(CVX, "3")


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("system", BAD_SYSTEMS)
def test_apply_redaction_survives_and_drops_a_non_string_system(system, where):
    from r6.redaction import apply_redaction
    out = apply_redaction(_obs("o1", system, where))
    text = json.dumps(out)
    assert CANARY not in text
    concept = (out["code"] if where == "code" else
               out["category"][0] if where == "category" else
               out["valueCodeableConcept"])
    coding = concept["coding"][0]
    assert "system" not in coding
    assert coding["code"] == "2160-0"
    # Never preserve the upstream display or text.
    assert coding.get("display") != CANARY and concept.get("text") != CANARY


def test_a_string_system_is_still_labelled_after_redaction():
    from r6.redaction import apply_redaction
    out = apply_redaction(_obs("o1", LOINC))
    coding = out["code"]["coding"][0]
    assert coding["system"] == LOINC
    assert coding["display"] and coding["display"] != CANARY


# --- the write: refused before commit -----------------------------------------

def _write_headers(auth_headers):
    return {**auth_headers, "X-Human-Confirmed": "true",
            "Content-Type": "application/fhir+json"}


def _count(app, tenant_id):
    from r6.models import R6Resource
    with app.app_context():
        return R6Resource.query.filter_by(
            tenant_id=tenant_id, resource_type="Observation").count()


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("system", BAD_SYSTEMS)
def test_a_write_with_a_non_string_system_is_refused_before_commit(
        app, client, tenant_id, auth_headers, system, where):
    before = _count(app, tenant_id)
    r = client.post("/r6/fhir/Observation", data=json.dumps(
        _obs("w1", system, where)), headers=_write_headers(auth_headers))
    assert r.status_code == 422, r.get_data(as_text=True)[:300]
    body = r.get_data(as_text=True)
    assert CANARY not in body
    assert "Coding.system" in body
    assert _count(app, tenant_id) == before


def test_an_update_with_a_non_string_system_is_refused_before_commit(
        app, client, tenant_id, auth_headers):
    hdrs = _write_headers(auth_headers)
    _row(app, tenant_id, _obs("u1", LOINC))
    r = client.put("/r6/fhir/Observation/u1",
                   data=json.dumps(_obs("u1", [LOINC])), headers=hdrs)
    assert r.status_code == 422, r.get_data(as_text=True)[:300]
    got = client.get("/r6/fhir/Observation/u1", headers=auth_headers)
    assert got.get_json()["code"]["coding"][0]["system"] == LOINC


def test_the_refusal_holds_when_an_external_validator_is_configured(
        app, client, tenant_id, auth_headers, monkeypatch):
    """The external validator replaces the structural checks when it is up,
    so the system check cannot live only among them."""
    from r6 import routes
    monkeypatch.setattr(routes.validator, "_is_validator_available",
                        lambda: True)
    monkeypatch.setattr(
        routes.validator, "_validate_external",
        lambda resource, profile=None: {
            "valid": True, "operation_outcome": {
                "resourceType": "OperationOutcome", "issue": []}})
    before = _count(app, tenant_id)
    r = client.post("/r6/fhir/Observation", data=json.dumps(
        _obs("w2", {"url": LOINC})), headers=_write_headers(auth_headers))
    assert r.status_code == 422
    assert _count(app, tenant_id) == before


def test_a_string_system_still_writes(app, client, tenant_id, auth_headers):
    r = client.post("/r6/fhir/Observation",
                    data=json.dumps(_obs("w3", LOINC)),
                    headers=_write_headers(auth_headers))
    assert r.status_code == 201, r.get_data(as_text=True)[:300]


# --- reads of a row already stored -------------------------------------------

@pytest.mark.parametrize("method,url", [
    ("get", "/r6/fhir/Observation?_count=50"),
    ("get", "/r6/fhir/Observation/bad"),
    ("post", "/r6/fhir/Observation/$interpret"),
    ("post", f"/r6/fhir/Observation/$interpret?subject=Patient/{PID}"),
])
@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("system", BAD_SYSTEMS)
def test_a_stored_non_string_system_does_not_break_tenant_reads(
        app, client, tenant_id, tenant_headers, system, where, method, url):
    _patient(app, tenant_id)
    _row(app, tenant_id, _obs("good", LOINC))
    _row(app, tenant_id, _obs("bad", system, where))
    r = getattr(client, method)(url, headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert CANARY not in r.get_data(as_text=True)
    _strict_body(r)
