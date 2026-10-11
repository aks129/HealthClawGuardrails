"""#922 re-test: every field the model, the chart or an MCP client reads is
checked by shape or against a closed set, not copied. Companion to
tests/test_922_bypasses.py (the red-team's five cases). Synthetic data only.
"""
import json

import pytest

from careagents import labs_timeline
from careagents.agent import _summarize_bundle
from models import db
from r6.labs.report import annotate_observation
from r6.models import R6Resource
from r6.redaction import apply_redaction, unit_is_shaped

NAME = "Quixtrandle"
SNOMED = "http://snomed.info/sct"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
LOINC = "http://loinc.org"

#: The only keys an item may carry: each is a parsed value, a closed-set
#: value or a phrase written here.
ITEM_KEYS = {"type", "name", "status", "value", "date", "unreadable",
             "uncoded", "note"}


def _summary(*resources, redact=True):
    bundle = {"entry": [{"resource": r} for r in resources]}
    return _summarize_bundle(apply_redaction(bundle) if redact else bundle)


def _obs(**kw):
    return {"resourceType": "Observation", "status": kw.get("status", "final"),
            "code": {"coding": [{"system": LOINC, "code": "13457-7"}]},
            "effectiveDateTime": kw.get("eff", "2026-09-01"),
            "valueQuantity": kw.get("vq", {"value": 99, "unit": "mg/dL"})}


def _cond(system, code):
    return {"resourceType": "Condition", "id": "c",
            "code": {"coding": [{"system": system, "code": code}]}}


# --- B1: status ------------------------------------------------------------

def test_a_valid_status_still_reaches_the_model():
    assert _summary(_obs())[0]["status"] == "final"


def test_a_status_from_another_type_does_not_pass():
    """MUTATION: a union of every type's statuses -> red."""
    assert "status" not in _summary(_obs(status="active"))[0]


def test_a_type_with_no_status_table_passes_none():
    """search_records takes any resource_type the model names."""
    res = {"resourceType": "Basic", "status": f"{NAME}",
           "code": {"coding": [{"system": SNOMED, "code": "44054006"}]}}
    assert "status" not in _summary(res)[0]


# --- B2: date --------------------------------------------------------------

def test_a_real_datetime_gives_its_date():
    assert _summary(_obs(eff="2026-09-01T08:30:00Z"))[0]["date"] == "2026-09-01"


@pytest.mark.parametrize("eff", ["2026-13-40", "Quixtr-and", "20260901"])
def test_a_non_date_gives_no_date(eff):
    assert "date" not in _summary(_obs(eff=eff))[0]


# --- B3: code token --------------------------------------------------------

@pytest.mark.parametrize("system,code", [
    (SNOMED, "123456789"),        # SSN without dashes: fails Verhoeff
    (SNOMED, "5550137719"),       # phone without dashes: fails Verhoeff
    (SNOMED, "E11.9"),            # an ICD-10 code under the SNOMED system
    (ICD10, "A00.ANNA"),          # a name after the dot
    (LOINC, "2026-09-01"),        # a date
    ("http://example.org/ids", "860975"),   # a system with no shape
])
def test_a_code_not_shaped_for_its_system_is_not_named(system, code):
    # No code is ever named now, shaped or not; the system may be.
    item = _summary(_cond(system, code), redact=False)[0]
    assert item["name"].startswith("unlabeled record")
    assert code not in item["name"]
    assert item["unreadable"] is True


def test_a_real_snomed_code_names_its_system_not_the_code():
    # Even a valid SNOMED id is not passed: a check digit admits one SSN or
    # phone number in ten (#922 re-test R1).
    assert _summary(_cond(SNOMED, "44054006"), redact=False)[0]["name"] == \
        "unlabeled record (SNOMED CT code)"


# --- allowlist -------------------------------------------------------------

