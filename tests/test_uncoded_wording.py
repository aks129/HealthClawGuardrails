"""Wording for records that were never coded at the source.

Live example (MEDENT tenant, 2026-08-04): the one AllergyIntolerance carries a
single coding with ONLY free text — no system, no code. Redaction rightly
strips the text (real feeds put patient names there), so nothing codeable
remains, and no label table will ever fix that row. The agent called it
"unreadable to me", which sounds like our failure and gives the person
nothing to act on.

Two situations, two sentences:
- code present, label unknown  -> "unlabeled record, code X"   (our table gap)
- nothing codeable ever existed -> "recorded but not coded at the source"

Both keep unreadable=True: whatever the sentence, the record is never absent
(#207, and SAFETY_CORE's never-infer-absence rule — for allergies especially,
"no known allergies" may only come from a human attestation).
"""
from __future__ import annotations

from careagents.agent import _summarize_bundle


def _bundle(*resources):
    return {"entry": [{"resource": r} for r in resources]}


def _allergy_no_code():
    # The live MEDENT shape after redaction: coding existed, carried only
    # display, display stripped -> an empty coding dict.
    return {"resourceType": "AllergyIntolerance", "status": "active",
            "code": {"coding": [{}]}}


def test_a_source_uncoded_record_gets_the_honest_sentence():
    """MUTATION: collapse both cases back to 'unlabeled record' -> red."""
    items = _summarize_bundle(_bundle(_allergy_no_code()))
    assert items[0]["name"] == "recorded but not coded at the source"
    assert items[0]["uncoded"] is True
    assert items[0]["unreadable"] is True
    assert "never treat it as absent" in items[0]["note"]


def test_a_record_with_no_code_field_at_all_gets_the_same_sentence():
    items = _summarize_bundle(_bundle(
        {"resourceType": "AllergyIntolerance", "status": "active"}))
    assert items[0]["name"] == "recorded but not coded at the source"


def test_a_known_gap_still_names_the_code():
    """Our-table-gap wording is unchanged: the code is real and actionable."""
    items = _summarize_bundle(_bundle(
        {"resourceType": "Condition",
         "code": {"coding": [{"system": "http://snomed.info/sct",
                              "code": "9014002"}]}}))
    assert items[0]["name"] == "unlabeled record, code 9014002"
    assert "uncoded" not in items[0]
    assert items[0]["unreadable"] is True


def test_a_code_in_a_later_coding_entry_is_still_found():
    """MUTATION: look only at coding[0] -> red. Real concepts often lead
    with a text-only coding followed by the coded one."""
    items = _summarize_bundle(_bundle(
        {"resourceType": "Condition",
         "code": {"coding": [{}, {"system": "http://snomed.info/sct",
                                  "code": "9014002"}]}}))
    assert items[0]["name"] == "unlabeled record, code 9014002"


def test_a_labelled_record_is_untouched():
    items = _summarize_bundle(_bundle(
        {"resourceType": "Condition",
         "code": {"text": "Hyperlipidemia", "coding": []}}))
    assert items[0]["name"] == "Hyperlipidemia"
    assert "unreadable" not in items[0]
    assert "uncoded" not in items[0]
