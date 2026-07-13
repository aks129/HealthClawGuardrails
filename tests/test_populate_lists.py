"""Tests for populate.py's list-resource population (forms rail Task 2).

Covers filling the intake Questionnaire's repeating medications / allergies /
conditions groups from a subject's MedicationRequest / AllergyIntolerance /
Condition resources.

THE LOAD-BEARING SAFETY INVARIANT: `allergies.no-known-allergies` (and
similarly `medications.no-current-medications`) must NEVER be auto-answered
by populate — not `true`, not `false`, no `answer` key at all — regardless
of whether the patient has any allergies/medications on file. Absent data is
not consent to a "no known allergies" attestation; only a human can make
that call (Task 6). See test_zero_allergies_never_infers_no_known_allergies
below, which is the point of this whole file.
"""

from r6.sdc.intake import intake_questionnaire
from r6.sdc.populate import populate_questionnaire


PATIENT = {
    "resourceType": "Patient",
    "id": "p1",
    "name": [{"given": ["Ada"], "family": "Lovelace"}],
    "birthDate": "1815-12-10",
    "gender": "female",
}


def _walk(items):
    for item in items:
        yield item
        if "item" in item:
            yield from _walk(item["item"])


def _by_link_id(qr, link_id):
    """Return the first item with this linkId (groups may repeat)."""
    for item in _walk(qr.get("item", [])):
        if item.get("linkId") == link_id:
            return item
    return None


def _all_by_link_id(qr, link_id):
    return [item for item in _walk(qr.get("item", [])) if item.get("linkId") == link_id]


def _med_request(rid, display, status="active", dose=None):
    resource = {
        "resourceType": "MedicationRequest",
        "id": rid,
        "status": status,
        "intent": "order",
        "subject": {"reference": "Patient/p1"},
        "medicationCodeableConcept": {
            "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                        "code": "0000", "display": display}],
        },
    }
    if dose:
        resource["dosageInstruction"] = [{"text": dose}]
    return resource


def _allergy(rid, allergen, reaction=None):
    resource = {
        "resourceType": "AllergyIntolerance",
        "id": rid,
        "patient": {"reference": "Patient/p1"},
        "code": {"text": allergen},
    }
    if reaction:
        resource["reaction"] = [{"manifestation": [{"text": reaction}]}]
    return resource


def _condition(rid, name):
    return {
        "resourceType": "Condition",
        "id": rid,
        "subject": {"reference": "Patient/p1"},
        "clinicalStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
            "code": "active"}]},
        "verificationStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
            "code": "confirmed"}]},
        "code": {"text": name},
    }


# ---------------------------------------------------------------------------
# 1. Medications populate from active MedicationRequests
# ---------------------------------------------------------------------------

def test_two_active_medications_populate_two_repeats():
    q = intake_questionnaire()
    meds = [
        _med_request("m1", "Metformin 500 MG Oral Tablet", dose="Twice daily"),
        _med_request("m2", "Lisinopril 10 MG Oral Tablet"),
    ]
    content = [PATIENT] + meds

    qr, issues = populate_questionnaire(q, PATIENT, content)

    repeats = _all_by_link_id(qr, "medications.item")
    assert len(repeats) == 2
    names = {
        next(c["answer"][0]["valueString"] for c in r["item"]
             if c["linkId"] == "medications.item.name")
        for r in repeats
    }
    assert names == {"Metformin 500 MG Oral Tablet", "Lisinopril 10 MG Oral Tablet"}

    dosed = next(r for r in repeats
                 if any(c["linkId"] == "medications.item.name"
                        and c["answer"][0]["valueString"] == "Metformin 500 MG Oral Tablet"
                        for c in r["item"]))
    dose_item = next(c for c in dosed["item"] if c["linkId"] == "medications.item.dose")
    assert dose_item["answer"][0]["valueString"] == "Twice daily"
    assert issues == []


def test_inactive_medications_excluded():
    q = intake_questionnaire()
    meds = [
        _med_request("m1", "Metformin 500 MG Oral Tablet", status="active"),
        _med_request("m2", "Old Stopped Drug", status="stopped"),
        _med_request("m3", "Cancelled Drug", status="cancelled"),
    ]
    content = [PATIENT] + meds

    qr, _issues = populate_questionnaire(q, PATIENT, content)

    repeats = _all_by_link_id(qr, "medications.item")
    assert len(repeats) == 1
    name_item = next(c for c in repeats[0]["item"] if c["linkId"] == "medications.item.name")
    assert name_item["answer"][0]["valueString"] == "Metformin 500 MG Oral Tablet"


# ---------------------------------------------------------------------------
# 2 & 3. Allergies populate + the load-bearing NKA invariant
# ---------------------------------------------------------------------------

