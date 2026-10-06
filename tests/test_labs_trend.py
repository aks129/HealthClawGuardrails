# tests/test_labs_trend.py
"""KDIGO creatinine trend check (#62, creatinine only) and the triglyceride
unit pin (#54). Synthetic data only.

KDIGO 2012 has two SEPARATE criteria, evaluated independently:
  A  rise >= 0.3 mg/dL within 48 hours
  B  rise to >= 1.5 x baseline within the prior 7 days
Our physician advisor's case — 0.8 -> 1.3 mg/dL over six days — fires B only,
and is the reason a 48h-only rule is not enough for outpatient draws.
"""
import json
from datetime import datetime, timedelta, timezone

from r6.labs.interpret import REFERENCES, UNIT_MISMATCH, interpret_observation
from r6.labs.report import build_consumer_summary
from r6.labs import trend
from r6.labs.trend import CREATININE_LOINC, kdigo_consumer_line

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def evaluate_creatinine_aki(observations):
    """The engine, with one assertion added to every call in this file:
    the result must be strict JSON. A bare `Infinity` or `NaN` token is not
    JSON, and an inf ratio once raised a false stage-3 alert."""
    result = trend.evaluate_creatinine_aki(observations)
    json.dumps(result, allow_nan=False)
    return result


def _cr(value, at, unit="mg/dL", rid=None, loinc=CREATININE_LOINC, **extra):
    obs = {"resourceType": "Observation", "status": "final",
           "id": rid or f"cr-{at.isoformat()}",
           "code": {"coding": [{"system": "http://loinc.org", "code": loinc,
                                "display": "Jane Doe creatinine"}]},
           "subject": {"reference": "Patient/p1"},
           "effectiveDateTime": at.isoformat(),
           "valueQuantity": {"value": value, "unit": unit}}
    obs.update(extra)
    return obs


def _criteria(result):
    return result["kdigo_criterion"]


# --- the eight required cases ---------------------------------------------

def test_advisor_case_six_days_fires_b_stage_1():
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6))])
    assert _criteria(r) == ["B"]
    assert r["stage"] == 1
    assert r["criteria"]["A"]["status"] == "not-evaluable"
    b = r["criteria"]["B"]
    assert b["status"] == "met" and b["ratio"] == 1.625
    assert b["baseline"]["value_mg_dl"] == 0.8
    assert "lowest comparable creatinine in the 7 days before" in r["note"]


def test_rise_in_40_hours_fires_a_only():
    r = evaluate_creatinine_aki([_cr(0.9, T0), _cr(1.25, T0 + timedelta(hours=40))])
    assert _criteria(r) == ["A"]
    assert r["stage"] == 1
    assert r["criteria"]["B"]["status"] == "not-met"  # 1.39x


def test_rise_over_30_days_fires_neither():
    r = evaluate_creatinine_aki([_cr(1.0, T0), _cr(1.4, T0 + timedelta(days=30))])
    assert _criteria(r) == []
    assert r["stage"] is None
    # No prior inside either window: that is "could not evaluate", never
    # "kidneys fine".
    assert r["criteria"]["A"]["status"] == "not-evaluable"
    assert r["criteria"]["B"]["status"] == "not-evaluable"
    assert r["criteria"]["B"]["reason"] == "no-prior-in-window"


def test_umol_per_litre_converts_by_88_4():
    # 70.72 umol/L == 0.8 mg/dL; same trajectory as the advisor case.
    r = evaluate_creatinine_aki([_cr(70.72, T0, unit="umol/L"),
                                 _cr(1.3, T0 + timedelta(days=6))])
    assert _criteria(r) == ["B"]
    assert r["criteria"]["B"]["baseline"]["value_mg_dl"] == 0.8
    assert r["criteria"]["B"]["baseline"]["original_unit"] == "umol/L"


def test_micro_sign_and_ucum_code_both_accepted():
    obs = _cr(70.72, T0, unit="µmol/L")
    obs["valueQuantity"].update(system="http://unitsofmeasure.org", code="umol/L")
    r = evaluate_creatinine_aki([obs, _cr(1.3, T0 + timedelta(days=6))])
    assert _criteria(r) == ["B"]


