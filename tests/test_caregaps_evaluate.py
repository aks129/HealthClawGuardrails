"""Care-gaps engine — preventive-care reminders from a patient's own record.

Decision support, not a directive. Every rule cites a guideline source (test-
enforced). 'Due' means no satisfying record was found in the CONNECTED data —
never an assertion that the screening wasn't done elsewhere.
"""

from r6.caregaps.evaluate import (
    CARE_GAP_RULES, REFERENCES, _cadence_desc, evaluate_care_gaps)


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


def test_indeterminate_results_name_which_demographic_was_missing():
    """report.py turns this into the words the patient reads, and it must not
    have to guess: one reason covering both demographics is how a person whose
    birthDate was on file got told it was not (#417).

    An indeterminate result with no cause recorded falls back to claiming BOTH
    are missing, so the cause is load-bearing rather than decorative.
    """
    no_dob = {r["rule_id"]: r for r in evaluate_care_gaps(
        {"resourceType": "Patient", "gender": "female"}, as_of="2026-07-01")}
    assert no_dob["mammography"]["indeterminate_reason"] == "birth-date-unknown"

    no_sex = {r["rule_id"]: r for r in evaluate_care_gaps(
        {"resourceType": "Patient", "birthDate": "1968-05-01"}, as_of="2026-07-01")}
    assert no_sex["mammography"]["indeterminate_reason"] == "sex-unknown"
    assert no_sex["bp-screening"]["status"] != "indeterminate"

    for res in (no_dob, no_sex):
        for r in res.values():
            if r["status"] == "indeterminate":
                assert r.get("indeterminate_reason"), r["rule_id"]


def test_colorectal_satisfied_by_recent_procedure():
    proc = {"resourceType": "Procedure", "status": "completed",
            "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt",
                                 "code": "45378"}]},
            "performedDateTime": "2020-06-01"}
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(birth="1968-05-01"), procedures=[proc], as_of="2026-07-01")}
    assert res["colorectal-screening"]["status"] == "up_to_date"


def test_colorectal_with_no_procedure_does_not_claim_the_patient_is_due():
    """#425 — the rule cannot see two of the accepted tests.

    Colorectal screening is satisfied here only by CPT colonoscopy and
    sigmoidoscopy codes, which are Procedures. FIT (annual) and FIT-DNA /
    Cologuard (1-3 yearly) are equally acceptable under USPSTF and arrive as
    lab Observations, so this rule never looks at them.

    "Due" therefore asserted something we had not checked: a patient
    screening exactly as advised with annual FIT matched nothing and was told
    they were overdue. The generic due note ("no record found in your
    connected data") is not enough, because it implies we read the data that
    would have satisfied the rule. We did not read it at all.

    Indeterminate is the honest status while a whole class of qualifying
    evidence is invisible, and the note still tells the patient to raise it —
    so nothing prompting them to act is lost. The status change is the point:
    a red "DUE" badge with fine print underneath is still a claim.

    MUTATION: drop `unread_evidence` from the colorectal rule -> red,
    status back to "due". Ran it, saw red.
    """
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(birth="1968-05-01"), as_of="2026-07-01")}
    gap = res["colorectal-screening"]

    assert gap["status"] == "indeterminate", (
        "claimed a screening gap without reading two of the accepted tests")
    assert gap["applicable"] is True, "the patient is still in the age range"
    assert gap["indeterminate_reason"] == "evidence-not-read"
    note = gap["note"].lower()
    assert "stool" in note, "the note must name what we could not see"
    assert "clinician" in note, "the patient must still be told to raise it"


def test_a_rule_with_no_blind_spot_still_reports_a_real_gap():
    """The disclosure must not become a blanket refusal to answer.

    Mammography reads the evidence that satisfies it, so an absent record
    means absent, and "due" is the honest answer. If this ever goes
    indeterminate too, the guard above has been over-applied and the report
    has stopped being useful.

    MUTATION: return indeterminate for every unsatisfied rule -> red.
    """
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(birth="1968-05-01", gender="female"), as_of="2026-07-01")}
    assert res["mammography"]["status"] == "due"
    assert res["bp-screening"]["status"] == "due"


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


def test_rules_carry_ecqm_crosswalk_where_one_exists():
    # Portability bridge to certified-measure stacks (e.g. CQL Studio ELM-to-SQL):
    # rules map to the related CMS eCQM where a clean mapping exists, None where
    # it doesn't. "Related" — not a claim of eCQM-equivalent logic.
    xwalk = {r["id"]: r.get("related_ecqm") for r in CARE_GAP_RULES}
    assert xwalk["colorectal-screening"] == "CMS130"
    assert xwalk["cervical-screening"] == "CMS124"
    assert xwalk["mammography"] == "CMS125"
    assert xwalk["flu-immunization"] == "CMS147"
    assert xwalk["bp-screening"] == "CMS22"
    assert xwalk["lipid-screening"] is None  # no current screening eCQM — honest None


def test_results_include_ecqm_crosswalk():
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(), as_of="2026-07-01")}
    assert res["mammography"]["related_ecqm"] == "CMS125"
    assert res["lipid-screening"]["related_ecqm"] is None


