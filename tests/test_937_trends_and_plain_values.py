"""#937, beta shakeout: a trend question and an everyday reading, answered.

"How has my A1c changed?" got "you can see the direction in the chart", four
times out of four: the web chart handed the model the chart's shape only. A
blood pressure of 138/88 and a glucose of 180 were read out as results that
"could not be interpreted". The real engine and the sample records where the
pair meets; a fake client where an edge needs building.

Synthetic data only.
"""

from __future__ import annotations

import json

import pytest

from careagents.agent import _execute_tool
from r6.labs.interpret import NO_NUMERIC_VALUE, UNKNOWN_ANALYTE
from r6.labs.report import build_consumer_summary
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_qa884_units_cross_layer import _world as _world_884

_LOINC = "http://loinc.org"


def _web_timeline(hc, tenant, topic):
    return json.loads(_execute_tool(hc, tenant, "show_lab_timeline",
                                    {"topic": topic}, [], surface="web"))


# --- trends on the web chart ------------------------------------------------

def test_the_web_chart_gives_the_model_first_latest_and_direction(
        cfg, svc, monkeypatch):  # noqa: F811
    """MUTATION: hand the web path the chart's shape again (name, readings,
    trend_plottable) -> red."""
    chain = Chain(cfg, svc, monkeypatch)
    for topic, name, first, latest in (("a1c", "Hemoglobin A1c", 9.2, 8.1),
                                       ("ldl", "LDL cholesterol", 162, 112)):
        out = _web_timeline(chain.hc, TENANT, topic)
        assert out["chart_shown"] is True
        (s,) = out["series"]
        assert s["name"] == name
        assert s["first"]["value"] == first and s["first"]["date"], s
        assert s["latest"]["value"] == latest and s["latest"]["date"], s
        assert s["direction"] == "lower", s
        # The note asks for the change in words, and not every value.
        assert "first reading to its latest" in out["note"]
        assert "Do not list every value" in out["note"]


def test_the_web_chart_gives_no_numbers_across_units_that_differ(
        cfg, svc, monkeypatch):  # noqa: F811
    """The sample's % A1c readings, then one in mmol/mol: "lower" from 8.1 %
    to 43 mmol/mol is not a finding (#884 QA F1), on the web as by text."""
    chain = _world_884(cfg, svc, monkeypatch)
    out = _web_timeline(chain.hc, TENANT, "a1c")
    (s,) = out["series"]
    assert s["trend_plottable"] and s["readings"] >= 2, s
    for key in ("first", "latest", "direction"):
        assert key not in s, s
    assert "no direction" in out["note"]


class _UnconfirmedHC:
    """Two A1c readings whose stated unit is not one we recognise."""

    def interpret_labs(self, _tenant):
        def a1c(when):
            return {"resource": {
                "resourceType": "Observation", "status": "final",
                "code": {"coding": [{"system": _LOINC, "code": "4548-4"}]},
                "effectiveDateTime": when,
                "valueQuantity": {"value": 7.3, "unit": "pct-ish"}}}
        return {"bundle": {"entry": [a1c("2026-03-01"), a1c("2026-06-01")]},
                "disclaimer": ""}


def test_the_web_chart_gives_no_numbers_for_an_unconfirmed_unit():
    out = _web_timeline(_UnconfirmedHC(), "t", "a1c")
    assert out["series"] == [{"name": "Hemoglobin A1c", "readings": 0,
                              "trend_plottable": False,
                              "readings_unit_unconfirmed": 2}]
    assert "7.3" not in json.dumps(out)


# --- readings the range check did not score -------------------------------------

def test_an_unscored_blood_pressure_and_glucose_reach_the_model_plainly(
        cfg, svc, monkeypatch):  # noqa: F811
    """MUTATION: drop `not_evaluated` from get_labs -> red. MUTATION: keep
    the engine's unevaluated_note in the model's summary -> red."""
    chain = Chain(cfg, svc, monkeypatch)
    out = json.loads(_execute_tool(chain.hc, TENANT, "get_labs", {}, [],
                                   surface="web"))
    # One row per analyte, checked as a list: a dict alone would keep
    # whichever duplicate came last and pass on storage order (QA #1021).
    assert sorted(x["analyte"] for x in out["not_evaluated"]) == \
        ["Blood pressure", "Glucose (blood)"], out["not_evaluated"]
    got = {x["analyte"]: x for x in out["not_evaluated"]}
    bp, glucose = got["Blood pressure"], got["Glucose (blood)"]
    assert (bp["value"], bp["unit"]) == ("138/88", "mmHg"), bp
    assert (glucose["value"], glucose["unit"]) == (180, "mg/dL"), glucose
    for x in (bp, glucose):
        assert x["date"] and x["reference_range_in_records"] is False, x
    raw = json.dumps(out)
    assert "could not be interpreted" not in raw.replace(
        "Do not say it 'could not be interpreted'", "")
    assert "unevaluated_note" not in out["consumer_summary"]
    assert "no reference range in the records" in out["note"]
    # Never a verdict: the note forbids one, and the model gets no range.
    assert "Do not call it normal" in out["note"]
    # The seed writes `display` on exactly these two readings; labels come
    # from the engine's terminology table, never from the reading.
    for display in ("Systolic BP", "Diastolic BP",
                    "Blood pressure systolic and diastolic",
                    "Glucose [Mass/volume] in Blood"):
        assert display not in raw, display


