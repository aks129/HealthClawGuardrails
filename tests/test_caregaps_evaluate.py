"""Care-gaps engine — preventive-care reminders from a patient's own record.

Decision support, not a directive. Every rule cites a guideline source (test-
enforced). 'Due' means no satisfying record was found in the CONNECTED data —
never an assertion that the screening wasn't done elsewhere.
"""

from r6.caregaps.evaluate import CARE_GAP_RULES, REFERENCES, evaluate_care_gaps


def _patient(gender="female", birth="1968-05-01"):
    return {"resourceType": "Patient", "gender": gender, "birthDate": birth}


def _obs(loinc, date):
    return {"resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
            "effectiveDateTime": date}


def test_every_rule_has_a_cited_source():
    for r in CARE_GAP_RULES:
        assert r.get("source") in REFERENCES, r.get("id")


def test_core_rules_present():
    ids = {r["id"] for r in CARE_GAP_RULES}
    for expected in ("bp-screening", "colorectal-screening", "cervical-screening",
                     "mammography", "flu-immunization"):
        assert expected in ids


def test_bp_screening_due_when_no_recent_bp():
    # 57yo, no BP observation on record -> BP screening is due
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(), observations=[], as_of="2026-07-01")}
    bp = res["bp-screening"]
    assert bp["applicable"] and bp["status"] == "due"
    assert bp["last_done"] is None


def test_bp_screening_up_to_date_with_recent_reading():
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(), observations=[_obs("8480-6", "2026-03-01")], as_of="2026-07-01")}
    assert res["bp-screening"]["status"] == "up_to_date"
    assert res["bp-screening"]["last_done"] == "2026-03-01"


def test_sex_specific_rules_not_applicable_to_wrong_sex():
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(gender="male"), as_of="2026-07-01")}
    assert res["mammography"]["status"] == "not_applicable"
    assert res["cervical-screening"]["status"] == "not_applicable"


def test_age_bounds_make_a_rule_not_applicable():
    # 30yo woman: mammography (40+) not applicable; cervical (21-65) applicable
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(birth="1996-01-01"), as_of="2026-07-01")}
    assert res["mammography"]["status"] == "not_applicable"
    assert res["cervical-screening"]["applicable"] is True


def test_unknown_age_is_indeterminate_not_a_false_alarm():
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        {"resourceType": "Patient", "gender": "female"}, as_of="2026-07-01")}
    # No birthDate -> we must not assert "due"; age-gated rules are indeterminate
    assert res["mammography"]["status"] == "indeterminate"


def test_colorectal_satisfied_by_recent_procedure():
    proc = {"resourceType": "Procedure", "status": "completed",
            "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt",
                                 "code": "45378"}]},
            "performedDateTime": "2020-06-01"}
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(birth="1968-05-01"), procedures=[proc], as_of="2026-07-01")}
    assert res["colorectal-screening"]["status"] == "up_to_date"


def test_year_only_birthdate_still_yields_age():
    # FHIR partial dates are legal — and HealthClaw's own redaction truncates
    # birthDate to the year. A ~60yo must not come back all-indeterminate.
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(birth="1966"), as_of="2026-07-01")}
    assert res["bp-screening"]["applicable"] is True
    assert res["mammography"]["applicable"] is True


def test_year_month_birthdate_still_yields_age():
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(birth="1966-05"), as_of="2026-07-01")}
    assert res["colorectal-screening"]["applicable"] is True