def test_snomed_coded_diabetes_triggers_a1c_rule():
    # Real-world records (Fasten/Epic pulls) code diabetes in SNOMED, not ICD.
    # Detection matched only ICD prefixes, so SNOMED-coded diabetics had their
    # A1c monitoring gap suppressed as "not applicable" (found in audit 2026-07-08).
    for code in ("73211009", "44054006", "46635009"):  # DM, T2DM, T1DM
        cond = {"resourceType": "Condition",
                "clinicalStatus": {"coding": [{"code": "active"}]},
                "code": {"coding": [{"system": "http://snomed.info/sct",
                                     "code": code}]}}
        res = {r["rule_id"]: r for r in evaluate_care_gaps(
            _patient(), conditions=[cond], as_of="2026-07-01")}
        assert res["diabetes-a1c"]["applicable"] is True, code
        assert res["diabetes-a1c"]["status"] == "due"


def test_future_dated_record_does_not_satisfy_a_gap():
    # A record dated after as_of must not count — bad source data could
    # otherwise produce a false "up to date" (audit finding 2026-07-08).
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(), observations=[_obs("8480-6", "2026-09-15")],
        as_of="2026-07-01")}
    assert res["bp-screening"]["status"] == "due"


# ─────────────────────────────────────────────
# Blood pressure: one cadence for two populations (council D14)
#
# The rule read `cadence_months: 12` from age 18, so someone under 40 with a
# normal reading eighteen months ago was told they were due. USPSTF screens
# adults 18-39 with normal blood pressure every 3-5 years and 40+ annually,
# so the single yearly figure asserted a gap the guideline does not describe.
#
# Encoded as an age band inside the ONE `bp-screening` rule rather than as two
# rules: rule ids and `summary.total` are read by the MCP App page, the brief
# and the eCQM crosswalk, and splitting the rule would have moved all three to
# fix a cadence.
# ─────────────────────────────────────────────

_AS_OF = "2026-07-01"


def _months_before(as_of, months):
    """A first-of-month date exactly `months` before `as_of`.

    Written out rather than hardcoded: `_months_between` counts whole months,
    so an off-by-one here would silently move a case to the other side of a
    cadence boundary and the test would still pass, for the wrong reason.
    """
    total = int(as_of[:4]) * 12 + int(as_of[5:7]) - 1 - months
    return f"{total // 12}-{total % 12 + 1:02d}-01"


def _bp_result(age, months_ago):
    """bp-screening for a patient of `age` whose last reading was that old."""
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(birth=f"{int(_AS_OF[:4]) - age}-01-01"),
        observations=[_obs("8480-6", _months_before(_AS_OF, months_ago))],
        as_of=_AS_OF)}
    return res["bp-screening"]


def test_the_reading_age_helper_lands_where_it_says_it_does():
    """Guard the guard. Every case below is a claim about a number of months,
    and all three would pass against a rule with no bands at all if this
    arithmetic were wrong in the generous direction."""
    assert _months_before(_AS_OF, 18) == "2025-01-01"
    assert _months_before(_AS_OF, 40) == "2023-03-01"
    assert _months_before(_AS_OF, 0) == _AS_OF


def test_a_reading_18_months_old_still_covers_someone_under_40():
    """The defect, stated as the person it reached: 18 months is inside the
    3-5 year interval USPSTF describes for 18-39.

    MUTATION: delete `cadence_bands` from the bp-screening rule -> red, the
    rule falls back to 12 months and reports a gap the guideline does not.
    """
    bp = _bp_result(age=30, months_ago=18)
    assert bp["status"] == "up_to_date", (
        "an under-40 patient was told they were due 18 months after a normal "
        "reading, on the 40+ annual cadence")
    assert bp["cadence"] == "every 3 years"


def test_the_same_reading_leaves_someone_over_40_due():
    """The band must not become a blanket relaxation. 40+ is annual, and 18
    months is past it.

    MUTATION: widen the band to cover every age -> red.
    """
    bp = _bp_result(age=45, months_ago=18)
    assert bp["status"] == "due"
    assert bp["cadence"] == "yearly"


def test_an_under_40_reading_does_go_stale_eventually():
    """40 months is outside the band. Without this, the first test passes for
    a rule that simply never expires under 40.

    MUTATION: set the band's cadence_months above 40 -> red.
    """
    bp = _bp_result(age=30, months_ago=40)
    assert bp["status"] == "due"


def test_every_age_the_rule_applies_to_gets_a_cadence_somebody_chose():
    """No age in the rule's own range may fall through to a cadence nobody
    picked for it. Swept rather than sampled at the edges: a band that leaves
    a hole reports the fallback, which looks like an answer.
    """
    rule = next(r for r in CARE_GAP_RULES if r["id"] == "bp-screening")
    # To max_age inclusive, not to 90. The sweep stopped 31 years short of the
    # ages the rule claims, so a band leaving a hole anywhere in 90-120 would
    # have reported the fallback and the sweep would never have looked. The
    # rule's own bounds are the only defensible end point for a test whose
    # subject is "every age the rule applies to".
    lo, hi = rule["applies"]["min_age"], rule["applies"]["max_age"]
    for age in range(lo, hi + 1):
        bp = _bp_result(age=age, months_ago=0)
        assert bp["status"] != "not_applicable", (
            f"age {age} is inside the rule's own {lo}-{hi} range and was "
            "dismissed as not applicable")
        assert bp["cadence"] == ("every 3 years" if age < 40 else "yearly"), age


