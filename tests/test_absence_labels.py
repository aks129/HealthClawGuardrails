"""An absence concept is written as a recorded entry, never as a name (#950).

SNOMED "no known allergy" (716186003 and its six descendants) and "patient
not asked" (1631000175102) are statements about allergies, not names. The
opt-in terminology resolver labels 716186003 "No known allergy", and
`label_codings` used to write that into `coding.display` and
`CodeableConcept.text` on every read and search, local and upstream. So
every MCP tool handed an agent "No known allergy" as a bare name: a fact
about the person that no one attested. AGENTS.md: "no known allergies" is
never inferred.

The engine now writes no `display` for those codes, and writes a `text`
from its own table keyed by code that reads as a record entry. The table
is in r6/terminology.py, the one source the brief engine imports and the
CareAgents mirror is held equal to (tests/test_careagents_nka_946.py).

Synthetic data only. "Zelda Quornwhistle" is an invented canary name.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from r6 import terminology
from r6.redaction import apply_patient_controlled_redaction, apply_redaction
from r6.terminology import (ABSENCE_CONCEPTS, ALLERGY_STATUS_CONCEPTS,
                            NKA_CONCEPTS, SNOMED, is_absence_coding,
                            label_codings)

NKA_CODES = ("716186003", "409137002", "428197003", "428607008",
             "429625007", "716220001", "1003774007")
STATUS = "1631000175102"
ABSENCE_CODES = NKA_CODES + (STATUS,)
PENICILLIN = "91936005"
CANARY = "Zelda Quornwhistle"

#: The three spellings of SNOMED's system seen in the wild.
SYSTEMS = (SNOMED, "urn:oid:2.16.840.1.113883.6.96", f"  {SNOMED}\n")


@pytest.fixture(autouse=True)
def _resolver_names_every_absence(monkeypatch):
    """Production's resolver: it labels these codes as bare names."""
    monkeypatch.setattr(
        "r6.terminology_resolver.resolve",
        lambda system, code: "No known allergy" if code in ABSENCE_CODES
        else None)


def _qualified(code):
    return terminology.absence_text(SNOMED, code)


def _no_bare_name(obj):
    """No absence name appears anywhere except inside our qualified text."""
    blob = json.dumps(obj)
    for code in ABSENCE_CODES:
        blob = blob.replace(json.dumps(_qualified(code))[1:-1], "")
    assert "no known" not in blob.lower(), blob
    assert "not asked" not in blob.lower(), blob


# --- one source of truth ----------------------------------------------------

def test_the_concept_sets_are_the_closed_lists():
    assert NKA_CONCEPTS == {(SNOMED, c) for c in NKA_CODES}
    assert ALLERGY_STATUS_CONCEPTS == {(SNOMED, STATUS)}
    assert ABSENCE_CONCEPTS == NKA_CONCEPTS | ALLERGY_STATUS_CONCEPTS


def test_every_absence_concept_has_qualified_text_and_nothing_else_does():
    for system, code in ABSENCE_CONCEPTS:
        text = terminology.absence_text(system, code)
        assert text.startswith("Recorded entry: ")
        assert "confirm with a clinician" in text
    assert terminology.absence_text(SNOMED, PENICILLIN) is None


def test_patient_not_asked_is_never_worded_as_no_allergies():
    assert "no known" not in _qualified(STATUS).lower()


def test_the_brief_engine_has_no_copy_of_its_own():
    from r6.brief import engine
    assert not hasattr(engine, "_ABSENCE_CONCEPTS")


# --- what label_codings writes ----------------------------------------------

@pytest.mark.parametrize("system", SYSTEMS)
@pytest.mark.parametrize("code", ABSENCE_CODES)
def test_an_absence_code_gets_qualified_text_and_no_display(system, code):
    concept = {"coding": [{"system": system, "code": code}]}
    label_codings(concept)
    assert "display" not in concept["coding"][0]
    assert concept["text"] == _qualified(code)
    _no_bare_name(concept)


def test_an_int_code_is_keyed_as_its_digits():
    concept = {"coding": [{"system": SNOMED, "code": int("716186003")}]}
    label_codings(concept)
    assert "display" not in concept["coding"][0]
    assert concept["text"] == _qualified("716186003")


@pytest.mark.parametrize("code", [True, ["716186003"], {"c": 1}])
def test_a_code_that_is_not_a_code_is_never_an_absence(code):
    coding = {"system": SNOMED, "code": code}
    assert is_absence_coding(coding) is False
    concept = {"coding": [coding]}
    label_codings(concept)
    assert "text" not in concept and "display" not in coding


def test_a_real_allergen_beside_an_absence_keeps_its_label():
    """Mixed codings: the record names a substance, so the text is the
    substance, never the absence sentence."""
    for order in ((PENICILLIN, "716186003"), ("716186003", PENICILLIN)):
        concept = {"coding": [{"system": SNOMED, "code": c} for c in order]}
        label_codings(concept)
        by_code = {c["code"]: c for c in concept["coding"]}
        assert by_code[PENICILLIN]["display"] == "Allergy to penicillin"
        assert "display" not in by_code["716186003"]
        assert concept["text"] == "Allergy to penicillin"