def test_latest_with_comparator_abstains():
    latest = _cr(1.3, T0 + timedelta(days=6))
    latest["valueQuantity"]["comparator"] = ">"
    r = evaluate_creatinine_aki([_cr(0.8, T0), latest])
    assert r["status"] == "abstained"
    assert r["abstained_reason"] == "latest-not-comparable"
    assert _criteria(r) == [] and r["stage"] is None
    assert {"id": latest["id"], "reason": "censored-value"} in r["skipped"]


def test_censored_baseline_is_not_used_as_a_point():
    # "<0.5" must not become 0.5 and manufacture a 2.6x rise.
    base = _cr(0.5, T0)
    base["valueQuantity"]["comparator"] = "<"
    r = evaluate_creatinine_aki([base, _cr(1.3, T0 + timedelta(days=6))])
    assert _criteria(r) == []
    assert r["status"] == "abstained"
    assert r["abstained_reason"] == "insufficient-comparable-results"


def test_both_criteria_fire_and_are_both_reported():
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(hours=30))])
    assert _criteria(r) == ["A", "B"]
    assert r["criteria"]["A"]["status"] == "met"
    assert r["criteria"]["B"]["status"] == "met"
    assert r["stage"] == 1


def test_exactly_seven_days_is_inside_the_window():
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=7))])
    assert _criteria(r) == ["B"]


def test_just_over_seven_days_is_outside_the_window():
    r = evaluate_creatinine_aki([_cr(0.8, T0),
                                 _cr(1.3, T0 + timedelta(days=7, seconds=1))])
    assert _criteria(r) == []


def test_exactly_one_and_a_half_times_fires():
    # 1.2 / 0.8 is 1.4999999999999998 in binary floating point.
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.2, T0 + timedelta(days=5))])
    assert _criteria(r) == ["B"]
    assert r["criteria"]["B"]["ratio"] == 1.5


def test_just_under_one_and_a_half_times_does_not_fire():
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.19, T0 + timedelta(days=5))])
    assert _criteria(r) == []
    assert r["criteria"]["B"]["status"] == "not-met"


# --- boundaries and staging -------------------------------------------------

def test_exactly_0_3_rise_in_48_hours_fires_a():
    # 1.2 - 0.9 is 0.29999999999999993 in binary floating point.
    r = evaluate_creatinine_aki([_cr(0.9, T0), _cr(1.2, T0 + timedelta(hours=48))])
    assert _criteria(r) == ["A"]


def test_rise_just_after_48_hours_is_not_criterion_a():
    r = evaluate_creatinine_aki([_cr(0.9, T0),
                                 _cr(1.2, T0 + timedelta(hours=48, seconds=1))])
    assert "A" not in _criteria(r)


def test_stage_2_and_3_by_ratio():
    r2 = evaluate_creatinine_aki([_cr(0.6, T0), _cr(1.2, T0 + timedelta(days=4))])
    assert r2["stage"] == 2
    r2b = evaluate_creatinine_aki([_cr(0.6, T0), _cr(1.79, T0 + timedelta(days=4))])
    assert r2b["stage"] == 2
    r3 = evaluate_creatinine_aki([_cr(0.6, T0), _cr(1.8, T0 + timedelta(days=4))])
    assert r3["stage"] == 3


def test_stage_3_by_absolute_value_with_acute_rise():
    # 3.0 -> 4.1 is 1.37x (B not met) but A fires and the value is >= 4.0.
    r = evaluate_creatinine_aki([_cr(3.0, T0), _cr(4.1, T0 + timedelta(hours=24))])
    assert _criteria(r) == ["A"]
    assert r["stage"] == 3


def test_baseline_is_lowest_in_the_seven_days():
    obs = [_cr(1.0, T0), _cr(0.8, T0 + timedelta(days=2)),
           _cr(0.9, T0 + timedelta(days=4)), _cr(1.3, T0 + timedelta(days=6))]
    r = evaluate_creatinine_aki(obs)
    assert r["criteria"]["B"]["baseline"]["value_mg_dl"] == 0.8


def test_a_rise_already_recovering_is_judged_at_the_latest_result():
    # End-point design, flagged for our physician advisor: only the latest
    # result is the end point, so a peak that has come back down is B not-met.
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.5, T0 + timedelta(days=2)),
                                 _cr(1.0, T0 + timedelta(days=5))])
    assert r["kdigo_criterion"] == []
    assert r["criteria"]["B"]["status"] == "not-met"


def test_falling_creatinine_fires_nothing():
    r = evaluate_creatinine_aki([_cr(1.3, T0), _cr(0.8, T0 + timedelta(days=6))])
    assert _criteria(r) == []


