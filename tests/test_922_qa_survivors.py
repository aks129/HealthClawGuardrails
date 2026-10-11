"""QA of PR #922: three guards that a mutation run showed no test pinned.

Each test here fails when its guard is removed:
- the letterless-unit rule in r6.redaction.unit_is_shaped (only %, 1 and
  10*n may have no unit word; the four-digit rule alone lets "911" through);
- the LOINC mod-10 check digit in r6.labs.report._loinc_ok;
- the text timeline's withheld count (careagents.labs_timeline.build_series
  keep_withheld), so a model on a text surface says readings exist rather
  than that there are none.
Synthetic data only.
"""
import pytest

from careagents import labs_timeline
from careagents.agent import _timeline_in_words
from r6.labs.report import _undecided_name
from r6.redaction import unit_is_shaped


@pytest.mark.parametrize("unit", ["911", "123", "12.5", "/100", "12-34", "0"])
def test_a_unit_with_no_unit_word_is_only_a_letterless_ucum_unit(unit):
    assert not unit_is_shaped(unit)


@pytest.mark.parametrize("unit", ["%", "1", "10*3", "10^6"])
def test_the_letterless_ucum_units_still_pass(unit):
    assert unit_is_shaped(unit)


def test_a_loinc_shaped_code_with_a_wrong_check_digit_is_not_named():
    # 12345-5 has a valid mod-10 check digit and is not in the terminology
    # table; 12345-6 differs only in the check digit.
    assert _undecided_name({"loinc": "12345-5"}) == "LOINC 12345-5"
    assert "12345-6" not in _undecided_name({"loinc": "12345-6"})


def _a1c(unit, when):
    return {"resource": {
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
        "effectiveDateTime": when,
        "valueQuantity": {"value": 7.1, "unit": unit}}}


def test_text_timeline_keeps_a_series_whose_units_were_all_withheld():
    bundle = {"entry": [_a1c("[Redacted]", "2026-03-01"),
                        _a1c("[Redacted]", "2026-06-01")]}
    keys = labs_timeline.keys_for_topic("a1c")
    # The chart has nothing to draw.
    assert labs_timeline.build_series(bundle, keys) == []
    # A text surface still learns the readings exist, with no numbers.
    series = labs_timeline.build_series(bundle, keys, keep_withheld=True)
    assert len(series) == 1 and series[0]["readings"] == []
    assert series[0]["withheld"] == 2
    words = _timeline_in_words(series[0])
    assert words["readings"] == 0
    assert words["readings_unit_unconfirmed"] == 2
    assert "7.1" not in str(words)
