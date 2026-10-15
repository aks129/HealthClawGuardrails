"""Security review of PR #957, round 2 (head 40fd790).

40fd790 closes the round-1 bypasses in three layers: a SNOMED code must
fullmatch [1-9][0-9]{5,17} before lookup, 160244002 is an absence concept,
and a resolver answer matching _ABSENCE_WORDING is never a label. It also
drops a malformed SNOMED code in redaction (`_clean_coding`).

R2-F1 (failed at 40fd790, fixed since): LOINC answers that state an
absence in words the pattern did not cover. tx.fhir.org (live, 2026-10-14) names LA10030-7
"None known" and LA9134-3 "None or none known". An Observation for 52571-7
("Known allergies or adverse drug reactions [CARE]") answered with either
one reached every consumer as "Known allergies ...: None known", a
bare, unqualified "no known allergies" statement. They are now a closed
set of LOINC absence answers (terminology.LOINC_ABSENCE_ANSWERS) written as
a qualified entry, and the wording check on resolver answers is broader.

The item-4 block shows that valid SNOMED ids survive redaction and that
only non-ids are dropped. It passes.

Synthetic data only. "Zelda Quornwhistle" is an invented canary name.
"""

from __future__ import annotations

import json
import re

import pytest

from r6 import terminology
from r6.redaction import apply_patient_controlled_redaction, apply_redaction
from r6.terminology import LOINC, SNOMED

CANARY = "Zelda Quornwhistle"

#: Live tx.fhir.org displays, captured 2026-10-14.
_TX = {
    (LOINC, "52571-7"): "Known allergies or adverse drug reactions [CARE]",
    (LOINC, "LA10030-7"): "None known",
    (LOINC, "LA9134-3"): "None or none known",
    (SNOMED, "91936005"): "Allergy to penicillin",
    (SNOMED, "591121000124109"): "No known overcrowding in residence",
    (SNOMED, "787481004"): "No known medications",
}

_ABSENCE_STATEMENT = re.compile(
    r"(?<!Recorded entry: ')(no known|none known|not asked|\bnkd?a\b)",
    re.IGNORECASE)


@pytest.fixture(autouse=True)
def _tx_fhir_org(monkeypatch):
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda system, code: _TX.get((system, code)))


def _known_allergies_obs(answer):
    return {
        "resourceType": "Observation", "id": "obs957", "status": "final",
        "subject": {"reference": "Patient/pt-957"},
        "code": {"coding": [{"system": LOINC, "code": "52571-7"}]},
        "valueCodeableConcept": {
            "coding": [{"system": LOINC, "code": answer,
                        "display": f"{CANARY}: none"}],
            "text": f"{CANARY} has none"},
    }


# --- R2-F1: absence wording the pattern does not catch -----------------------

@pytest.mark.parametrize("answer", ["LA10030-7", "LA9134-3"])
def test_r2_f1_loinc_none_known_answer_gets_qualified_text(answer):
    out = apply_redaction(_known_allergies_obs(answer))
    assert CANARY not in json.dumps(out)
    value = out["valueCodeableConcept"]
    blob = json.dumps(value)
    assert not _ABSENCE_STATEMENT.search(blob), blob
    assert "display" not in value["coding"][0]
    assert value["text"] == terminology.absence_text(LOINC, answer)


@pytest.mark.parametrize("answer", ["LA10030-7", "LA9134-3"])
def test_r2_f1_through_read_and_search(app, client, tenant_id, tenant_headers,
                                       answer):
    from models import db
    from r6.models import R6Resource
    obs = _known_allergies_obs(answer)
    with app.app_context():
        db.session.add(R6Resource("Observation", json.dumps(obs),
                                  resource_id=obs["id"], tenant_id=tenant_id))
        db.session.commit()
    for path in ("/r6/fhir/Observation/obs957", "/r6/fhir/Observation"):
        resp = client.get(path, headers=tenant_headers)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert CANARY not in body
        assert not _ABSENCE_STATEMENT.search(body), body[:600]


# --- the pattern itself: wordings it lets through -----------------------------

@pytest.mark.parametrize("wording", [
    "None known", "None or none known", "Nil known allergies",
    "Allergies: none", "NKFA",
    "Keine bekannten Allergien", "Aucune allergie connue",
    "Sin alergias conocidas",
])
def test_absence_wording_pattern_covers_paraphrases(wording):
    """Only the first two have been seen from a live resolver. The rest
    are hypothetical today, since the resolver asks for no display
    language; the check refuses them all the same."""
    assert terminology._ABSENCE_WORDING.search(wording), wording


# --- item 4: what _clean_coding now drops ------------------------------------

@pytest.mark.parametrize("code", [
    "716186003", "91936005", "1631000175102", "160244002",
    "591121000124109",      # US extension id, 15 digits
    "123456",               # 6 digits, the shortest valid id
    "123456789012345678",   # 18 digits, the longest
    " 91936005\n",          # padded: kept (stripped where keyed)
])
def test_item4_valid_snomed_ids_survive_every_redaction(code):
    res = {"resourceType": "Condition", "id": "c957",
           "code": {"coding": [{"system": SNOMED, "code": code}]},
           "meta": {"tag": [{"system": SNOMED, "code": code}]}}
    for out in (apply_redaction(res),
                apply_patient_controlled_redaction(res, "pt-957")):
        assert out["code"]["coding"][0]["code"] == code
        assert out["meta"]["tag"][0]["code"] == code


def test_item4_int_snomed_code_survives_as_its_digits():
    res = {"resourceType": "Condition", "id": "c957",
           "code": {"coding": [{"system": SNOMED, "code": 91936005}]}}
    assert apply_redaction(res)["code"]["coding"][0]["code"] == "91936005"


@pytest.mark.parametrize("code", [
    "0716186003", "716186003|No known allergy|", "716186003+716186003",
    "128045006:{363698007=56459004}",   # a post-coordinated expression
    "12345",                            # 5 digits
    "1234567890123456789",              # 19 digits
    f"716186003 {CANARY}",
])
def test_item4_non_ids_are_dropped(code):
    res = {"resourceType": "Condition", "id": "c957",
           "code": {"coding": [{"system": SNOMED, "code": code}]}}
    out = apply_redaction(res)
    assert "code" not in out["code"]["coding"][0]
    assert CANARY not in json.dumps(out)


@pytest.mark.parametrize("system,code", [
    (LOINC, "LA10030-7"), (LOINC, "4548-4"),
    ("http://hl7.org/fhir/sid/icd-10-cm", "Z88.0"),
    ("urn:example:local", "0716186003"),
])
def test_item4_other_systems_untouched(system, code):
    res = {"resourceType": "Condition", "id": "c957",
           "code": {"coding": [{"system": system, "code": code}]}}
    assert apply_redaction(res)["code"]["coding"][0]["code"] == code


def test_non_allergy_no_known_concepts_get_no_bare_label():
    """Layer 3 also covers "No known medications" and similar concepts that
    are not in ABSENCE_CONCEPTS. They get no label, and no qualified text
    either."""
    for code in ("787481004", "591121000124109"):
        out = apply_redaction({"resourceType": "Condition", "id": "c",
                               "code": {"coding": [{"system": SNOMED,
                                                    "code": code}]}})
        assert not _ABSENCE_STATEMENT.search(json.dumps(out))