# --- comparability gates ------------------------------------------------------

def test_data_absent_reason_is_skipped():
    gone = _cr(0.8, T0, dataAbsentReason={"coding": [{"code": "error"}]})
    r = evaluate_creatinine_aki([gone, _cr(1.3, T0 + timedelta(days=6))])
    assert {"id": gone["id"], "reason": "data-absent"} in r["skipped"]
    assert _criteria(r) == []


def test_missing_or_date_only_time_is_skipped():
    no_time = _cr(0.8, T0, rid="no-time")
    del no_time["effectiveDateTime"]
    date_only = _cr(0.7, T0, rid="date-only")
    date_only["effectiveDateTime"] = "2026-09-01"
    naive = _cr(0.6, T0, rid="naive")
    naive["effectiveDateTime"] = "2026-09-01T08:00:00"
    r = evaluate_creatinine_aki([no_time, date_only, naive,
                                 _cr(1.3, T0 + timedelta(days=6))])
    reasons = {s["id"]: s["reason"] for s in r["skipped"]}
    assert reasons == {"no-time": "ambiguous-time", "date-only": "ambiguous-time",
                       "naive": "ambiguous-time"}
    assert _criteria(r) == []


def test_unknown_unit_is_skipped():
    odd = _cr(0.8, T0, unit="mg/L")
    r = evaluate_creatinine_aki([odd, _cr(1.3, T0 + timedelta(days=6))])
    assert {"id": odd["id"], "reason": "unit-not-comparable"} in r["skipped"]
    assert _criteria(r) == []


def test_whole_blood_creatinine_is_not_mixed_with_serum():
    wb = _cr(0.8, T0, loinc="38483-4")
    r = evaluate_creatinine_aki([wb, _cr(1.3, T0 + timedelta(days=6))])
    assert {"id": wb["id"], "reason": "not-serum-creatinine"} in r["skipped"]
    assert _criteria(r) == []


def test_entered_in_error_is_skipped():
    bad = _cr(0.8, T0, status="entered-in-error")
    r = evaluate_creatinine_aki([bad, _cr(1.3, T0 + timedelta(days=6))])
    assert _criteria(r) == []


def test_single_result_abstains():
    r = evaluate_creatinine_aki([_cr(1.3, T0)])
    assert r["status"] == "abstained"
    assert r["abstained_reason"] == "insufficient-comparable-results"


def test_no_creatinine_at_all_returns_none():
    other = _cr(4.2, T0, loinc="2823-3", unit="mmol/L")
    assert evaluate_creatinine_aki([other]) is None


def test_result_carries_no_upstream_display():
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6))])
    assert "Jane Doe" not in json.dumps(r)
    assert r["analyte"] == "Creatinine"


def test_kdigo_is_cited_in_references():
    assert "KDIGO" in REFERENCES["kdigo-2012"]
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6))])
    assert r["source"] == "kdigo-2012"


# --- consumer wording ---------------------------------------------------------

RECENT = T0 + timedelta(days=8)  # "now" for a result that is not stale


def test_consumer_line_is_plain_and_calm_and_dated():
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6))])
    line = kdigo_consumer_line(r, now=RECENT)
    assert line["message"] == (
        "Between Sep 1 and Sep 7, 2026, your creatinine rose from 0.8 to "
        "1.3 mg/dL in 6 days. A rise like this can mean the kidneys are under "
        "strain. Contact your clinician promptly.")
    assert "AKI" not in line["message"] and "injury" not in line["message"]


def test_consumer_line_across_a_year_boundary():
    t = datetime(2025, 12, 30, 8, 0, tzinfo=timezone.utc)
    r = evaluate_creatinine_aki([_cr(0.8, t), _cr(1.3, t + timedelta(days=4))])
    msg = kdigo_consumer_line(r, now=t + timedelta(days=5))["message"]
    assert msg.startswith("Between Dec 30, 2025 and Jan 3, 2026, your creatinine")


def test_consumer_line_uses_hours_under_two_days():
    r = evaluate_creatinine_aki([_cr(0.9, T0), _cr(1.25, T0 + timedelta(hours=40))])
    msg = kdigo_consumer_line(r, now=RECENT)["message"]
    assert "from 0.9 to 1.25 mg/dL in 40 hours" in msg


