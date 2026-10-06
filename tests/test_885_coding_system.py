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


def _obs(rid, system, where="code", code="2160-0"):
    obs = {"resourceType": "Observation", "id": rid, "status": "final",
           "subject": {"reference": f"Patient/{PID}"},
           "effectiveDateTime": "2026-09-01T08:00:00Z",
           "code": {"coding": [{"system": LOINC, "code": "2160-0"}]},
           "valueQuantity": {"value": 0.9, "unit": "mg/dL"}}
    bad = {"coding": [{"system": system, "code": code,
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


# --- Coding.code: the same gap, closed the same way ---------------------------

#: Code values that are neither a string nor an int. An object or a list can
#: carry text the same way an object system could.
BAD_CODES = [
    pytest.param({"value": CANARY}, id="object-canary"),
    pytest.param({"code": "2160-0", "note": {"who": CANARY}},
                 id="object-nested-canary"),
    pytest.param([CANARY], id="list-canary"),
    pytest.param(["2160-0"], id="list"),
    pytest.param(1.5, id="float"),
    pytest.param(True, id="bool"),
    pytest.param(None, id="null"),
]


def _concept(out, where):
    return (out["code"] if where == "code" else
            out["category"][0] if where == "category" else
            out["valueCodeableConcept"])


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("code", BAD_CODES)
def test_apply_redaction_drops_a_code_that_is_not_a_string_or_int(
        code, where):
    from r6.redaction import apply_redaction
    out = apply_redaction(_obs("o1", LOINC, where, code=code))
    assert CANARY not in json.dumps(out)
    coding = _concept(out, where)["coding"][0]
    assert "code" not in coding
    assert coding["system"] == LOINC
    assert "display" not in coding


@pytest.mark.parametrize("where", WHERE)
def test_an_int_code_is_kept_as_its_string_form(where):
    from r6.redaction import apply_redaction
    from r6.terminology import CVX
    out = apply_redaction(_obs("o1", CVX, where, code=3))
    coding = _concept(out, where)["coding"][0]
    assert coding["code"] == "3"
    assert coding.get("display") != CANARY


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("code", BAD_CODES)
def test_a_write_with_a_bad_code_is_refused_before_commit(
        app, client, tenant_id, auth_headers, code, where):
    before = _count(app, tenant_id)
    r = client.post("/r6/fhir/Observation", data=json.dumps(
        _obs("w1", LOINC, where, code=code)),
        headers=_write_headers(auth_headers))
    assert r.status_code == 422, r.get_data(as_text=True)[:300]
    body = r.get_data(as_text=True)
    assert CANARY not in body
    assert "Coding.code" in body and ".coding[0].code" in body
    assert _count(app, tenant_id) == before


def test_a_write_with_an_int_code_is_accepted(
        app, client, tenant_id, auth_headers):
    r = client.post("/r6/fhir/Observation",
                    data=json.dumps(_obs("w4", LOINC, code=2160)),
                    headers=_write_headers(auth_headers))
    assert r.status_code == 201, r.get_data(as_text=True)[:300]
    assert r.get_json()["code"]["coding"][0]["code"] == "2160"


def test_an_absent_code_is_not_refused(app, client, tenant_id, auth_headers):
    obs = _obs("w5", LOINC)
    obs["code"]["coding"][0].pop("code")
    r = client.post("/r6/fhir/Observation", data=json.dumps(obs),
                    headers=_write_headers(auth_headers))
    assert r.status_code == 201, r.get_data(as_text=True)[:300]


@pytest.mark.parametrize("method,url", [
    ("get", "/r6/fhir/Observation?_count=50"),
    ("get", "/r6/fhir/Observation/bad"),
    ("post", "/r6/fhir/Observation/$interpret"),
    ("post", f"/r6/fhir/Observation/$interpret?subject=Patient/{PID}"),
])
@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("code", BAD_CODES)
def test_a_stored_bad_code_does_not_break_or_leak_on_tenant_reads(
        app, client, tenant_id, tenant_headers, code, where, method, url):
    _patient(app, tenant_id)
    _row(app, tenant_id, _obs("good", LOINC))
    _row(app, tenant_id, _obs("bad", LOINC, where, code=code))
    r = getattr(client, method)(url, headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert CANARY not in r.get_data(as_text=True)
    _strict_body(r)


# --- R886-1: every Coding-shaped dict, not only those in a `coding` list -------

#: Places a Coding sits with no `coding` list around it.
BARE_PLACES = {
    "valueCoding": lambda r, c: r.__setitem__("valueCoding", c),
    "extension.valueCoding": lambda r, c: r.__setitem__(
        "extension", [{"url": "http://x.example/e", "valueCoding": c}]),
    "deep-extension.valueCoding": lambda r, c: r.__setitem__(
        "extension", [{"url": "http://x.example/e", "extension": [
            {"url": "inner", "extension": [
                {"url": "deeper", "valueCoding": c}]}]}]),
    "class": lambda r, c: r.__setitem__("class", c),
    "meta.tag": lambda r, c: r.__setitem__("meta", {"tag": [c]}),
    "meta.security": lambda r, c: r.__setitem__("meta", {"security": [c]}),
}

BARE_BAD = [
    pytest.param("system", {"foo": CANARY}, id="system-object"),
    pytest.param("system", [CANARY], id="system-list"),
    pytest.param("system", 5, id="system-int"),
    pytest.param("code", {"foo": CANARY}, id="code-object"),
    pytest.param("code", {"value": {"deep": CANARY}}, id="code-object-deep"),
    pytest.param("code", [CANARY], id="code-list"),
    pytest.param("code", 1.5, id="code-float"),
    pytest.param("code", True, id="code-bool"),
]


def _bare(rid, place, field, value):
    res = _obs(rid, LOINC)
    coding = {"system": LOINC, "code": "2160-0"}
    coding[field] = value
    BARE_PLACES[place](res, coding)
    return res


def _find_bare(out, place):
    if place == "valueCoding":
        return out["valueCoding"]
    if place == "class":
        return out["class"]
    if place.startswith("meta."):
        return out["meta"][place.split(".")[1]][0]
    node = out["extension"][0]
    while "extension" in node:
        node = node["extension"][0]
    return node["valueCoding"]


@pytest.mark.parametrize("place", sorted(BARE_PLACES))
@pytest.mark.parametrize("field,value", BARE_BAD)
def test_redaction_cleans_a_bare_coding(place, field, value):
    from r6.redaction import apply_redaction
    out = apply_redaction(_bare("o1", place, field, value))
    assert CANARY not in json.dumps(out)
    coding = _find_bare(out, place)
    assert field not in coding
    other = "code" if field == "system" else "system"
    assert other in coding


@pytest.mark.parametrize("place", sorted(BARE_PLACES))
def test_redaction_keeps_a_bare_int_code_as_its_string(place):
    from r6.redaction import apply_redaction
    out = apply_redaction(_bare("o1", place, "code", 2160))
    assert _find_bare(out, place)["code"] == "2160"


@pytest.mark.parametrize("place", sorted(BARE_PLACES))
@pytest.mark.parametrize("field,value", BARE_BAD)
def test_the_validator_refuses_a_bare_coding(
        app, client, tenant_id, auth_headers, place, field, value):
    before = _count(app, tenant_id)
    r = client.post("/r6/fhir/Observation", data=json.dumps(
        _bare("wb", place, field, value)), headers=_write_headers(auth_headers))
    body = r.get_data(as_text=True)
    assert r.status_code == 422, body[:300]
    assert CANARY not in body
    assert f"Coding.{field}" in body
    assert _count(app, tenant_id) == before


#: Shapes that are NOT a malformed Coding and must survive untouched.
def test_a_codeableconcept_valued_code_is_not_mistaken_for_a_coding():
    """component.code is a CodeableConcept, including one carrying only an
    extension (data-absent-reason); none of them is dropped."""
    from r6.redaction import apply_redaction
    res = _obs("o1", LOINC)
    res.pop("valueQuantity")
    dar = {"extension": [{
        "url": "http://hl7.org/fhir/StructureDefinition/data-absent-reason",
        "valueCode": "unknown"}]}
    res["component"] = [
        {"code": {"coding": [{"system": LOINC, "code": "8480-6"}]},
         "valueQuantity": {"value": 120, "unit": "mm[Hg]",
                           "system": "http://unitsofmeasure.org",
                           "code": "mm[Hg]"}},
        {"code": dar, "valueQuantity": {"value": 80}}]
    out = apply_redaction(res)
    assert out["component"][0]["code"]["coding"][0]["code"] == "8480-6"
    assert out["component"][0]["valueQuantity"]["code"] == "mm[Hg]"
    assert out["component"][1]["code"]["extension"][0]["valueCode"] == "unknown"


def test_a_questionnaire_item_code_list_of_codings_is_kept():
    from r6.redaction import apply_redaction
    q = {"resourceType": "Questionnaire", "status": "active", "item": [{
        "linkId": "1", "type": "decimal",
        "code": [{"system": LOINC, "code": "29463-7"},
                 {"system": [CANARY], "code": {"x": CANARY}}, CANARY]}]}
    out = apply_redaction(q)
    codes = out["item"][0]["code"]
    assert CANARY not in json.dumps(out)
    assert codes[0]["code"] == "29463-7" and codes[0]["system"] == LOINC
    assert len(codes) == 2 and codes[1] == {}


def test_a_valid_questionnaire_and_coded_resources_still_write(
        app, client, tenant_id, auth_headers):
    hdrs = _write_headers(auth_headers)
    q = {"resourceType": "Questionnaire", "status": "active", "item": [{
        "linkId": "1", "type": "choice",
        "code": [{"system": LOINC, "code": "29463-7"}],
        "answerOption": [{"valueCoding": {"system": LOINC,
                                          "code": "LA33-6"}}]}]}
    r = client.post("/r6/fhir/Questionnaire", data=json.dumps(q), headers=hdrs)
    assert r.status_code == 201, r.get_data(as_text=True)[:300]
    obs = _obs("w-ok", LOINC)
    obs["meta"] = {"tag": [{"system": "https://x.example/t", "code": "t1"}],
                   "security": [{"system": "http://terminology.hl7.org/"
                                 "CodeSystem/v3-Confidentiality",
                                 "code": "N"}]}
    obs["valueQuantity"]["system"] = "http://unitsofmeasure.org"
    obs["valueQuantity"]["code"] = "mg/dL"
    r = client.post("/r6/fhir/Observation", data=json.dumps(obs), headers=hdrs)
    assert r.status_code == 201, r.get_data(as_text=True)[:300]


# --- QA: patient-controlled redaction on a malformed meta.tag -------------------

@pytest.mark.parametrize("tag", [
    pytest.param({"system": "https://x.example", "code": {"foo": CANARY}},
                 id="code-object"),
    pytest.param({"system": "https://x.example", "code": [CANARY]},
                 id="code-list"),
    pytest.param({"system": "https://x.example", "code": {"text": CANARY}},
                 id="code-cc-shaped"),
    pytest.param("not-a-coding", id="tag-string"),
    pytest.param(None, id="tag-null"),
])
def test_patient_controlled_redaction_survives_a_malformed_meta_tag(tag):
    from r6.redaction import apply_patient_controlled_redaction
    res = _obs("o1", LOINC)
    res["meta"] = {"tag": [tag]}
    out = apply_patient_controlled_redaction(res, "hc-patient-1")
    assert CANARY not in json.dumps(out)
    codes = [t.get("code") for t in out["meta"]["tag"] if isinstance(t, dict)]
    assert "ANONYED" in codes and "patient-controlled" in codes


@pytest.mark.parametrize("meta", [
    pytest.param({"tag": {"code": "x"}}, id="tag-object"),
    pytest.param({"tag": "x"}, id="tag-string"),
    pytest.param("x", id="meta-string"),
])
def test_patient_controlled_redaction_survives_a_malformed_meta(meta):
    from r6.redaction import apply_patient_controlled_redaction
    res = _obs("o1", LOINC)
    res["meta"] = meta
    out = apply_patient_controlled_redaction(res, "hc-patient-1")
    codes = [t.get("code") for t in out["meta"]["tag"] if isinstance(t, dict)]
    assert "ANONYED" in codes and "patient-controlled" in codes


def test_patient_controlled_redaction_does_not_restamp_an_existing_tag():
    from r6.redaction import apply_patient_controlled_redaction
    res = _obs("o1", LOINC)
    res["meta"] = {"tag": [{"system": "https://healthclaw.io/tags",
                            "code": "patient-controlled"}]}
    out = apply_patient_controlled_redaction(res, "hc-patient-1")
    codes = [t.get("code") for t in out["meta"]["tag"]]
    assert codes.count("patient-controlled") == 1


# --- the third redaction path: $deidentify (deidentified-preview, default) ----

#: QA's shapes: a junk system or code in code, category, valueCoding and an
#: extension's valueCoding, stored as an upstream feed would land them.
DEID_SHAPES = {
    "code-system-object": lambda r: r.__setitem__("code", {"coding": [
        {"system": {"foo": CANARY}, "code": "2160-0"}]}),
    "code-system-list": lambda r: r.__setitem__("code", {"coding": [
        {"system": [CANARY], "code": "2160-0"}]}),
    "code-code-object": lambda r: r.__setitem__("code", {"coding": [
        {"system": LOINC, "code": {"foo": CANARY}}]}),
    "category-system-list": lambda r: r.__setitem__("category", [{"coding": [
        {"system": [CANARY], "code": "laboratory"}]}]),
    "category-code-list": lambda r: r.__setitem__("category", [{"coding": [
        {"system": LOINC, "code": [CANARY]}]}]),
    "valueCoding-code-object": lambda r: r.__setitem__("valueCoding", {
        "system": LOINC, "code": {"foo": CANARY}}),
    "valueCoding-system-list": lambda r: r.__setitem__("valueCoding", {
        "system": [CANARY], "code": "x"}),
    "extension-valueCoding-code-list": lambda r: r.__setitem__("extension", [
        {"url": "http://x.example/e", "valueCoding": {
            "system": LOINC, "code": [CANARY]}}]),
    "deep-extension-valueCoding-system-object": lambda r: r.__setitem__(
        "extension", [{"url": "http://x.example/e", "extension": [
            {"url": "inner", "valueCoding": {
                "system": {"foo": CANARY}, "code": "x"}}]}]),
}


def _deid_row(app, tenant_id, rid, shape):
    res = _obs(rid, LOINC)
    DEID_SHAPES[shape](res)
    _row(app, tenant_id, res)


@pytest.mark.parametrize("shape", sorted(DEID_SHAPES))
def test_deidentify_default_mode_never_echoes_a_junk_system_or_code(
        app, client, tenant_id, auth_headers, shape):
    _patient(app, tenant_id)
    _deid_row(app, tenant_id, "deid-1", shape)
    r = client.get("/r6/fhir/Observation/deid-1/$deidentify",
                   headers=auth_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert CANARY not in r.get_data(as_text=True)
    _strict_body(r)


@pytest.mark.parametrize("shape", sorted(DEID_SHAPES))
def test_deidentify_patient_controlled_mode_never_echoes_either(
        app, client, tenant_id, auth_headers, shape):
    _patient(app, tenant_id)
    _deid_row(app, tenant_id, "deid-2", shape)
    r = client.get("/r6/fhir/Observation/deid-2/$deidentify"
                   "?mode=patient-controlled", headers=auth_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert CANARY not in r.get_data(as_text=True)


@pytest.mark.parametrize("shape", sorted(DEID_SHAPES))
def test_deidentify_resource_engine_cleans_codings(shape):
    from r6.health_compliance import deidentify_resource
    res = _obs("o1", LOINC)
    DEID_SHAPES[shape](res)
    assert CANARY not in json.dumps(deidentify_resource(res))


def test_deidentify_keeps_good_codings_and_an_int_code_as_string():
    from r6.health_compliance import deidentify_resource
    res = _obs("o1", LOINC)
    res["code"] = {"coding": [{"system": LOINC, "code": "2160-0"}]}
    res["valueCoding"] = {"system": "http://x.example/cs", "code": 7}
    out = deidentify_resource(res)
    assert out["code"]["coding"][0] == {"system": LOINC, "code": "2160-0"}
    assert out["valueCoding"] == {"system": "http://x.example/cs", "code": "7"}
    assert out["meta"]["security"][-1]["code"] == "ANONYED"