def test_no_item_carries_a_key_outside_the_allowlist():
    hostile = _obs(status=NAME, eff=NAME)
    hostile.update({"note": [{"text": NAME}], "issued": NAME,
                    "valueString": NAME, "category": [{"text": NAME}],
                    "performer": [{"display": NAME}]})
    items = _summary(hostile, _cond(SNOMED, NAME))
    for item in items:
        assert set(item) <= ITEM_KEYS
    assert NAME not in json.dumps(items)


# --- B4: engine redaction --------------------------------------------------

@pytest.mark.parametrize("unit", ["Smith", "widgets", "Quixtrandle.",
                                  "mg/Quixtrandle"])
def test_a_word_is_not_unit_shaped(unit):
    """MUTATION: drop the letters-only rule or the long-run rule -> red."""
    assert not unit_is_shaped(unit)


@pytest.mark.parametrize("unit", [
    "mg/dL", "mmol/L", "%", "[IU]/L", "U/L", "g", "L", "mL", "kg", "mmHg",
    "mm[Hg]", "bpm", "Cel", "h", "min", "fL", "pg", "pH", "{beats}/min",
    "10*3/uL", "mL/min/{1.73_m2}", "µmol/L", "kg/m2", "{tbl}", "serving"])
def test_a_real_unit_survives(unit):
    red = apply_redaction({"resourceType": "Observation",
                           "valueQuantity": {"value": 1, "unit": unit,
                                             "code": unit}})
    assert red["valueQuantity"]["unit"] == unit
    assert red["valueQuantity"]["code"] == unit


def test_every_consumer_unit_survives_the_engine():
    """The engine's word list must cover every letters-only unit a consumer
    allows, or a real reading is blanked before CareAgents sees it."""
    from careagents.agent import _RECORD_UNITS
    for unit in labs_timeline.UCUM_ALLOWED | _RECORD_UNITS:
        assert unit_is_shaped(unit), unit


def test_a_free_text_quantity_code_is_dropped():
    """MUTATION: skip the Quantity.code rule -> red."""
    red = apply_redaction({"resourceType": "Observation", "valueQuantity": {
        "value": 99, "system": "http://unitsofmeasure.org", "code": NAME}})
    assert NAME not in json.dumps(red)
    assert red["valueQuantity"]["value"] == 99


def test_a_coding_code_is_not_treated_as_a_unit():
    red = apply_redaction(_cond(SNOMED, "44054006"))
    assert red["code"]["coding"][0]["code"] == "44054006"


def test_the_table_range_carries_no_free_text_unit():
    """annotate_observation adds a range after redaction ran; its unit is
    the stored record's."""
    out = annotate_observation(
        {"resourceType": "Observation"},
        {"range_source": "table", "low": 1, "high": 2, "unit": NAME})
    assert NAME not in json.dumps(out)


def test_the_chart_flag_is_a_flag():
    res = _obs()
    res["interpretation"] = [{"coding": [{"code": NAME}]}]
    series = labs_timeline.build_series({"entry": [{"resource": res}]})
    assert NAME not in json.dumps(series)


# --- B5: $stats ------------------------------------------------------------

def test_stats_unit_comes_from_the_requested_code(client, app, tenant_id,
                                                   tenant_headers):
    """MUTATION: take the unit from the tenant's first Observation -> red."""
    with app.app_context():
        for rid, code, unit in (("s1", "2093-3", "kg"), ("s2", "4548-4", "%")):
            body = {"resourceType": "Observation", "id": rid,
                    "status": "final",
                    "code": {"coding": [{"system": LOINC, "code": code}]},
                    "valueQuantity": {"value": 5, "unit": unit}}
            db.session.add(R6Resource(resource_id=rid, resource_type="Observation",
                                      resource_json=json.dumps(body),
                                      tenant_id=tenant_id))
        db.session.commit()
    r = client.get("/r6/fhir/Observation/$stats?code=4548-4",
                   headers=tenant_headers)
    assert r.status_code == 200
    params = {p["name"]: p for p in r.get_json()["parameter"]}
    assert params["unit"]["valueString"] == "%"
