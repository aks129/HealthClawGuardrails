"""Series building for the chat's lab-timeline card.

Reported live 2026-08-04: "give me a timeline of my cholesterol results" made
eight tool calls and then errored. Prose was the wrong shape for the question.

The series carries clinical numbers into a chart, so what these pin is not
"does it group" but the honesty properties: no invented values, no invented
direction, no invented absence, and no second opinion on a reference range.
"""
from __future__ import annotations

import pathlib
import re

from careagents.labs_timeline import ANALYTES, build_series, keys_for_topic

LOINC = "http://loinc.org"
TEMPLATE = (pathlib.Path(__file__).resolve().parent.parent
            / "templates" / "mcp_apps" / "lab_trends.html")


def _obs(code, value, date="2026-01-01", unit="mg/dL", flag="N", **kw):
    res = {"resourceType": "Observation",
           "code": {"coding": [{"system": LOINC, "code": code}]},
           "effectiveDateTime": date}
    if value is not None:
        res["valueQuantity"] = {"value": value, "unit": unit}
    if flag:
        res["interpretation"] = [{"coding": [{"code": flag}]}]
    res.update(kw)
    return {"resource": res}


def _bundle(*entries):
    return {"resourceType": "Bundle", "entry": list(entries)}


# --- grouping ------------------------------------------------------------

def test_readings_are_grouped_by_analyte_oldest_first():
    series = build_series(_bundle(
        _obs("2093-3", 244, "2026-03-01"),
        _obs("2093-3", 210, "2025-01-01"),
        _obs("2085-9", 48, "2025-06-01")))
    names = [s["name"] for s in series]
    assert names == ["Total cholesterol", "HDL cholesterol"]
    assert [r["date"] for r in series[0]["readings"]] == \
        ["2025-01-01", "2026-03-01"]


def test_an_analyte_is_a_code_set():
    """MUTATION: match only the first LDL code -> red.

    The same test arrives under different LOINCs from different labs; matching
    one draws a confident line through part of the data (#343, one level up).
    """
    series = build_series(_bundle(
        _obs("13457-7", 130, "2025-01-01"),
        _obs("18262-6", 118, "2026-01-01")))
    assert len(series) == 1
    assert len(series[0]["readings"]) == 2


def test_an_analyte_with_no_readings_is_omitted():
    series = build_series(_bundle(_obs("2093-3", 200)))
    assert [s["key"] for s in series] == ["total-cholesterol"]


# --- honesty -------------------------------------------------------------

def test_a_reading_without_a_number_is_skipped_never_zeroed():
    """MUTATION: default a missing value to 0 -> red.

    A 0 plotted on a cholesterol chart is a clinical claim nobody made.
    """
    series = build_series(_bundle(
        _obs("2093-3", None, "2025-01-01"),
        _obs("2093-3", 244, "2026-01-01")))
    assert [r["value"] for r in series[0]["readings"]] == [244]


def test_a_boolean_is_not_mistaken_for_a_number():
    """bool is a subclass of int in Python — True would plot as 1."""
    series = build_series(_bundle(_obs("2093-3", True, "2025-01-01")))
    assert series == []


def test_one_reading_is_not_plottable_as_a_trend():
    """MUTATION: always report trend_plottable -> red. One point has no
    direction, and the surface must not draw a line through it."""
    series = build_series(_bundle(_obs("2093-3", 244)))
    assert series[0]["trend_plottable"] is False


def test_two_dated_readings_are_plottable():
    series = build_series(_bundle(
        _obs("2093-3", 244, "2025-01-01"), _obs("2093-3", 210, "2026-01-01")))
    assert series[0]["trend_plottable"] is True


def test_undated_readings_are_kept_but_do_not_make_a_trend():
    """A reading we cannot place in time is still a reading. It must not be
    dropped (that would report absence), nor counted toward a trend line."""
    series = build_series(_bundle(
        _obs("2093-3", 244, ""), _obs("2093-3", 210, "")))
    assert len(series[0]["readings"]) == 2
    assert series[0]["trend_plottable"] is False


def test_the_engines_flag_is_carried_through_never_recomputed():
    """MUTATION: derive the flag from the value here -> red.

    A second opinion on a reference range is how a patient sees a different
    verdict from the one we audited.
    """
    series = build_series(_bundle(_obs("2093-3", 244, flag="HH")))
    assert series[0]["readings"][0]["flag"] == "HH"


def test_a_reading_with_no_interpretation_is_indeterminate_not_normal():
    """MUTATION: default the flag to N -> red. 'The engine did not judge
    this' must never render as 'this is fine'."""
    series = build_series(_bundle(_obs("2093-3", 244, flag=None)))
    assert series[0]["readings"][0]["flag"] == "IND"


# --- topic narrowing -----------------------------------------------------

def test_a_cholesterol_question_narrows_to_the_lipid_panel():
    keys = keys_for_topic("how has my cholesterol changed?")
    assert set(keys) == {"total-cholesterol", "ldl", "hdl", "triglycerides"}
    assert "a1c" not in keys


def test_no_topic_means_everything_but_an_unmatched_topic_means_nothing():
    """MUTATION: return None for an unmatched topic -> red.

    None ("show what there is") and [] ("nothing matched") are different
    answers. Collapsing them means asking about one test silently dumps every
    series the person has.
    """
    assert keys_for_topic("") is None
    assert keys_for_topic(None) is None
    assert keys_for_topic("my knee") == []

    everything = build_series(_bundle(_obs("2093-3", 200), _obs("4548-4", 6.1)),
                              keys_for_topic(""))
    assert len(everything) == 2
    assert build_series(_bundle(_obs("2093-3", 200)),
                        keys_for_topic("my knee")) == []


# --- the duplication we cannot remove ------------------------------------

def test_the_analyte_table_matches_the_mcp_app():
    """MUTATION: add a code on either side only -> red.

    templates/mcp_apps/lab_trends.html groups the same way in JavaScript
    because it talks to the engine straight from a browser and cannot import
    this module. Two implementations of one concept drift; a constant we
    cannot share in code is at least one we refuse to let diverge.
    """
    src = TEMPLATE.read_text(encoding="utf-8")
    block = re.search(r"var PANELS = \[(.*?)\];", src, re.S)
    assert block, "PANELS not found in the MCP App"
    js = {}
    for name, codes in re.findall(
            r'name:\s*"([^"]+)",\s*codes:\s*\[([^\]]*)\]', block.group(1)):
        js[name] = {c.strip().strip('"') for c in codes.split(",") if c.strip()}
    py = {a["name"]: set(a["codes"]) for a in ANALYTES}
    assert js == py, (
        "the chat card and the MCP App disagree about which LOINC codes make "
        "up an analyte; one surface will silently plot fewer readings")
