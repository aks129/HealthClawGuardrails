"""Tests for the canonical healthclaw-intake Questionnaire (forms rail Task 1).

Covers:
  - Valid FHIR R4 Questionnaire shape.
  - Repeating groups for medications / allergies / conditions exist and are
    structured with stable linkIds for Task 2 (list-resource population) to
    fill in later.
  - The NKA (no-known-allergies) invariant: it is a distinct affirmative
    attestation, never defaulted and never inferrable from absent data.
  - $populate still runs end-to-end against the new Questionnaire and mirrors
    its structure in the QuestionnaireResponse. List-resource population
    itself (filling medications/allergies/conditions from FHIR resources) is
    covered in tests/test_populate_lists.py (Task 2); this file only checks
    that, absent any list resources, those groups populate to zero repeats
    rather than fabricating anything.
"""

from r6.sdc.intake import intake_questionnaire
from r6.sdc.populate import populate_questionnaire


def _walk(items):
    for item in items:
        yield item
        if "item" in item:
            yield from _walk(item["item"])


def _by_link_id(questionnaire, link_id):
    for item in _walk(questionnaire.get("item", [])):
        if item.get("linkId") == link_id:
            return item
    return None


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_valid_fhir_questionnaire_shape():
    q = intake_questionnaire()
    assert q["resourceType"] == "Questionnaire"
    assert q["status"] == "active"
    assert q["id"] == "healthclaw-intake"
    assert q["url"] == "http://healthclaw.io/fhir/Questionnaire/healthclaw-intake"
    assert isinstance(q["item"], list) and q["item"]


def test_intake_questionnaire_returns_a_fresh_copy_each_call():
    a = intake_questionnaire()
    b = intake_questionnaire()
    assert a == b
    a["item"].append({"linkId": "mutated"})
    assert b != a
    assert _by_link_id(intake_questionnaire(), "mutated") is None


def test_all_linkids_are_unique():
    q = intake_questionnaire()
    link_ids = [item["linkId"] for item in _walk(q["item"])]
    assert len(link_ids) == len(set(link_ids))


# ---------------------------------------------------------------------------
# Demographics
# ---------------------------------------------------------------------------

def test_demographics_group_present():
    q = intake_questionnaire()
    demo = _by_link_id(q, "demographics")
    assert demo is not None
    assert demo["type"] == "group"
    child_ids = {c["linkId"] for c in demo["item"]}
    assert {
        "demographics.given-name",
        "demographics.family-name",
        "demographics.birth-date",
        "demographics.gender",
        "demographics.phone",
    } <= child_ids
    # Address is present in some structured form under the demographics group.
    assert any(cid.startswith("demographics.address") for cid in child_ids)


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------

def test_medications_repeating_group_present():
    q = intake_questionnaire()
    repeat_group = _by_link_id(q, "medications.item")
    assert repeat_group is not None
    assert repeat_group["type"] == "group"
    assert repeat_group["repeats"] is True
    child_ids = {c["linkId"] for c in repeat_group["item"]}
    assert "medications.item.name" in child_ids


def test_medications_no_current_medications_boolean():
    q = intake_questionnaire()
    item = _by_link_id(q, "medications.no-current-medications")
    assert item is not None
    assert item["type"] == "boolean"
    assert item["text"] == "Patient confirms no current medications"


# ---------------------------------------------------------------------------
# Allergies
# ---------------------------------------------------------------------------

def test_allergies_repeating_group_present():
    q = intake_questionnaire()
    repeat_group = _by_link_id(q, "allergies.item")
    assert repeat_group is not None
    assert repeat_group["type"] == "group"
    assert repeat_group["repeats"] is True
    child_ids = {c["linkId"] for c in repeat_group["item"]}
    assert "allergies.item.allergen" in child_ids


def test_no_known_allergies_is_required_boolean_with_exact_text():
    q = intake_questionnaire()
    item = _by_link_id(q, "allergies.no-known-allergies")
    assert item is not None
    assert item["type"] == "boolean"
    assert item["required"] is True
    assert item["text"] == "No known allergies (patient confirmed)"


def test_no_known_allergies_invariant_never_defaulted():
    """CRITICAL: 'no known allergies' must be an affirmative attestation.

    It must never carry an `initial` value and must never be populated via
    an initialExpression — silence about allergies is not consent, and a
    default here would let the form-fill rail claim NKA for a patient who
    was simply never asked. This is load-bearing for patient safety.
    """
    q = intake_questionnaire()
    item = _by_link_id(q, "allergies.no-known-allergies")
    assert "initial" not in item

    initial_expr_url = (
        "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
        "sdc-questionnaire-initialExpression"
    )
    for ext in item.get("extension", []):
        assert ext.get("url") != initial_expr_url, (
            "allergies.no-known-allergies must never be populated by an "
            "initialExpression — it must be an explicit patient attestation"
        )


# ---------------------------------------------------------------------------
# Problems / conditions
# ---------------------------------------------------------------------------

def test_conditions_repeating_group_present():
    q = intake_questionnaire()
    repeat_group = _by_link_id(q, "conditions.item")
    assert repeat_group is not None
    assert repeat_group["type"] == "group"
    assert repeat_group["repeats"] is True
    child_ids = {c["linkId"] for c in repeat_group["item"]}
    assert "conditions.item.name" in child_ids


# ---------------------------------------------------------------------------
# $populate integration
# ---------------------------------------------------------------------------

def test_populate_mirrors_new_structure():
    q = intake_questionnaire()
    patient = {
        "resourceType": "Patient",
        "id": "p1",
        "name": [{"given": ["Ada"], "family": "Lovelace"}],
        "birthDate": "1815-12-10",
        "gender": "female",
        "telecom": [{"system": "phone", "value": "617-555-0198"}],
        "address": [{"line": ["123 Clinical Ave"], "city": "Boston",
                     "state": "MA", "postalCode": "02101"}],
    }

    qr, issues = populate_questionnaire(q, patient, [patient])

    assert qr["resourceType"] == "QuestionnaireResponse"
    assert issues == []

    demo_group = next(i for i in qr["item"] if i["linkId"] == "demographics")
    given = next(i for i in demo_group["item"]
                 if i["linkId"] == "demographics.given-name")
    assert given["answer"][0]["valueString"] == "Ada"
    family = next(i for i in demo_group["item"]
                  if i["linkId"] == "demographics.family-name")
    assert family["answer"][0]["valueString"] == "Lovelace"

    # No MedicationRequest/AllergyIntolerance/Condition resources were
    # supplied, so the repeating medications.item/allergies.item groups
    # populate to zero repeats (Task 2: list-resource population never
    # fabricates a repeat, and absence of data never flips a sibling
    # boolean — see test_populate_lists.py for the full Task 2 coverage).
    meds_group = next(i for i in qr["item"] if i["linkId"] == "medications")
    assert all(i["linkId"] != "medications.item" for i in meds_group["item"])
    no_current_meds = next(i for i in meds_group["item"]
                            if i["linkId"] == "medications.no-current-medications")
    assert "answer" not in no_current_meds

    allergies_group = next(i for i in qr["item"]
                            if i["linkId"] == "allergies")
    assert all(i["linkId"] != "allergies.item" for i in allergies_group["item"])
    nka = next(i for i in allergies_group["item"]
               if i["linkId"] == "allergies.no-known-allergies")
    assert "answer" not in nka
