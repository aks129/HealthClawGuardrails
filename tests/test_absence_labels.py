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
                            LOINC, LOINC_ABSENCE_ANSWERS, NKA_CONCEPTS,
                            SNOMED, is_absence_coding, label_codings)

#: LOINC answers stating an absence, confirmed by tx.fhir.org $lookup
#: 2026-10-14 (#957 round 2).
LOINC_ANSWERS = {"LA10030-7": "none known", "LA9134-3": "none known",
                 "LA4715-4": "none or not known"}

NKA_CODES = ("716186003", "409137002", "428197003", "428607008",
             "429625007", "716220001", "1003774007",
             # Inactive since 2016-07-31; legacy feeds still carry it.
             "160244002",
             # "None" (qualifier value): an allergy coded with it was named
             # "None" (#957 delta 4).
             "260413007")
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
    assert LOINC_ABSENCE_ANSWERS == {(LOINC, c) for c in LOINC_ANSWERS}
    assert ABSENCE_CONCEPTS == (NKA_CONCEPTS | ALLERGY_STATUS_CONCEPTS
                                | LOINC_ABSENCE_ANSWERS)


@pytest.mark.parametrize("code,words", sorted(LOINC_ANSWERS.items()))
def test_a_loinc_absence_answer_gets_qualified_text_and_no_display(
        monkeypatch, code, words):
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda s, c: "None known")
    concept = {"coding": [{"system": LOINC, "code": code,
                           "display": "None known"}]}
    label_codings(concept)
    assert "display" not in concept["coding"][0]
    assert concept["text"] == (f"Recorded entry: '{words}'. Not a confirmed "
                               "absence; confirm with a clinician.")


def test_every_absence_concept_has_qualified_text_and_nothing_else_does():
    for system, code in ABSENCE_CONCEPTS:
        text = terminology.absence_text(system, code)
        assert text.startswith("Recorded entry: ")
        assert "confirm with a clinician" in text
    assert terminology.absence_text(SNOMED, PENICILLIN) is None


#: Absence codes that are generic answers, used on questions about anything.
GENERIC_ABSENCE = [(SNOMED, "260413007"), (LOINC, "LA10030-7"),
                   (LOINC, "LA9134-3"), (LOINC, "LA4715-4")]


@pytest.mark.parametrize("system,code", GENERIC_ABSENCE)
def test_a_generic_absence_answer_says_nothing_about_allergies(system, code):
    """These answer any question, so their text must be topic-neutral. On
    "Smoking status" a sentence about allergies would be false (#957 delta
    5, where LOINC LA14761-3 "Not asked" was taken back out of the set)."""
    obs = {"resourceType": "Observation", "id": "o", "status": "final",
           "code": {"coding": [{"system": LOINC, "code": "72166-2"}]},
           "valueCodeableConcept": {"coding": [{"system": system,
                                                "code": code}]}}
    value = apply_redaction(obs)["valueCodeableConcept"]
    assert value["text"] == terminology.absence_text(system, code)
    assert value["text"].startswith("Recorded entry: ")
    assert "allerg" not in value["text"].lower()


def test_a_generic_not_asked_answer_is_not_an_allergy_status():
    assert (LOINC, "LA14761-3") not in ABSENCE_CONCEPTS


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


def test_an_absence_coding_that_arrives_with_a_display_loses_it():
    """QA Q1 on #957: apply_redaction strips display first, so only a
    direct caller of label_codings exercises the pop."""
    concept = {"coding": [{"system": SNOMED, "code": "716186003",
                           "display": "No known allergy"}]}
    label_codings(concept)
    assert "display" not in concept["coding"][0]
    assert concept["text"] == terminology.absence_text(SNOMED, "716186003")


# --- layer 1: a SNOMED code is a well-formed concept id, or it is not looked up

@pytest.mark.parametrize("code", [
    "0716186003", "716186003|No known allergy|", "716186003+716186003",
    "12345", "1234567890123456789", "no known allergies", "71618600３"])
