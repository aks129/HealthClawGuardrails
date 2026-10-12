"""QA of PR #922 at fc55f27: pin the F1 and F4 fixes, which a mutation run
showed no test held. Synthetic data only.

- A refused Quantity.code is replaced, never dropped. Dropped, a reading with
  no `unit` reads as "no unit stated", and the brief then puts the analyte's
  usual unit beside a number it was not measured in (#884 QA F1).
- On the web surface, an analyte whose every reading was withheld still
  reaches the model with its count, and no chart is claimed for it (F4).
"""
import json

from careagents.agent import _execute_tool
from r6.brief.engine import _unit as brief_unit
from r6.redaction import UNIT_REDACTED, apply_redaction


def test_a_refused_coded_unit_is_replaced_so_no_usual_unit_stands_in():
    out = apply_redaction({
        "resourceType": "Observation",
        "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
        "valueQuantity": {"value": 47, "system": "http://unitsofmeasure.org",
                          "code": "Zebulon Quixtrandle"}})
    vq = out["valueQuantity"]
    assert vq["code"] == UNIT_REDACTED
    # A1c's usual unit is %; 47 here was not stated in %.
    assert brief_unit(vq, ["4548-4"]) is None


def _a1c(unit, when):
    return {"resource": {
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
        "effectiveDateTime": when,
        "valueQuantity": {"value": 7.1, "unit": unit}}}


class _HC:
    def interpret_labs(self, _tenant):
        return {"bundle": {"entry": [_a1c(UNIT_REDACTED, "2026-03-01"),
                                     _a1c(UNIT_REDACTED, "2026-06-01")]},
                "disclaimer": ""}


def test_web_timeline_says_withheld_readings_exist_and_claims_no_chart():
    events = []
    out = json.loads(_execute_tool(_HC(), "t", "show_lab_timeline",
                                   {"topic": "a1c"}, events))
    assert events == []
    assert out["chart_shown"] is False
    assert out["series"] == [{"name": "Hemoglobin A1c", "readings": 0,
                              "trend_plottable": False,
                              "readings_unit_unconfirmed": 2}]
    assert "7.1" not in json.dumps(out)
