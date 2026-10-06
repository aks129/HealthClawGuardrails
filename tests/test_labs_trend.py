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
from r6.labs.trend import (
    CREATININE_LOINC, evaluate_creatinine_aki, kdigo_consumer_line,
)

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


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

def test_consumer_line_is_plain_and_calm():
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6))])
    line = kdigo_consumer_line(r)
    assert line["message"] == (
        "Your creatinine rose from 0.8 to 1.3 mg/dL in 6 days. A rise like "
        "this can mean the kidneys are under strain. Contact your clinician "
        "promptly.")
    assert "AKI" not in line["message"] and "injury" not in line["message"]


def test_consumer_line_uses_hours_under_two_days():
    r = evaluate_creatinine_aki([_cr(0.9, T0), _cr(1.25, T0 + timedelta(hours=40))])
    assert "from 0.9 to 1.25 mg/dL in 40 hours" in kdigo_consumer_line(r)["message"]


def test_no_consumer_line_when_nothing_fired():
    r = evaluate_creatinine_aki([_cr(1.0, T0), _cr(1.4, T0 + timedelta(days=30))])
    assert kdigo_consumer_line(r) is None
    assert kdigo_consumer_line(None) is None


def test_consumer_summary_carries_trends_separately_from_lines():
    r = evaluate_creatinine_aki([_cr(0.8, T0), _cr(1.3, T0 + timedelta(days=6))])
    out = build_consumer_summary([], trends=[r])
    assert out["lines"] == []
    assert out["trends"][0]["message"].startswith("Your creatinine rose")


def test_consumer_summary_without_trends_is_unchanged():
    assert "trends" not in build_consumer_summary([])


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
    _store(app, tenant_id, _cr(0.8, T0, rid="cr-a"))
    _store(app, tenant_id, _cr(1.3, T0 + timedelta(days=6), rid="cr-b"))
    r = client.post("/r6/fhir/Observation/$interpret?subject=Patient/p1",
                    headers=tenant_headers)
    assert r.status_code == 200
    body = r.get_json()
    summary = json.loads(_param(body, "summary")["valueString"])
    assert summary["trends"][0]["kdigo_criterion"] == ["B"]
    assert summary["trends"][0]["stage"] == 1
    consumer = json.loads(_param(body, "consumerSummary")["valueString"])
    assert consumer["trends"][0]["message"].startswith(
        "Your creatinine rose from 0.8 to 1.3 mg/dL in 6 days.")
    assert "Jane Doe" not in r.get_data(as_text=True)


def test_no_subject_means_no_trend(app, client, tenant_headers, tenant_id):
    # The stored fallback can span several patients; a trend across them
    # would be arithmetic on two different people.
    _store(app, tenant_id, _cr(0.8, T0, rid="cr-c"))
    _store(app, tenant_id, _cr(1.3, T0 + timedelta(days=6), rid="cr-d"))
    r = client.post("/r6/fhir/Observation/$interpret", headers=tenant_headers)
    summary = json.loads(_param(r.get_json(), "summary")["valueString"])
    assert "trends" not in summary
