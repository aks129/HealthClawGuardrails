"""#880: a stored Immunization can close the flu-vaccine gap, and only the
patient's own, completed, dated one in the window does.

Two readers were wrong together. The evaluator read `code`, which an
Immunization does not have (its code is `vaccineCode`), and the shared subject
matcher read `subject`, which an Immunization does not have either (it names
its patient in `patient`). Fixing the first alone would let another person's
shot close this patient's gap on a one-Patient tenant, because with no
`subject` every Immunization fell under the no-subject rule. That direction
withholds a due item, so both are pinned here.

Synthetic data only.
"""

import json
from datetime import date, timedelta

import pytest

from r6.caregaps.evaluate import CARE_GAP_RULES, evaluate_care_gaps
from tests.test_labs_subject_matching import PID, _row

AS_OF = "2026-07-01"
CVX = "http://hl7.org/fhir/sid/cvx"


def _adult():
    return {"resourceType": "Patient", "id": PID, "gender": "female",
            "birthDate": "1970-03-01"}


def _shot(rid="imm1", *, patient=f"Patient/{PID}", cvx="140",
          status="completed", when=None, no_patient=False, **extra):
    imm = {"resourceType": "Immunization", "id": rid, "status": status,
           "vaccineCode": {"coding": [{"system": CVX, "code": cvx}]},
           "occurrenceDateTime": when or "2026-06-01"}
    if not no_patient:
        imm["patient"] = {"reference": patient}
    imm.update(extra)
    return imm


def _flu(results):
    return next(r for r in results if r["rule_id"] == "flu-immunization")


def _evaluate(*immunizations):
    return _flu(evaluate_care_gaps(_adult(), immunizations=list(immunizations),
                                   as_of=AS_OF))


# ── The evaluator ────────────────────────────────────────────────────────

def test_a_completed_flu_shot_in_the_window_closes_the_gap():
    """MUTATION: read `code` instead of `vaccineCode` -> red."""
    flu = _evaluate(_shot())
    assert flu["status"] == "up_to_date"
    assert flu["last_done"] == "2026-06-01"


@pytest.mark.parametrize("status", ["not-done", "entered-in-error", "",
                                    None, ["completed"]])
def test_only_a_completed_shot_closes_the_gap(status):
    """MUTATION: drop the status filter -> red for every row."""
    flu = _evaluate(_shot(status=status))
    assert flu["status"] == "due"
    assert flu["last_done"] is None


def test_a_shot_outside_the_window_does_not_close_the_gap():
    flu = _evaluate(_shot(when="2025-05-01"))
    assert flu["status"] == "due"


def test_a_future_dated_shot_does_not_close_the_gap():
    flu = _evaluate(_shot(when="2026-09-01"))
    assert flu["status"] == "due"


def test_an_occurrence_string_is_undated_and_cannot_close_the_gap():
    imm = _shot()
    del imm["occurrenceDateTime"]
    imm["occurrenceString"] = "last autumn"
    assert _evaluate(imm)["status"] == "due"


def test_only_the_occurrence_date_dates_an_immunization():
    """`recorded` is when the row was written, not when the shot was given,
    and the other date fields are not Immunization fields at all."""
    imm = _shot()
    del imm["occurrenceDateTime"]
    imm["recorded"] = "2026-06-01"
    imm["effectiveDateTime"] = "2026-06-01"
    assert _evaluate(imm)["status"] == "due"


def test_a_code_under_code_rather_than_vaccineCode_does_not_count():
    imm = _shot()
    imm["code"] = imm.pop("vaccineCode")
    assert _evaluate(imm)["status"] == "due"


def test_a_non_influenza_vaccine_does_not_close_the_gap():
    # 213: SARS-COV-2 (COVID-19), unspecified formulation.
    assert _evaluate(_shot(cvx="213"))["status"] == "due"


@pytest.mark.parametrize("vaccine_code", [
    "140", ["140"], None, {"coding": None}, {"coding": [None, "140"]},
    {"coding": [{"code": None}]}, {"coding": [{"code": ["140"]}]},
    {"text": "influenza"},
], ids=["string", "list", "null", "null-coding", "junk-codings",
        "null-code", "list-code", "text-only"])
