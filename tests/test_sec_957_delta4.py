"""Security review of PR #957, delta 472f0fc..830cf7a.

ab5ba29 narrowed _ABSENCE_WORDING to allergy context. Two live resolver
answers that round 3's pattern refused are labels again (tx.fhir.org,
2026-10-14):

- SNOMED 260413007 "None": an AllergyIntolerance coded with it reads back
  as `text: "None"`, an allergy named "None". FAILS at 830cf7a.
- LOINC LA14761-3 "Not asked": a status, not an absence. Low.

260413007 is fixed: it is in NKA_CONCEPTS and a bare "None"/"Nil" label is
refused. LA14761-3 was added to ALLERGY_STATUS_CONCEPTS and taken back out
(delta 5): it answers any question, so allergy text was false on others.
Its xfail stands.

Synthetic data only.
"""

from __future__ import annotations

import pytest

from r6.redaction import apply_redaction
from r6.terminology import LOINC, SNOMED

_TX = {(SNOMED, "260413007"): "None", (LOINC, "LA14761-3"): "Not asked",
       (LOINC, "52571-7"): "Known allergies or adverse drug reactions [CARE]",
       (LOINC, "LA19544-8"): "None detected"}


@pytest.fixture(autouse=True)
def _tx_fhir_org(monkeypatch):
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda system, code: _TX.get((system, code)))


def test_an_allergy_coded_none_is_never_named_none():
    out = apply_redaction({
        "resourceType": "AllergyIntolerance", "id": "a",
        "code": {"coding": [{"system": SNOMED, "code": "260413007"}]}})
    assert out["code"].get("text") != "None"
    assert out["code"]["coding"][0].get("display") != "None"


@pytest.mark.xfail(strict=False, reason="Low: a status, not an absence; "
                   "LA14761-3 is generic, so no allergy text fits it")
def test_loinc_not_asked_answer_is_not_bare():
    out = apply_redaction({
        "resourceType": "Observation", "id": "o", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": "52571-7"}]},
        "valueCodeableConcept": {"coding": [{"system": LOINC,
                                             "code": "LA14761-3"}]}})
    assert out["valueCodeableConcept"].get("text") != "Not asked"


def test_control_none_detected_keeps_its_label():
    out = apply_redaction({
        "resourceType": "Observation", "id": "o", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": "52571-7"}]},
        "valueCodeableConcept": {"coding": [{"system": LOINC,
                                             "code": "LA19544-8"}]}})
    assert out["valueCodeableConcept"]["text"] == "None detected"