def test_an_unknown_code_beside_an_absence_gets_no_absence_text():
    """An unlabelled code may be a real allergen: the absence sentence would
    then describe a record that names a substance."""
    concept = {"coding": [{"system": SNOMED, "code": "716186003"},
                          {"system": "urn:example:local", "code": "Z1"}]}
    label_codings(concept)
    assert "text" not in concept
    _no_bare_name(concept)


# --- through redaction: upstream text is stripped, ours is written ----------

def _nka_allergy(code="716186003", system=SNOMED, rid="nka-950"):
    return {
        "resourceType": "AllergyIntolerance", "id": rid,
        "patient": {"reference": "Patient/pt-950"},
        "code": {"coding": [{"system": system, "code": code,
                             "display": f"No known allergy - {CANARY}"}],
                 "text": f"{CANARY} says no known allergies"},
    }


@pytest.mark.parametrize("code", ABSENCE_CODES)
def test_redaction_strips_upstream_text_and_writes_ours(code):
    out = apply_redaction(_nka_allergy(code))
    assert CANARY not in json.dumps(out)
    assert "display" not in out["code"]["coding"][0]
    assert out["code"]["text"] == _qualified(code)
    _no_bare_name(out)


def test_the_share_bundle_carries_no_absence_name():
    out = apply_patient_controlled_redaction(_nka_allergy(), "pt-950")
    assert CANARY not in json.dumps(out)
    _no_bare_name(out)


# --- every read exit: local and upstream, read and search -------------------

def _plant(app, tenant_id, resource):
    from models import db
    from r6.models import R6Resource
    with app.app_context():
        db.session.add(R6Resource(resource["resourceType"],
                                  json.dumps(resource),
                                  resource_id=resource["id"],
                                  tenant_id=tenant_id))
        db.session.commit()


@pytest.mark.parametrize("system", SYSTEMS)
@pytest.mark.parametrize("code", ["716186003", STATUS])
def test_local_read_and_search(app, client, tenant_id, tenant_headers,
                               system, code):
    _plant(app, tenant_id, _nka_allergy(code, system))
    read = client.get("/r6/fhir/AllergyIntolerance/nka-950",
                      headers=tenant_headers)
    assert read.status_code == 200, read.get_data(as_text=True)
    search = client.get("/r6/fhir/AllergyIntolerance", headers=tenant_headers)
    assert search.status_code == 200
    [entry] = search.get_json()["entry"]
    for resource in (read.get_json(), entry["resource"]):
        assert resource["code"]["text"] == _qualified(code)
        assert "display" not in resource["code"]["coding"][0]
    for resp in (read, search):
        assert CANARY not in resp.get_data(as_text=True)
        _no_bare_name(resp.get_json())


@pytest.mark.parametrize("system", SYSTEMS)
@pytest.mark.parametrize("code", ["716186003", STATUS])
def test_upstream_read_and_search(client, tenant_headers, system, code):
    proxy = MagicMock()
    proxy.read.return_value = (_nka_allergy(code, system), 200)
    proxy.search.return_value = ({
        "resourceType": "Bundle", "type": "searchset", "total": 1,
        "entry": [{"resource": _nka_allergy(code, system)}]}, 200)
    with patch("r6.routes.get_proxy_for_request", return_value=proxy):
        read = client.get("/r6/fhir/AllergyIntolerance/nka-950",
                          headers=tenant_headers)
        search = client.get("/r6/fhir/AllergyIntolerance",
                            headers=tenant_headers)
    assert read.status_code == 200 and search.status_code == 200
    [entry] = search.get_json()["entry"]
    for resource in (read.get_json(), entry["resource"]):
        assert resource["code"]["text"] == _qualified(code)
        assert "display" not in resource["code"]["coding"][0]
    for resp in (read, search):
        assert CANARY not in resp.get_data(as_text=True)
        _no_bare_name(resp.get_json())


# --- SDC prefill: accepted, and pinned so it stays a decision ---------------

def test_intake_prefill_carries_the_qualified_text_and_never_attests_nka():
    """The CTO accepted this explicitly (#950 design note, A0 call 6): an
    intake form prefilled from a redacted NKA record shows the qualified
    sentence as the allergen. A person reviews the form before submitting,
    and that submission is the human attestation. The attestation item
    itself is never answered by population."""
    from r6.sdc.intake import intake_questionnaire
    from r6.sdc.populate import populate_questionnaire

    patient = {"resourceType": "Patient", "id": "pt-950"}
    qr, _issues = populate_questionnaire(
        intake_questionnaire(), patient, [apply_redaction(_nka_allergy())])

    def walk(items):
        for item in items:
            yield item
            yield from walk(item.get("item", []))
            for answer in item.get("answer", []):
                yield from walk(answer.get("item", []))

    items = list(walk(qr["item"]))
    allergens = [a.get("valueString") for i in items
                 if i["linkId"] == "allergies.item.allergen"
                 for a in i.get("answer", [])]
    assert allergens == [_qualified("716186003")]
    [attest] = [i for i in items
                if i["linkId"] == "allergies.no-known-allergies"]
    assert "answer" not in attest
    assert CANARY not in json.dumps(qr)
