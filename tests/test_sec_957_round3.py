"""Security review of PR #957, round 3 (head 472f0fc).

Round-2 bypasses are closed by LOINC_ABSENCE_ANSWERS. This file pins what
round 3 found (live tx.fhir.org answers captured 2026-10-14):

- HL7 v3 NullFlavor codes are not resolvable, so they carry no label: PASS.
- A generic "No" answering an allergy question (LOINC 66374-0 = LA32-8,
  SNOMED 1300212001 "Allergy status" = 373067005) reads back as the bare
  answer "No". That is not an absence concept, and no code set can close it
  without knowing the question. Follow-up, non-blocking: xfail.
- Wording false positives: real labels the broadened pattern dropped.
  Fixed: a negation is refused only when it is about allergies or
  sensitivities, so these are must-keep controls now.

Synthetic data only.
"""

from __future__ import annotations

import json

import pytest

from r6 import terminology
from r6.redaction import apply_redaction
from r6.terminology import LOINC, SNOMED

NULLFLAVOR = "http://terminology.hl7.org/CodeSystem/v3-NullFlavor"

_TX = {
    (LOINC, "66374-0"):
        "Do you have allergies to medications, food, or any vaccine [PhenX]",
    (LOINC, "LA32-8"): "No",
    (SNOMED, "1300212001"): "Allergy status",
    (SNOMED, "373067005"): "No",
    (LOINC, "LA10030-7"): "None known",
    (LOINC, "LA9134-3"): "None or none known",
    (LOINC, "LA4715-4"): "None or not known",
}


@pytest.fixture(autouse=True)
def _tx_fhir_org(monkeypatch):
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda system, code: _TX.get((system, code)))


def _obs(qs, qc, vs, vc):
    return {"resourceType": "Observation", "id": "o", "status": "final",
            "code": {"coding": [{"system": qs, "code": qc}]},
            "valueCodeableConcept": {"coding": [{"system": vs, "code": vc}]}}


@pytest.mark.parametrize("answer", ["LA10030-7", "LA9134-3", "LA4715-4"])
def test_round2_loinc_answers_are_qualified(answer):
    out = apply_redaction(_obs(LOINC, "52571-7", LOINC, answer))
    value = out["valueCodeableConcept"]
    assert "display" not in value["coding"][0]
    assert value["text"].startswith("Recorded entry: ")


@pytest.mark.parametrize("code", ["NASK", "UNK", "ASKU", "NI", "NA", "NAV"])
def test_nullflavor_codes_get_no_label(code):
    out = apply_redaction({"resourceType": "AllergyIntolerance", "id": "a",
                           "code": {"coding": [{"system": NULLFLAVOR,
                                                "code": code}]}})
    assert "text" not in out["code"]
    assert "display" not in out["code"]["coding"][0]


@pytest.mark.xfail(strict=False, reason="follow-up: question-aware "
                   "qualification of a yes/no answer to an allergy question")
@pytest.mark.parametrize("q,a", [((LOINC, "66374-0"), (LOINC, "LA32-8")),
                                 ((SNOMED, "1300212001"), (SNOMED, "373067005"))])
def test_a_no_to_an_allergy_question_is_qualified(q, a):
    out = apply_redaction(_obs(q[0], q[1], a[0], a[1]))
    assert out["valueCodeableConcept"].get("text") != "No", json.dumps(out)


# Must-keep: fixed after round 3 by refusing a negation only when it is
# about allergies or sensitivities.
@pytest.mark.parametrize("label", [
    "Hantavirus sin nombre Ab [Presence] in Serum",   # LOINC 33549-7
    "Sin Nombre virus encephalitis",                  # SNOMED 1177044001
    "None detected",                                  # LOINC LA19544-8
    "Nil by mouth",                                   # SNOMED 182923009
])
def test_real_labels_are_not_refused(label):
    assert not terminology._ABSENCE_WORDING.search(label)


@pytest.mark.parametrize("label", ["Nilotinib", "Nonsteroidal anti-"
                                   "inflammatory agent", "Ketoconazole",
                                   "Allergy to penicillin", "Natural killer "
                                   "cells", "Nonessential amino acid"])
def test_control_substance_names_keep_their_labels(label):
    assert not terminology._ABSENCE_WORDING.search(label)