def test_consumer_line_under_an_hour_on_one_day():
    r = evaluate_creatinine_aki([_cr(0.9, T0), _cr(1.2, T0 + timedelta(minutes=30))])
    msg = kdigo_consumer_line(r, now=RECENT)["message"]
    assert msg.startswith("On Sep 1, 2026, your creatinine rose from 0.9 to "
                          "1.2 mg/dL within an hour.")


def test_stale_result_softens_the_consumer_line_but_keeps_the_flag():
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6))])
    assert r["kdigo_criterion"] == ["B"] and r["stage"] == 1
    msg = kdigo_consumer_line(
        r, now=T0 + timedelta(days=6 + 30, seconds=1))["message"]
    assert msg == (
        "In Sep 2026, your creatinine rose from 0.8 to 1.3 mg/dL in 6 days. "
        "If you haven't already, ask your clinician whether this was "
        "followed up.")
    assert "promptly" not in msg


def test_exactly_30_days_old_is_not_yet_stale():
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6))])
    msg = kdigo_consumer_line(r, now=T0 + timedelta(days=36))["message"]
    assert msg.endswith("Contact your clinician promptly.")


def test_consumer_line_quotes_the_labs_own_umol_numbers():
    r = evaluate_creatinine_aki([_cr(80, T0, unit="umol/L"),
                                 _cr(133, T0 + timedelta(days=6), unit="umol/L")])
    msg = kdigo_consumer_line(r, now=RECENT)["message"]
    assert "rose from 80 to 133 µmol/L in 6 days" in msg
    assert "mg/dL" not in msg


def test_mixed_units_are_quoted_in_the_latest_results_unit():
    r = evaluate_creatinine_aki([_cr(0.8, T0),
                                 _cr(133, T0 + timedelta(days=6), unit="umol/L")])
    msg = kdigo_consumer_line(r, now=RECENT)["message"]
    assert "rose from 70.7 to 133 µmol/L" in msg


def test_no_consumer_line_when_nothing_fired():
    r = evaluate_creatinine_aki([_cr(1.0, T0), _cr(1.4, T0 + timedelta(days=30))])
    assert kdigo_consumer_line(r) is None
    assert kdigo_consumer_line(None) is None


def test_consumer_summary_carries_trends_separately_from_lines():
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6))])
    out = build_consumer_summary([], trends=[r])
    assert out["lines"] == []
    assert "your creatinine rose" in out["trends"][0]["message"]


def test_consumer_summary_without_trends_is_unchanged():
    assert "trends" not in build_consumer_summary([])


def test_unevaluated_note_does_not_contradict_the_trend():
    # A umol/L result is a unit mismatch for the range check, but the trend
    # converts it. The note must not read as "creatinine was not looked at".
    latest = _cr(133, T0 + timedelta(days=6), unit="umol/L")
    r = evaluate_creatinine_aki([_cr(80, T0, unit="umol/L"), latest])
    out = build_consumer_summary([interpret_observation(latest)], trends=[r])
    assert out["unevaluated_analytes"] == ["Creatinine"]
    assert ("Creatinine was still compared with its own earlier results"
            in out["unevaluated_note"])


def test_unevaluated_note_unchanged_when_the_trend_did_not_fire():
    latest = _cr(133, T0 + timedelta(days=60), unit="umol/L")
    r = evaluate_creatinine_aki([_cr(80, T0, unit="umol/L"), latest])
    out = build_consumer_summary([interpret_observation(latest)], trends=[r])
    assert "compared with its own earlier" not in out["unevaluated_note"]


# --- F1: malformed shapes never raise -----------------------------------------

def test_status_as_a_list_is_skipped_not_raised():
    r = evaluate_creatinine_aki([_cr(0.8, T0),
                                 _cr(1.3, T0 + timedelta(days=2), status=["final"])])
    assert r["status"] == "abstained"
    assert r["abstained_reason"] == "latest-not-comparable"
    assert "malformed-field" in {s["reason"] for s in r["skipped"]}


def _bare(**fields):
    obs = {"resourceType": "Observation", "id": "m",
           "code": {"coding": [{"system": "http://loinc.org",
                                "code": CREATININE_LOINC}]},
           "effectiveDateTime": T0.isoformat(),
           "valueQuantity": {"value": 1.0, "unit": "mg/dL"}}
    obs.update(fields)
    return obs


