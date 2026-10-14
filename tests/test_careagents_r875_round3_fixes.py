"""#875 round 3: the fixes behind R875-2/3/4, and the labs pairing check.

The three findings are pinned in tests/test_careagents_r875_round3_exploits.py
(once strict xfails). These cover what they do not: the web chart's dates,
the UCUM allowlist's edges, the analyte check in the labs pairing, and the
not-evaluated note.
"""

from __future__ import annotations

import json

import pytest

from careagents import agent, imessage, labs_timeline
from careagents.agent import _execute_tool

_V3 = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
UCUM = "http://unitsofmeasure.org"


@pytest.mark.parametrize("raw,want", [
    ("2026-03-01T10:00:00Z", "2026-03-01"),
    ("2026-03-01", "2026-03-01"),
    ("JaneDoeXYZ-but-not-a-date", ""),
    ("2026-02-30T00:00:00Z", ""),
    ("20260301", ""),
    ("", ""),
])
def test_the_web_chart_dates_are_parsed_not_cut(raw, want):
    assert labs_timeline._date_of({"effectiveDateTime": raw}) == want


def test_an_unparseable_date_is_not_charted_as_a_date():
    bundle = {"entry": [{"resource": {
        "resourceType": "Observation",
        "code": {"coding": [{"system": "http://loinc.org", "code": "2093-3"}]},
        "effectiveDateTime": "Jane Doe", "valueQuantity": {"value": 200}}}]}
    [series] = labs_timeline.build_series(bundle)
    assert series["readings"][0]["date"] == ""


@pytest.mark.parametrize("code,want", [
    ("mg/dL", "mg/dL"), ("mmol/L", "mmol/L"), ("umol/L", "umol/L"),
    ("%", "%"), ("g/dL", "g/dL"), ("mm[Hg]", "mm[Hg]"),
    ("mL/min/{1.73_m2}", "mL/min/{1.73_m2}"), ("mmol/mol", "mmol/mol"),
    # #884 QA F1: a unit the reading stated but we do not recognise is not
    # swapped for the analyte's usual one. It used to read "mg/dL".
    ("JaneDoe", None), ("mg/dL2", None),
    # Nothing stated: the usual unit.
    ("", "mg/dL"),
])
def test_a_ucum_code_must_be_a_known_unit(code, want):
    reading = {"code": code, "system": UCUM}
    assert agent._coded_unit(reading, {"key": "ldl"}) == want


def test_a_known_token_is_recognised_in_any_system_or_the_unit_string():
    """#884 QA F1: an exact allowlist token is the reading's own unit
    whatever system it names, and in `unit` as well as `code`."""
    assert agent._coded_unit({"code": "mmol/L", "system": "local"},
                             {"key": "ldl"}) == "mmol/L"
    assert agent._coded_unit({"unit": "µmol/L"},
                             {"key": "creatinine"}) == "µmol/L"
    assert agent._coded_unit({"unit": "MG/DL"}, {"key": "ldl"}) is None
    assert agent._coded_unit({}, {"key": "ldl"}) == "mg/dL"


def _obs(code, flag, date="2026-03-01"):
    return {"resource": {
        "resourceType": "Observation",
        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
        "effectiveDateTime": date,
        "valueQuantity": {"value": 1, "code": "mg/dL", "system": UCUM},
        "interpretation": [{"coding": [{"system": _V3, "code": flag}]}]}}


def _get_labs(lines, entries, consumer_extra=None):
    class _HC:
        def interpret_labs(self, _t):
            return {"summary": {}, "disclaimer": "d",
                    "consumer": {"lines": lines, **(consumer_extra or {})},
                    "bundle": {"entry": entries}}
    return json.loads(_execute_tool(_HC(), "t", "get_labs", {}, []))


def test_a_line_whose_analyte_does_not_match_its_observation_falls_back():
    # Same flag, wrong analyte: the pairing cannot be trusted.
    out = _get_labs([{"analyte": "Hemoglobin A1c", "flag": "H",
                      "message": "m"}], [_obs("2093-3", "H")])
    assert "latest" not in out["consumer_summary"]
    assert out["consumer_summary"]["lines"][0]["analyte"] == "Hemoglobin A1c"


def test_a_line_matching_its_observation_is_paired():
    out = _get_labs([{"analyte": "Total cholesterol", "flag": "H",
                      "message": "m"}], [_obs("2093-3", "H")])
    assert out["consumer_summary"]["latest"][0]["analyte"] == (
        "Total cholesterol")


def test_an_observation_code_the_table_does_not_know_falls_back():
    out = _get_labs([{"analyte": "Mystery", "flag": "N", "message": "m"}],
                    [_obs("99999-9", "N")])
    assert "latest" not in out["consumer_summary"]


def test_the_label_table_mirrors_the_engine():
    from r6.labs.interpret import LOINC_RANGES
    assert agent.ENGINE_ANALYTE_LABELS == {
        code: spec["name"] for code, spec in LOINC_RANGES.items()}


def test_the_not_evaluated_note_names_the_latest_readings():
    out = _get_labs([{"analyte": "Total cholesterol", "flag": "H",
                      "message": "m"}], [_obs("2093-3", "H")],
                    {"unevaluated": "unknown-analyte", "unevaluated_count": 1,
                     "unevaluated_note": "x"})
    assert "Report the latest readings you were given" in out["note"]
    assert "Report the lines" not in out["note"]


def test_percent_encoded_markers_are_stripped_but_plain_text_is_kept():
    text = "See https://hc.example/r6/sdc/%64ocuments/d?%73ig=1 now."
    assert imessage.strip_signed_urls(text) == "See now."
    assert imessage.strip_signed_urls("50% of 100%") == "50% of 100%"
