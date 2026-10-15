"""Security review of PR #957, delta 830cf7a..96b89a4.

D5-F1 (failed at 96b89a4; fixed by taking LA14761-3 back out of
ALLERGY_STATUS_CONCEPTS): LOINC LA14761-3 "Not asked" is a generic answer
used on any question. It is now in ALLERGY_STATUS_CONCEPTS, and the engine
writes "Recorded entry: 'not asked about allergies'. Allergies were not
checked" on every Observation answered with it. On "Smoking status: Not
asked" that text is a false statement about the person, and every MCP client
receives it.

Brief: a Condition left system-only by redaction (a malformed NKA spelling)
shows as "Recorded, name unavailable · Active". That is pinned as a non-strict
xfail (Low, copy).

Synthetic data only.
"""

from __future__ import annotations

import pytest

from r6.redaction import apply_redaction
from r6.terminology import LOINC, SNOMED

_TX = {(LOINC, "72166-2"): "Smoking status",
       (LOINC, "LA19544-8"): "None detected"}


@pytest.fixture(autouse=True)
def _tx_fhir_org(monkeypatch):
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda system, code: _TX.get((system, code)))


def _obs(question, answer):
    return {"resourceType": "Observation", "id": "o", "status": "final",
            "code": {"coding": [{"system": LOINC, "code": question}]},
            "valueCodeableConcept": {"coding": [answer]}}


def test_d5_f1_a_non_allergy_not_asked_never_claims_allergies_unchecked():
    out = apply_redaction(_obs("72166-2", {"system": LOINC,
                                           "code": "LA14761-3"}))
    assert "allerg" not in (out["valueCodeableConcept"].get("text") or "").lower()


def test_control_none_on_smoking_status_has_no_allergy_claim():
    out = apply_redaction(_obs("72166-2", {"system": SNOMED,
                                           "code": "260413007"}))
    assert "allerg" not in out["valueCodeableConcept"]["text"].lower()


def test_control_d4_f1_closed():
    out = apply_redaction({"resourceType": "AllergyIntolerance", "id": "a",
                           "code": {"coding": [{"system": SNOMED,
                                                "code": "260413007"}]}})
    assert out["code"]["text"].startswith("Recorded entry: ")
    assert "display" not in out["code"]["coding"][0]


@pytest.mark.xfail(strict=False, reason="Low: a malformed NKA spelling reads "
                   "as an unnamed active problem on the brief")
def test_brief_hides_a_condition_whose_only_code_was_a_malformed_absence():
    from r6.brief import engine
    c = {"resourceType": "Condition", "id": "c",
         "clinicalStatus": {"coding": [{"code": "active"}]},
         "code": {"coding": [{"system": SNOMED,
                              "code": "716186003|No known allergy|"}]}}
    assert engine.build_problems([apply_redaction(c)]) == []