def test_malformed_shapes_never_raise():
    shapes = [
        {"code": None}, {"code": "2160-0"}, {"code": {"coding": None}},
        {"code": {"coding": [None, "x"]}},
        {"code": {"coding": [{"system": "http://loinc.org", "code": ["2160-0"]}]}},
        {"valueQuantity": None}, {"valueQuantity": [1.0]},
        {"valueQuantity": {"value": [1.0], "unit": "mg/dL"}},
        {"valueQuantity": {"value": 1.0, "unit": ["mg/dL"]}},
        {"valueQuantity": {"value": 1.0, "unit": "mg/dL",
                           "system": "http://unitsofmeasure.org", "code": {}}},
        {"effectiveDateTime": 20260901}, {"effectiveDateTime": ["2026-09-01"]},
        {"effectiveDateTime": "not a date"}, {"status": {"a": 1}},
        {"id": ["x"]},
    ]
    for shape in shapes:
        r = evaluate_creatinine_aki([_bare(**shape),
                                     _cr(1.3, T0 + timedelta(days=2))])
        json.dumps(r, allow_nan=False)
    assert evaluate_creatinine_aki(["junk", None, 3]) is None


# --- F2c: a newer draw we cannot place -----------------------------------------

def test_newer_date_only_result_abstains():
    newer = _cr(0.8, T0, rid="newer")
    newer["effectiveDateTime"] = "2026-09-20"
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6)),
                                 newer])
    assert r["status"] == "abstained"
    assert r["abstained_reason"] == "newer-result-unusable"
    assert r["kdigo_criterion"] == [] and kdigo_consumer_line(r) is None


def test_newer_naive_time_result_abstains():
    newer = _cr(0.8, T0, rid="newer")
    newer["effectiveDateTime"] = "2026-09-08T08:00:00"
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6)),
                                 newer])
    assert r["abstained_reason"] == "newer-result-unusable"


def test_newer_year_month_result_abstains():
    newer = _cr(0.8, T0, rid="newer")
    newer["effectiveDateTime"] = "2026-10"
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6)),
                                 newer])
    assert r["abstained_reason"] == "newer-result-unusable"


def test_date_only_result_on_the_same_day_does_not_abstain():
    same = _cr(0.8, T0, rid="same")
    same["effectiveDateTime"] = "2026-09-07"
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6)),
                                 same])
    assert r["kdigo_criterion"] == ["B"]


# --- F4: tolerance compares, not rounding --------------------------------------

def test_ratio_just_under_one_and_a_half_does_not_round_up():
    # 1.999 / 1.333 = 1.49962...; rounding to 3 places would call it 1.5.
    r = evaluate_creatinine_aki([_cr(1.333, T0), _cr(1.999, T0 + timedelta(days=5))])
    assert "B" not in r["kdigo_criterion"]
    # ...and the reported ratio must not read as on the threshold either.
    assert r["criteria"]["B"]["ratio"] < 1.5


def test_umol_pair_compares_against_26_5_umol_per_litre():
    # KDIGO 2012 (Recommendation 2.1.1, Table 2) writes criterion A as
    # ">=0.3 mg/dL (>=26.5 umol/L)". A pair reported in umol/L is held to
    # the umol/L figure, so 88 -> 114.5 (exactly 26.5) fires even though it
    # is 0.2998 mg/dL after conversion.
    r = evaluate_creatinine_aki([_cr(88, T0, unit="umol/L"),
                                 _cr(114.5, T0 + timedelta(hours=24), unit="umol/L")])
    assert "A" in r["kdigo_criterion"]
    assert r["criteria"]["A"]["compared_in"] == "umol/L"


def test_umol_pair_just_under_26_5_does_not_fire_a():
    r = evaluate_creatinine_aki([_cr(88, T0, unit="umol/L"),
                                 _cr(114.4, T0 + timedelta(hours=24), unit="umol/L")])
    assert "A" not in r["kdigo_criterion"]


def test_mixed_unit_pair_compares_in_mg_dl():
    # 0.8 mg/dL -> 97.2 umol/L is 0.2995 mg/dL: under 0.3, so A does not fire.
    r = evaluate_creatinine_aki([_cr(0.8, T0),
                                 _cr(97.2, T0 + timedelta(hours=24), unit="umol/L")])
    assert "A" not in r["kdigo_criterion"]
    assert r["criteria"]["A"]["compared_in"] == "mg/dL"