def test_a_malformed_vaccine_code_is_skipped_not_fatal(vaccine_code):
    """And text is never read as a code: `{"text": "influenza"}` closes
    nothing."""
    imm = _shot()
    imm["vaccineCode"] = vaccine_code
    assert _evaluate(imm)["status"] == "due"


def test_the_standard_seasonal_influenza_cvx_codes_close_the_gap():
    """Seasonal influenza codes from the CDC CVX table, active or not. A
    12-month window makes a retired code harmless."""
    codes = next(r for r in CARE_GAP_RULES
                 if r["id"] == "flu-immunization")["satisfied_by"]["codes"]
    for cvx in ("88", "111", "135", "140", "141", "149", "150", "153", "155",
                "158", "161", "168", "171", "185", "186", "197", "205",
                "320", "333", "338"):
        assert cvx in codes, cvx
    # Pandemic, avian and non-influenza codes are not a seasonal flu shot.
    for cvx in ("69", "123", "125", "126", "127", "128", "160", "321",
                "322", "323"):
        assert cvx not in codes, cvx


def test_conditions_are_still_read_from_code():
    """`_codes_of` serves the diabetes gate too; Conditions keep `code`."""
    cond = {"resourceType": "Condition",
            "code": {"coding": [{"system": "http://snomed.info/sct",
                                 "code": "44054006"}]}}
    a1c = next(r for r in evaluate_care_gaps(_adult(), conditions=[cond],
                                             as_of=AS_OF)
               if r["rule_id"] == "diabetes-a1c")
    assert a1c["applicable"] is True


# ── The subject matcher ──────────────────────────────────────────────────

def _patient_row(app, tenant_id, pid=PID):
    _row(app, tenant_id, {"resourceType": "Patient", "id": pid,
                          "gender": "female", "birthDate": "1970-03-01"})


def _rows(app, tenant_id, subject=f"Patient/{PID}"):
    from r6.caregaps.routes import subject_rows
    with app.app_context():
        rows, unreadable = subject_rows("Immunization", subject, tenant_id)
    return sorted(r["id"] for r in rows), unreadable


def _recent():
    return (date.today() - timedelta(days=30)).isoformat()


def _care_gaps_flu(client, headers, subject=None):
    url = "/r6/fhir/Patient/$care-gaps"
    if subject:
        url += f"?subject={subject}"
    r = client.post(url, headers=headers, json={})
    assert r.status_code == 200, r.get_data(as_text=True)
    detail = next(p for p in r.get_json()["parameter"]
                  if p["name"] == "detail")
    return _flu(json.loads(detail["valueString"]))


def test_the_patients_own_shot_closes_the_gap_end_to_end(
        app, client, tenant_id, tenant_headers):
    """The reproduction from the issue: one Patient, a recent CVX 140 shot
    naming that Patient, an empty body.

    MUTATION: read `subject` instead of `patient` for Immunization -> red.
    """
    _patient_row(app, tenant_id)
    _row(app, tenant_id, _shot(when=_recent()))
    flu = _care_gaps_flu(client, tenant_headers)
    assert flu["status"] == "up_to_date"
    assert flu["last_done"] == _recent()


@pytest.mark.parametrize("ref", [
    f"Patient/{PID}", f"https://ehr.example.org/fhir/Patient/{PID}",
    f"urn:uuid:{PID}", f"Patient/{PID}/_history/2",
])
def test_each_reference_form_in_patient_matches(app, tenant_id, ref):
    _patient_row(app, tenant_id)
    _patient_row(app, tenant_id, "p-two")
    _row(app, tenant_id, _shot(patient=ref))
    assert _rows(app, tenant_id) == (["imm1"], 0)


@pytest.mark.parametrize("patients", [[PID], [PID, "p-two"]],
                         ids=["one-patient", "two-patients"])
def test_another_patients_shot_does_not_close_the_gap(
        app, client, tenant_id, tenant_headers, patients):
    """The hazard the issue names: on a one-Patient tenant, a shot whose
    `patient` names somebody else is not this patient's."""
    for pid in patients:
        _patient_row(app, tenant_id, pid)
    _row(app, tenant_id, _shot(patient="Patient/p-other", when=_recent()))
    assert _rows(app, tenant_id) == ([], 0)
    flu = _care_gaps_flu(client, tenant_headers, f"Patient/{PID}")
    assert flu["status"] == "due"


