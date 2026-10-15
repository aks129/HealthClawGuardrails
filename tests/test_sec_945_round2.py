"""Round 2 security exploits for PR #945 (head f25730e): re-attack of the
F1/F2 fix to allergy-status matching. Synthetic data only."""

from __future__ import annotations

import json

import pytest

from careagents.agent import _summarize_bundle
from tests.test_sec_945_first_answer import DAR, NULLFLAVOR, SNOMED, _allergy


# --- Round 2 (head f25730e): re-attack of the F1/F2 fix ----------------------

def _items(*resources):
    return _summarize_bundle({"resourceType": "Bundle",
                              "entry": [{"resource": r} for r in resources]})


def _status(*codings):
    return {"resourceType": "AllergyIntolerance",
            "code": {"coding": list(codings)}}


@pytest.mark.parametrize("codings", [
    [{"system": SNOMED, "code": "91936005"}, {"system": NULLFLAVOR, "code": "UNK"}],
    [{"system": NULLFLAVOR, "code": "UNK"}, {"system": SNOMED, "code": "91936005"}],
    [{"system": "urn:x-unknown", "code": "ZZ1"}, {"system": DAR, "code": "unknown"}],
    [{"system": NULLFLAVOR, "code": "UNK"}, {"system": SNOMED, "code": "91936005"},
     {"system": NULLFLAVOR, "code": "UNK"}],
])
def test_r2_any_real_coding_keeps_the_record_an_allergy(codings):
    [item] = _items(_status(*codings))
    assert not item.get("not_an_allergy"), item


@pytest.mark.parametrize("codings", [
    [{"system": SNOMED, "code": "1631000175102"}],
    [{"system": SNOMED, "code": "1631000175102"},
     {"system": SNOMED, "code": "1631000175102"}],
])
def test_r2_duplicate_status_codes_stay_status(codings):
    [item] = _items(_status(*codings))
    assert item.get("not_an_allergy")
    assert "no known" not in json.dumps(item).lower()


# Round two narrowed the status set to explicit SNOMED status concepts: a
# coding-level null flavor or data-absent-reason is a real allergy record
# (C-CDA's uncoded allergen, F3), and so is any record with another coding.
@pytest.mark.parametrize("codings", [
    [{"system": NULLFLAVOR, "code": "UNK"}, {"system": NULLFLAVOR, "code": "UNK"}],
    [{"system": SNOMED, "code": "1631000175102"}, {"system": DAR, "code": "not-asked"}],
    [{"system": NULLFLAVOR, "code": "NASK"}],
    [{"system": DAR, "code": "asked-unknown"}],
])
def test_r2_null_flavor_and_mixed_records_are_allergy_records(codings):
    [item] = _items(_status(*codings))
    assert not item.get("not_an_allergy"), item
    assert item.get("unreadable") and item.get("name")


@pytest.mark.parametrize("res", [
    {"resourceType": "AllergyIntolerance", "code": {"coding": []}},
    {"resourceType": "AllergyIntolerance", "code": {}},
    {"resourceType": "AllergyIntolerance"},
    {"resourceType": "AllergyIntolerance", "code": {"coding": [{}]}},
])
def test_r2_empty_codes_are_never_status_and_never_absence(res):
    [item] = _items(res)
    assert not item.get("not_an_allergy")
    assert item.get("unreadable") or item.get("name")


@pytest.mark.parametrize("allergen", [
    {"code": "91936005"},                                # system missing
    {"system": SNOMED, "code": 91936005},                # code not a string
    {"system": None, "code": "91936005"},
])
def test_r2_a_malformed_allergen_coding_is_not_hidden_by_a_null_flavor(allergen):
    """Pre-PR this record read as an unlabeled allergy record. A coding the
    fix cannot key on drops out of `keys`, leaving the null flavor alone to
    decide the record is not an allergy."""
    [item] = _items(_status(allergen, {"system": NULLFLAVOR, "code": "UNK"}))
    assert not item.get("not_an_allergy"), item


def test_r2_a_free_text_allergy_with_a_null_flavored_code_is_an_allergy():
    """The C-CDA shape `<code nullFlavor="UNK"><originalText>…` arrives as a
    NullFlavor coding beside free text. Redaction strips the text, so the
    record reaches CareAgents as an active, high-criticality allergy with a
    reaction, whose substance code is UNK. A null flavor on the *coding*
    says the substance was not coded, not that allergies were never asked.
    Pre-PR it read as "unlabeled record" (a real allergy); now the person is
    told their records don't say whether they have allergies."""
    res = {"resourceType": "AllergyIntolerance",
           "clinicalStatus": {"coding": [{
               "system": "http://terminology.hl7.org/CodeSystem/"
                         "allergyintolerance-clinical", "code": "active"}]},
           "criticality": "high", "category": ["medication"],
           "reaction": [{"severity": "severe", "manifestation": [{"coding": [
               {"system": SNOMED, "code": "39579001"}]}]}],
           "code": {"coding": [{"system": NULLFLAVOR, "code": "UNK"}]}}
    [item] = _items(res)
    assert not item.get("not_an_allergy"), item
    assert "don't say whether" not in json.dumps(item)


def test_r2_a_real_allergy_past_the_page_still_rewords_the_status_items():
    """F2 is decided on the shown items only. Thirteen status records, then
    penicillin: the shown twelve keep "your records don't say whether you
    have allergies" beside a truncation marker, while penicillin is on file."""
    out = _items(*[_status({"system": NULLFLAVOR, "code": "NI"})] * 13,
                 _allergy("pen", (SNOMED, "91936005")))
    assert any(i.get("truncated") for i in out)
    assert not any("don't say whether" in json.dumps(i) for i in out), out


def test_r2_a_status_record_beside_an_allergy_is_not_sent():
    # Was a reworded "do not mention this entry" note; #949 real-model QA
    # showed such a note gets mentioned and disclosed, so the status
    # record is no longer sent at all beside a recorded allergy.
    out = _items(_allergy("pen", (SNOMED, "91936005")),
                 _status({"system": SNOMED, "code": "1631000175102"}))
    assert not [i for i in out if i.get("not_an_allergy")]
    blob = json.dumps(out).lower()
    for bad in ("no known", "no allergies", "don't say whether",
                "patient not asked", "do not mention"):
        assert bad not in blob