def test_stage_3_cutoff_uses_a_tolerance():
    # 1.2 / 0.4 is 2.9999999999999996 in binary floating point.
    r = evaluate_creatinine_aki([_cr(0.4, T0), _cr(1.2, T0 + timedelta(days=3))])
    assert r["stage"] == 3


# --- F5: implausible values --------------------------------------------------------

def test_zero_negative_and_non_finite_values_are_skipped():
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        base = _cr(bad, T0, rid="bad")
        r = evaluate_creatinine_aki([base, _cr(1.3, T0 + timedelta(days=2))])
        assert {"id": "bad", "reason": "implausible-value"} in r["skipped"]
        assert r["kdigo_criterion"] == []
        json.dumps(r, allow_nan=False)


def test_an_integer_too_large_for_a_float_is_implausible_not_raised():
    # JSON allows 10**400; float() of it raises OverflowError.
    r = evaluate_creatinine_aki([_cr(0.8, T0),
                                 _cr(10**400, T0 + timedelta(days=2), rid="huge")])
    assert {"id": "huge", "reason": "implausible-value"} in r["skipped"]
    assert r["status"] == "abstained"
    assert r["abstained_reason"] == "latest-not-comparable"
    json.dumps(r, allow_nan=False)


def test_tiny_positive_baselines_are_implausible_not_raised():
    # 5e-324 umol/L becomes 0.0 mg/dL after conversion (a divide by zero);
    # 5e-324 and 1e-300 mg/dL made an inf or astronomic ratio and a false
    # stage-3 alert reading "rose from 0 to 1.3".
    for value, unit in ((5e-324, "umol/L"), (5e-324, "mg/dL"), (1e-300, "mg/dL")):
        r = evaluate_creatinine_aki([_cr(value, T0, rid="tiny", unit=unit),
                                     _cr(1.3, T0 + timedelta(days=2))])
        assert {"id": "tiny", "reason": "implausible-value"} in r["skipped"]
        assert r["kdigo_criterion"] == [] and r["stage"] is None


def test_lower_bound_is_0_1_mg_dl():
    for value, unit in ((0.1, "mg/dL"), (8.84, "umol/L")):
        r = evaluate_creatinine_aki([_cr(value, T0, unit=unit),
                                     _cr(0.5, T0 + timedelta(days=2))])
        assert r["kdigo_criterion"] == ["A", "B"]
    for value, unit in ((0.0999, "mg/dL"), (8.83, "umol/L")):
        r = evaluate_creatinine_aki([_cr(value, T0, rid="low", unit=unit),
                                     _cr(0.5, T0 + timedelta(days=2))])
        assert {"id": "low", "reason": "implausible-value"} in r["skipped"]


def test_ratio_is_guarded_even_without_the_lower_bound(monkeypatch):
    # The bound is clinical and may be tuned; the division must stay safe
    # if it is ever lowered to nothing.
    monkeypatch.setattr(trend, "MIN_PLAUSIBLE_MG_DL", 0.0)
    for value, unit in ((5e-324, "umol/L"), (5e-324, "mg/dL")):
        r = evaluate_creatinine_aki([_cr(value, T0, unit=unit),
                                     _cr(1.3, T0 + timedelta(days=2))])
        assert r["criteria"]["B"]["status"] == "not-evaluable"
        assert "B" not in r["kdigo_criterion"]


def test_creatinine_above_40_mg_dl_is_implausible():
    for value, unit in ((40.1, "mg/dL"), (3537, "umol/L")):
        r = evaluate_creatinine_aki([_cr(0.8, T0),
                                     _cr(value, T0 + timedelta(days=2), rid="hi",
                                         unit=unit)])
        assert {"id": "hi", "reason": "implausible-value"} in r["skipped"]
    # The bound itself is plausible.
    for value, unit in ((40, "mg/dL"), (3536, "umol/L")):
        r = evaluate_creatinine_aki([_cr(0.8, T0),
                                     _cr(value, T0 + timedelta(days=2), unit=unit)])
        assert r["kdigo_criterion"] == ["A", "B"]


# --- #54: triglycerides -------------------------------------------------------

def test_triglycerides_in_mmol_stay_indeterminate():
    """No mg/dL<->mmol/L conversion exists for triglycerides today (#54), so a
    mmol/L result is a unit mismatch — never converted with the cholesterol
    factor (x0.0259). Whoever builds #54: the triglyceride factor is
    1 mmol/L = 88.57 mg/dL (x0.01129)."""
    obs = {"resourceType": "Observation",
           "code": {"coding": [{"system": "http://loinc.org", "code": "2571-8"}]},
           "valueQuantity": {"value": 1.2, "unit": "mmol/L"}}
    res = interpret_observation(obs)
    assert res["flag"] is None
    assert res["indeterminate_reason"] == UNIT_MISMATCH