def test_a_malformed_snomed_code_never_reaches_the_resolver(monkeypatch,
                                                            code):
    """tx.fhir.org names "0716186003" and compositional expressions as
    absences (#957 security F2), and free text sent there leaves the
    deployment. Neither is looked up, labelled, or counted as a miss."""
    asked = []
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda s, c: asked.append(c) or "No known allergy")
    terminology.reset_unlabelled()
    assert terminology.lookup(SNOMED, code) is None
    concept = {"coding": [{"system": SNOMED, "code": code}]}
    label_codings(concept)
    assert "display" not in concept["coding"][0] and "text" not in concept
    assert asked == []
    assert terminology.unlabelled_codes() == []


def test_a_well_formed_snomed_code_still_reaches_the_resolver(monkeypatch):
    asked = []
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda s, c: asked.append(c) or "Myocardial infarction")
    # A code with no static row (195967001 has one since #959's labels).
    assert terminology.lookup(SNOMED, " 22298006\n") == "Myocardial infarction"
    assert asked == ["22298006"]


# --- layer 3: a resolver answer that reads as an absence is never a label ----

@pytest.mark.parametrize("label", [
    "No known allergies", "No known drug allergy", "NKDA", "NKA",
    "No drug allergy", "No allergy to latex", "Allergy status not asked",
    "None known", "None or not known", "None or none known", "Nil known",
    "None", " none ", "NIL",
    "Nil known allergies", "Allergies: none", "NKFA",
    "No known sensitivities", "Drug intolerance: none",
    "Adverse reaction: nil", "No known medications",
    "Keine bekannten Allergien", "Aucune allergie connue",
    "Sin alergias conocidas", "Nessuna allergia nota",
    "Ninguna alergia conocida"])
def test_a_resolver_absence_answer_for_an_unlisted_code_is_never_a_label(
        monkeypatch, label):
    """A code missing from ABSENCE_CONCEPTS (here a synthetic, well-formed
    id) that the resolver names as an absence still gets no display and no
    text: the set can lag SNOMED, the check on the answer cannot."""
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda s, c: label)
    assert terminology.lookup(SNOMED, "999999001") is None
    concept = {"coding": [{"system": SNOMED, "code": "999999001"}]}
    label_codings(concept)
    assert "display" not in concept["coding"][0] and "text" not in concept


@pytest.mark.parametrize("label", [
    "Allergy to penicillin", "Non-allergic rhinitis", "Known allergy",
    "Nodular goiter", "Nilotinib", "Sinus tachycardia",
    "Nonsteroidal anti-inflammatory agent", "Ketoconazole",
    # Real labels the round-2 check refused (#957 round 3): a negation that
    # is not about allergies is a name, and the closed sets do the rest.
    "None detected", "Nil by mouth",
    "Hantavirus sin nombre Ab [Presence] in Serum",
    "Sin Nombre virus encephalitis",
    # "None known" counts on its own only as the WHOLE label.
    "Fever, cause none known"])
def test_an_ordinary_resolver_answer_is_still_a_label(monkeypatch, label):
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda s, c: label)
    assert terminology.lookup(SNOMED, "999999001") == label


# --- the brief: a dropped code is still a recorded entry ---------------------

def test_a_condition_whose_code_redaction_dropped_reads_unnamed_not_unknown():
    """Redaction drops a malformed SNOMED code and leaves the system. That
    entry was recorded; "Unknown · Active" read like a mystery illness
    (#957 patient test)."""
    from r6.brief import engine
    cond = {"resourceType": "Condition", "id": "c",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"coding": [{"system": SNOMED,
                                 "code": "716186003|x|"}]}}
    redacted = apply_redaction(cond)
    assert redacted["code"]["coding"] == [{"system": SNOMED}]
    [field] = engine.build_problems([redacted])
    assert field.label == engine.UNLABELLED


@pytest.mark.parametrize("code", [None, {"coding": [{}]}, {"coding": []}])
def test_a_condition_with_nothing_coded_still_reads_unknown(code):
    """No code element, or codings with neither a code nor a system: there
    is nothing recorded to name."""
    from r6.brief import engine
    cond = {"resourceType": "Condition", "id": "c",
            "clinicalStatus": {"coding": [{"code": "active"}]}}
    if code is not None:
        cond["code"] = code
    [field] = engine.build_problems([apply_redaction(cond)])
    assert field.label == engine.UNKNOWN


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