def _bundle_of(*resources):
    return {"entry": [{"resource": r} for r in resources]}


def _obs(code, **kw):
    return {"resourceType": "Observation",
            "code": {"coding": [{"system": _LOINC, "code": code}]},
            "effectiveDateTime": "2026-10-01", **kw}


class _LabsHC:
    def __init__(self, consumer, bundle):
        self.payload = {"consumer": consumer, "bundle": bundle,
                        "disclaimer": ""}

    def interpret_labs(self, _tenant):
        return self.payload


_CONSUMER = {"lines": [], "unevaluated": "partly-unevaluated",
             "unevaluated_count": 1, "unevaluated_analytes": ["Blood pressure"],
             "unevaluated_codes": ["55284-4"],
             "unevaluated_note": "This check did not evaluate Blood pressure."}


def _bp(sys_unit="mmHg", dia_unit="mmHg"):
    return _obs("55284-4", component=[
        {"code": {"coding": [{"system": _LOINC, "code": "8480-6"}]},
         "valueQuantity": {"value": 150, "unit": sys_unit}},
        {"code": {"coding": [{"system": _LOINC, "code": "8462-4"}]},
         "valueQuantity": {"value": 95, "unit": dia_unit}}])


def _labs(hc):
    return json.loads(_execute_tool(hc, "t", "get_labs", {}, []))


def test_a_blood_pressure_in_a_unit_we_do_not_recognise_has_no_number():
    out = _labs(_LabsHC(_CONSUMER, _bundle_of(_bp(sys_unit="mm/x"))))
    (bp,) = out["not_evaluated"]
    assert (bp["value"], bp["unit"]) == (None, ""), bp
    assert "150" not in json.dumps(out)


@pytest.mark.parametrize("sys_unit,dia_unit", [
    ("mmHg", "cm[H2O]"),  # both allow-listed, but not the same unit
    (None, None),          # neither part states a unit
])
def test_a_blood_pressure_needs_one_allowed_unit_on_both_parts(
        sys_unit, dia_unit):
    """QA #938: "138/88" only when both parts carry the same allowed unit.
    MUTATION: drop the systolic/diastolic unit-equality check -> red (first
    case). MUTATION: drop `not unit` for a part -> red (second case); each
    survived the PR's own tests, which only vary one part."""
    out = _labs(_LabsHC(_CONSUMER, _bundle_of(
        _bp(sys_unit=sys_unit, dia_unit=dia_unit))))
    (bp,) = out["not_evaluated"]
    assert (bp["value"], bp["unit"]) == (None, ""), bp
    assert "150" not in json.dumps(out)


def test_a_reading_with_no_unit_and_no_usual_unit_has_no_number():
    """2339-0 has no usual unit on file here: a bare 180 would be read with
    whatever unit the reader supplies (#884 QA F1)."""
    consumer = dict(_CONSUMER, unevaluated_analytes=["Glucose (blood)"],
                    unevaluated_codes=["2339-0"])
    out = _labs(_LabsHC(consumer, _bundle_of(
        _obs("2339-0", valueQuantity={"value": 180}))))
    (g,) = out["not_evaluated"]
    assert (g["value"], g["unit"]) == (None, ""), g
    assert "180" not in json.dumps(out)


def test_a_reading_that_does_not_pair_with_its_code_falls_back_to_the_note():
    """The engine's order, checked code by code: a reading under another
    code is not this name's reading, and its number is not passed on."""
    out = _labs(_LabsHC(_CONSUMER, _bundle_of(
        _obs("2339-0", valueQuantity={"value": 180, "unit": "mg/dL"}))))
    assert "not_evaluated" not in out
    assert "180" not in json.dumps(out)
    assert "using unevaluated_note" in out["note"]


def test_a_reading_with_a_reference_range_on_file_says_so():
    consumer = dict(_CONSUMER, unevaluated_analytes=["Glucose (blood)"],
                    unevaluated_codes=["2339-0"])
    out = _labs(_LabsHC(consumer, _bundle_of(_obs(
        "2339-0", valueQuantity={"value": 180, "unit": "mg/dL"},
        referenceRange=[{"text": "lab text, never passed on"}]))))
    (g,) = out["not_evaluated"]
    assert g["reference_range_in_records"] is True
    assert "lab text" not in json.dumps(out)


# --- the engine's own note ------------------------------------------------------

def test_the_engine_note_names_each_cause_rather_than_could_not_be_interpreted():
    """MUTATION: restore "they could not be interpreted, for more than one
    reason" -> red."""
    out = build_consumer_summary([
        {"analyte": None, "loinc": "55284-4", "flag": None,
         "indeterminate_reason": NO_NUMERIC_VALUE},
        {"analyte": None, "loinc": "2339-0", "flag": None,
         "indeterminate_reason": UNKNOWN_ANALYTE},
    ])
    note = out["unevaluated_note"]
    assert "could not be interpreted" not in note
    assert "Blood pressure, because the result carried no number" in note
    assert ("Glucose (blood), because there is no reference range on file "
            "for it") in note
    assert out["unevaluated_codes"] == ["55284-4", "2339-0"]