def test_a_not_done_shot_does_not_close_the_gap_end_to_end(
        app, client, tenant_id, tenant_headers):
    _patient_row(app, tenant_id)
    _row(app, tenant_id, _shot(status="not-done", when=_recent()))
    assert _care_gaps_flu(client, tenant_headers)["status"] == "due"


def test_a_shot_outside_the_window_does_not_close_the_gap_end_to_end(
        app, client, tenant_id, tenant_headers):
    _patient_row(app, tenant_id)
    old = (date.today() - timedelta(days=400)).isoformat()
    _row(app, tenant_id, _shot(when=old))
    assert _care_gaps_flu(client, tenant_headers)["status"] == "due"


def test_a_shot_with_neither_field_follows_the_no_subject_rule(
        app, client, tenant_id, tenant_headers):
    """The disclosed policy (#878): a row naming nobody belongs to the
    tenant's one Patient. An Immunization with no `patient` is such a row."""
    _patient_row(app, tenant_id)
    _row(app, tenant_id, _shot(no_patient=True, when=_recent()))
    assert _rows(app, tenant_id) == (["imm1"], 0)
    assert _care_gaps_flu(client, tenant_headers)["status"] == "up_to_date"


def test_a_shot_with_no_patient_belongs_to_nobody_on_a_shared_tenant(
        app, tenant_id):
    _patient_row(app, tenant_id)
    _patient_row(app, tenant_id, "p-two")
    _row(app, tenant_id, _shot(no_patient=True))
    assert _rows(app, tenant_id) == ([], 0)


def test_a_subject_on_an_immunization_is_not_read_as_its_patient(
        app, client, tenant_id, tenant_headers):
    """`subject` is not an Immunization field. A row carrying one names
    somebody, so it is not a row naming nobody: it is unreadable, never
    claimed by the no-subject rule and never read as the patient."""
    _patient_row(app, tenant_id)
    _row(app, tenant_id, _shot("imm-other", no_patient=True, when=_recent(),
                               subject={"reference": "Patient/p-other"}))
    _row(app, tenant_id, _shot("imm-mine", no_patient=True, when=_recent(),
                               subject={"reference": f"Patient/{PID}"}))
    assert _rows(app, tenant_id) == ([], 2)
    assert _care_gaps_flu(client, tenant_headers)["status"] == "due"


def test_observations_still_match_on_subject(app, tenant_id):
    from r6.caregaps.routes import subject_rows
    from tests.test_labs_subject_matching import _obs
    _patient_row(app, tenant_id)
    _patient_row(app, tenant_id, "p-two")
    _row(app, tenant_id, _obs("o1"))
    with app.app_context():
        rows, _ = subject_rows("Observation", f"Patient/{PID}", tenant_id)
    assert [r["id"] for r in rows] == ["o1"]


@pytest.mark.parametrize("patient", [
    f"Patient/{PID}", [{"reference": f"Patient/{PID}"}], 7,
    {"reference": ["Patient/x"]}, {"reference": None},
], ids=["string", "list", "number", "list-reference", "null-reference"])
def test_a_malformed_patient_is_unreadable_not_a_500(
        app, client, tenant_id, tenant_headers, patient):
    _patient_row(app, tenant_id)
    _row(app, tenant_id, {**_shot(no_patient=True, when=_recent()),
                          "patient": patient})
    assert _rows(app, tenant_id) == ([], 1)
    assert _care_gaps_flu(client, tenant_headers)["status"] == "due"


def test_a_patient_with_only_an_identifier_names_no_row_we_hold(
        app, tenant_id):
    _patient_row(app, tenant_id)
    _row(app, tenant_id, {**_shot(no_patient=True),
                          "patient": {"identifier": {"value": "x"}}})
    assert _rows(app, tenant_id) == ([], 0)


def test_a_non_patient_subject_compares_patient_exactly(app, tenant_id):
    """The legacy exact-string branch reads the same field."""
    _patient_row(app, tenant_id)
    _row(app, tenant_id, _shot(patient="Group/g1"))
    _row(app, tenant_id, _shot("imm2", no_patient=True,
                               subject={"reference": "Group/g1"}))
    assert _rows(app, tenant_id, "Group/g1") == (["imm1"], 0)