def test_one_allergy_populates_and_nka_stays_unanswered():
    q = intake_questionnaire()
    content = [PATIENT, _allergy("a1", "Penicillin", reaction="Hives")]

    qr, issues = populate_questionnaire(q, PATIENT, content)

    repeats = _all_by_link_id(qr, "allergies.item")
    assert len(repeats) == 1
    allergen = next(c for c in repeats[0]["item"] if c["linkId"] == "allergies.item.allergen")
    assert allergen["answer"][0]["valueString"] == "Penicillin"
    reaction = next(c for c in repeats[0]["item"] if c["linkId"] == "allergies.item.reaction")
    assert reaction["answer"][0]["valueString"] == "Hives"

    nka = _by_link_id(qr, "allergies.no-known-allergies")
    assert "answer" not in nka
    assert issues == []


def test_zero_allergies_never_infers_no_known_allergies():
    """THE LOAD-BEARING TEST.

    Zero AllergyIntolerance resources must NOT cause the engine to answer
    `allergies.no-known-allergies` with true, false, or anything at all.
    Absent data is not an attestation. A human must affirm NKA explicitly
    (Task 6's review step) — populate must never do it on their behalf.
    """
    q = intake_questionnaire()
    content = [PATIENT]  # no AllergyIntolerance resources whatsoever

    qr, issues = populate_questionnaire(q, PATIENT, content)

    nka = _by_link_id(qr, "allergies.no-known-allergies")
    assert nka is not None
    assert "answer" not in nka, (
        "allergies.no-known-allergies must never be auto-answered — absent "
        "allergy data is not consent to a no-known-allergies attestation"
    )

    # The allergies repeating group produced zero repeats — it's empty, not
    # defaulted to anything.
    assert _all_by_link_id(qr, "allergies.item") == []
    assert issues == []


# ---------------------------------------------------------------------------
# 4. Medications: same absent-data-is-not-inference principle
# ---------------------------------------------------------------------------

def test_zero_medications_never_infers_no_current_medications():
    q = intake_questionnaire()
    content = [PATIENT]  # no MedicationRequest resources whatsoever

    qr, issues = populate_questionnaire(q, PATIENT, content)

    no_current = _by_link_id(qr, "medications.no-current-medications")
    assert no_current is not None
    assert "answer" not in no_current

    assert _all_by_link_id(qr, "medications.item") == []
    assert issues == []


# ---------------------------------------------------------------------------
# 5. Conditions populate
# ---------------------------------------------------------------------------

def test_three_conditions_populate_three_repeats():
    q = intake_questionnaire()
    conditions = [
        _condition("c1", "Type 2 diabetes mellitus"),
        _condition("c2", "Essential hypertension"),
        _condition("c3", "Hyperlipidemia"),
    ]
    content = [PATIENT] + conditions

    qr, issues = populate_questionnaire(q, PATIENT, content)

    repeats = _all_by_link_id(qr, "conditions.item")
    assert len(repeats) == 3
    names = {
        next(c["answer"][0]["valueString"] for c in r["item"]
             if c["linkId"] == "conditions.item.name")
        for r in repeats
    }
    assert names == {"Type 2 diabetes mellitus", "Essential hypertension",
                      "Hyperlipidemia"}
    assert issues == []


# ---------------------------------------------------------------------------
# 6. Regression: existing Observation/demographics population still works
# ---------------------------------------------------------------------------

def test_demographics_and_observations_regression():
    q = intake_questionnaire()
    patient = dict(PATIENT, telecom=[{"system": "phone", "value": "617-555-0198"}],
                   address=[{"line": ["123 Clinical Ave"], "city": "Boston",
                             "state": "MA", "postalCode": "02101"}])
    content = [patient]

    qr, issues = populate_questionnaire(q, patient, content)

    given = _by_link_id(qr, "demographics.given-name")
    assert given["answer"][0]["valueString"] == "Ada"
    family = _by_link_id(qr, "demographics.family-name")
    assert family["answer"][0]["valueString"] == "Lovelace"
    dob = _by_link_id(qr, "demographics.birth-date")
    assert dob["answer"][0]["valueDate"] == "1815-12-10"
    assert issues == []


def test_all_three_lists_populate_together_and_nka_still_unanswered():
    q = intake_questionnaire()
    content = [
        PATIENT,
        _med_request("m1", "Metformin 500 MG Oral Tablet"),
        _allergy("a1", "Penicillin"),
        _condition("c1", "Type 2 diabetes mellitus"),
    ]

    qr, issues = populate_questionnaire(q, PATIENT, content)

    assert len(_all_by_link_id(qr, "medications.item")) == 1
    assert len(_all_by_link_id(qr, "allergies.item")) == 1
    assert len(_all_by_link_id(qr, "conditions.item")) == 1
    nka = _by_link_id(qr, "allergies.no-known-allergies")
    assert "answer" not in nka
    assert issues == []
