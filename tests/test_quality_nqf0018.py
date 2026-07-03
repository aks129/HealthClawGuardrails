"""NQF 0018 / CMS165 — Controlling High Blood Pressure — measure engine.

The measure CONTROL target is <140/90 (office), which is deliberately distinct
from the 130/80 home *diagnostic* threshold used by the SMBP triage engine.
"""

from r6.quality.measures import evaluate_nqf0018, HYPERTENSION_ICD10_PREFIXES


def _patient(birth_date):
    return {"resourceType": "Patient", "id": "p1", "birthDate": birth_date}


def _htn_condition(code="I10", system="http://hl7.org/fhir/sid/icd-10-cm"):
    return {
        "resourceType": "Condition",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [{"system": system, "code": code}]},
        "subject": {"reference": "Patient/p1"},
    }


def _bp(sys_v, dia_v, when):
    return {
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
        "subject": {"reference": "Patient/p1"},
        "effectiveDateTime": when,
        "component": [
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
             "valueQuantity": {"value": sys_v}},
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
             "valueQuantity": {"value": dia_v}},
        ],
    }


PERIOD = ("2026-01-01", "2026-12-31")


def test_controlled_patient_in_numerator():
    # 55yo, hypertension dx, most recent BP 132/82 — above the 130/80 DIAGNOSTIC
    # line but controlled under the 140/90 MEASURE target -> numerator TRUE.
    r = evaluate_nqf0018(_patient("1970-06-01"), [_htn_condition()],
                         [_bp(138, 88, "2026-03-01"), _bp(132, 82, "2026-09-01")],
                         *PERIOD)
    assert r["in_denominator"] is True
    assert r["in_numerator"] is True
    assert r["most_recent_bp"] == {"systolic": 132, "diastolic": 82}
    assert r["control_threshold"] == {"systolic": 140, "diastolic": 90}


def test_uncontrolled_patient_not_in_numerator():
    r = evaluate_nqf0018(_patient("1970-06-01"), [_htn_condition()],
                         [_bp(132, 82, "2026-03-01"), _bp(148, 92, "2026-09-01")],
                         *PERIOD)
    assert r["in_denominator"] is True
    assert r["in_numerator"] is False  # most recent 148/92 is uncontrolled


def test_numerator_needs_both_below_threshold():
    # systolic controlled but diastolic 92 -> uncontrolled
    r = evaluate_nqf0018(_patient("1970-06-01"), [_htn_condition()],
                         [_bp(138, 92, "2026-09-01")], *PERIOD)
    assert r["in_numerator"] is False


def test_age_over_85_excluded_from_denominator():
    r = evaluate_nqf0018(_patient("1935-06-01"), [_htn_condition()],
                         [_bp(120, 78, "2026-09-01")], *PERIOD)
    assert r["age"] >= 86
    assert r["in_initial_population"] is False
    assert r["in_denominator"] is False


def test_age_under_18_excluded():
    r = evaluate_nqf0018(_patient("2012-06-01"), [_htn_condition()],
                         [_bp(120, 78, "2026-09-01")], *PERIOD)
    assert r["in_initial_population"] is False


def test_no_hypertension_diagnosis_not_in_denominator():
    r = evaluate_nqf0018(_patient("1970-06-01"), [],
                         [_bp(120, 78, "2026-09-01")], *PERIOD)
    assert r["in_initial_population"] is True   # right age
    assert r["in_denominator"] is False         # but no htn dx


def test_snomed_hypertension_counts():
    cond = _htn_condition(code="59621000", system="http://snomed.info/sct")
    r = evaluate_nqf0018(_patient("1970-06-01"), [cond],
                         [_bp(120, 78, "2026-09-01")], *PERIOD)
    assert r["in_denominator"] is True


def test_inactive_hypertension_not_counted():
    cond = _htn_condition()
    cond["clinicalStatus"]["coding"][0]["code"] = "resolved"
    r = evaluate_nqf0018(_patient("1970-06-01"), [cond],
                         [_bp(120, 78, "2026-09-01")], *PERIOD)
    assert r["in_denominator"] is False


def test_readings_outside_period_ignored():
    # only reading is before the period -> no qualifying BP -> not controlled
    r = evaluate_nqf0018(_patient("1970-06-01"), [_htn_condition()],
                         [_bp(120, 78, "2025-09-01")], *PERIOD)
    assert r["in_denominator"] is True
    assert r["most_recent_bp"] is None
    assert r["in_numerator"] is False


def test_pregnancy_exclusion():
    preg = {
        "resourceType": "Condition",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "77386006"}]},
        "subject": {"reference": "Patient/p1"},
    }
    r = evaluate_nqf0018(_patient("1990-06-01"), [_htn_condition(), preg],
                         [_bp(120, 78, "2026-09-01")], *PERIOD)
    assert r["denominator_exclusion"] is True
    assert r["in_denominator"] is False


def test_hypertension_icd10_prefixes_present():
    assert "I10" in HYPERTENSION_ICD10_PREFIXES