def test_the_a1c_gate_reports_what_the_records_held_not_what_the_person_has():
    """A gate that read Conditions may only speak about Conditions.

    "Applies to patients with a diabetes diagnosis", printed beside a
    screening that has been set aside, reads as a finding that this person
    does not have diabetes. The gate established no such thing — it saw the
    Conditions in the connected records, which are routinely incomplete, and
    the same absence is produced by a record that was never shared.

    Same rule as "no known allergies is never inferred" (docs/constitution.md)
    and the same shape as #389: an absence in our copy of the data reported as
    an absence in the person.

    MUTATION: restore the old wording -> red.
    """
    res = {r["rule_id"]: r for r in evaluate_care_gaps(
        _patient(), conditions=[], as_of="2026-07-01")}
    a1c = res["diabetes-a1c"]
    assert a1c["status"] == "not_applicable"
    note = a1c["note"]
    assert note == "no diabetes diagnosis found in your connected records"
    # The claim is bounded by where we looked.
    assert "connected records" in note
    assert "applies to patients" not in note


# ─────────────────────────────────────────────
# A row that could not pick a band must not quote one (#616)
#
# `base` was assembled at the top of the loop from `rule["cadence_months"]`,
# before any gate ran, and the age-banded `_cadence_for` was consulted only
# after the age gate passed. So the row for an unknown or missing date of
# birth said "date of birth unknown — cannot determine eligibility" and
# "yearly" in the same object: it admitted it did not know the age and then
# asserted the cadence for one age band.
#
# The patient-facing filter drops that row (`_consumer_line` renders only
# `applicable is True`), which is what made it look contained. It is not: the
# operation's `detail` parameter is the raw unfiltered results
# (r6/caregaps/routes.py) and the connector tool returns the whole Parameters
# document unchanged (services/agent-orchestrator/src/tools.ts), so an agent
# narrating the record can quote "yearly" as authoritative for someone who may
# be twenty-five.
#
# Asserted as a property of every banded rule rather than against the string
# "yearly", so it still holds when bands are added to a second rule.
# ─────────────────────────────────────────────

#: Inputs that leave `_age_years` with nothing to work from. Missing and
#: unparseable are different journeys to the same row, and the row carries the
#: same contradiction either way.
_AGELESS = ({"resourceType": "Patient", "gender": "female"},
            {"resourceType": "Patient", "gender": "female", "birthDate": ""},
            {"resourceType": "Patient", "gender": "female",
             "birthDate": "not-a-date"})


def _band_figures(rule):
    """Every cadence this rule can describe — its own and each band's.

    The rule's own `cadence_months` is in here on purpose: for bp-screening it
    IS a band's figure (the 40-and-over one), so "not a band's, just the
    default" would print the same false claim.
    """
    months = {rule["cadence_months"]}
    months |= {b["cadence_months"] for b in rule.get("cadence_bands") or ()}
    return {_cadence_desc(m) for m in months}


def test_a_row_that_never_picked_a_band_quotes_no_bands_figure():
    """MUTATION: build `base` from `_cadence_desc(rule["cadence_months"])`
    again -> red, the birth-date-unknown row reports "yearly".
    """
    banded = [r for r in CARE_GAP_RULES if r.get("cadence_bands")]
    assert banded, (
        "no rule carries cadence_bands — this test would assert nothing; "
        "delete it or give it a rule to guard")
    for patient in _AGELESS:
        res = {r["rule_id"]: r for r in evaluate_care_gaps(
            patient, as_of=_AS_OF)}
        for rule in banded:
            row = res[rule["id"]]
            assert row["cadence"] not in _band_figures(rule), (
                f"{rule['id']}: {row['cadence']!r} belongs to one age band, "
                f"and this row has just said it does not know the age "
                f"({row.get('indeterminate_reason') or row['note']})")
            # Nothing else may stand in for it either: a range describing both
            # bands is still an answer to the question the row has just
            # declined to answer, and the note is what explains the silence.
            assert row["cadence"] is None, row["cadence"]


def test_an_unbanded_rule_still_states_its_one_cadence_with_no_age():
    """The scope of the fix, pinned from the other side. A rule with a single
    cadence for everyone it covers makes no age-dependent claim, so blanking
    it would drop something true — and "consistency" is the reason someone
    would.
    """
    unbanded = [r for r in CARE_GAP_RULES if not r.get("cadence_bands")]
    assert unbanded, "every rule is banded — this test asserts nothing"
    for patient in _AGELESS:
        res = {r["rule_id"]: r for r in evaluate_care_gaps(
            patient, as_of=_AS_OF)}
        for rule in unbanded:
            assert res[rule["id"]]["cadence"] == _cadence_desc(
                rule["cadence_months"]), rule["id"]