# --- route: ?subject= wiring ---------------------------------------------------

def _store(app, tenant_id, obs):
    from r6.models import R6Resource, db
    with app.app_context():
        db.session.add(R6Resource(
            resource_type="Observation", resource_json=json.dumps(obs),
            resource_id=obs["id"], tenant_id=tenant_id))
        db.session.commit()


def _param(body, name):
    return next((p for p in body["parameter"] if p["name"] == name), None)


def test_subject_interpret_reports_the_trend(app, client, tenant_headers, tenant_id):
    # Relative to now, so the consumer line is the not-stale wording.
    t = datetime.now(timezone.utc) - timedelta(days=8)
    _store(app, tenant_id, _cr(0.8, t, rid="cr-a"))
    _store(app, tenant_id, _cr(1.3, t + timedelta(days=6), rid="cr-b"))
    r = client.post("/r6/fhir/Observation/$interpret?subject=Patient/p1",
                    headers=tenant_headers)
    assert r.status_code == 200
    body = r.get_json()
    summary = json.loads(_param(body, "summary")["valueString"])
    assert summary["trends"][0]["kdigo_criterion"] == ["B"]
    assert summary["trends"][0]["stage"] == 1
    consumer = json.loads(_param(body, "consumerSummary")["valueString"])
    msg = consumer["trends"][0]["message"]
    assert "your creatinine rose from 0.8 to 1.3 mg/dL in 6 days." in msg
    assert msg.endswith("Contact your clinician promptly.")
    assert "Jane Doe" not in r.get_data(as_text=True)


def test_subject_interpret_survives_a_list_status(
        app, client, tenant_headers, tenant_id):
    _store(app, tenant_id, _cr(0.8, T0, rid="cr-s1"))
    _store(app, tenant_id, _cr(1.3, T0 + timedelta(days=2), rid="cr-s2",
                               status=["final"]))
    r = client.post("/r6/fhir/Observation/$interpret?subject=Patient/p1",
                    headers=tenant_headers)
    assert r.status_code == 200
    summary = json.loads(_param(r.get_json(), "summary")["valueString"])
    assert summary["trends"][0]["abstained_reason"] == "latest-not-comparable"


def test_subject_interpret_survives_a_huge_integer(
        app, client, tenant_headers, tenant_id):
    _store(app, tenant_id, _cr(0.8, T0, rid="cr-h1"))
    _store(app, tenant_id, _cr(10**400, T0 + timedelta(days=2), rid="cr-h2"))
    r = client.post("/r6/fhir/Observation/$interpret?subject=Patient/p1",
                    headers=tenant_headers)
    assert r.status_code == 200
    summary = json.loads(_param(r.get_json(), "summary")["valueString"])
    assert {"id": "cr-h2", "reason": "implausible-value"} in \
        summary["trends"][0]["skipped"]


def test_subject_interpret_survives_a_subnormal_baseline(
        app, client, tenant_headers, tenant_id):
    _store(app, tenant_id, _cr(5e-324, T0, rid="cr-t1", unit="umol/L"))
    _store(app, tenant_id, _cr(1.3, T0 + timedelta(days=2), rid="cr-t2"))
    r = client.post("/r6/fhir/Observation/$interpret?subject=Patient/p1",
                    headers=tenant_headers)
    assert r.status_code == 200
    assert "Infinity" not in r.get_data(as_text=True)
    summary = json.loads(_param(r.get_json(), "summary")["valueString"])
    assert {"id": "cr-t1", "reason": "implausible-value"} in \
        summary["trends"][0]["skipped"]


def test_no_subject_means_no_trend(app, client, tenant_headers, tenant_id):
    # The stored fallback can span several patients; a trend across them
    # would be arithmetic on two different people.
    _store(app, tenant_id, _cr(0.8, T0, rid="cr-c"))
    _store(app, tenant_id, _cr(1.3, T0 + timedelta(days=6), rid="cr-d"))
    r = client.post("/r6/fhir/Observation/$interpret", headers=tenant_headers)
    summary = json.loads(_param(r.get_json(), "summary")["valueString"])
    assert "trends" not in summary
