"""Security review of the #1018 fix (PR #1101): the per-record fail-closed
in `_summarize_bundle`.

Three rows:

- Record shapes that are not strings where a string is expected, planted
  in the real engine and read through the real client in a full chat turn.
  None of them may lose the summary, mark a list unread, or carry planted
  text to the model.
- A row that raises, wherever in the row it raises, beside each allergy
  entry the summary treats specially (a "no known allergy" entry, a "not
  asked" status entry, a withdrawn entry). The list must read as longer
  than shown. It must never quote "no known allergies" or say the records
  don't say whether there are allergies. "No known allergies" is never
  inferred, least of all from a list with a row missing.
- A gap that predated #1101: `valueQuantity.value` was copied to the model
  as whatever the feed put there. It now passes only as a number, on every
  type the summary lists.

Synthetic data only.
"""

from __future__ import annotations

import json
import logging

import pytest

from careagents import agent
from careagents.agent import (ALLERGY_STATUS_STATEMENT, _execute_tool,
                              _summarize_bundle)
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_first_answer import _first_turn
from tests.test_sec_945_first_answer import _plant

pytestmark = pytest.mark.extension

PATIENT = {"reference": "Patient/demo-patient-rivera"}
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
#: Planted in every hostile field; must never reach the model or a log.
CANARY = "QZX1101CANARY"


def _med(rid, rt="MedicationRequest", **fields):
    res = {"resourceType": rt, "id": rid, "subject": PATIENT,
           "intent": "order", "status": "active",
           "medicationCodeableConcept": {"coding": [
               {"system": RXNORM, "code": "860975"}]}}
    res.update(fields)
    return res


def _allergy(rid, code="91936005", **fields):
    res = {"resourceType": "AllergyIntolerance", "id": rid,
           "patient": PATIENT,
           "code": {"coding": [{"system": SNOMED, "code": code}]}}
    res.update(fields)
    return res


# --- 1. hostile shapes through the real client and engine --------------------

SHAPES = {
    "status-int": _med("s-int", status=5),
    "status-bool": _med("s-bool", status=True),
    "status-nested": _med("s-nest", status={"a": [{"b": CANARY}]}),
    "statement-status-list": _med("s-ms", rt="MedicationStatement",
                                  status=[CANARY]),
    "med-concept-list": _med("s-mcc", medicationCodeableConcept=[CANARY]),
    "med-coding-object": _med("s-cod", medicationCodeableConcept={
        "coding": {"code": CANARY}}),
    "med-text-list": _med("s-txt", medicationCodeableConcept={
        "text": [CANARY]}),
    "med-reference-list": _med("s-ref", medicationCodeableConcept=None,
                               medicationReference={"reference": [CANARY]}),
    "med-reference-string": _med("s-refs", medicationCodeableConcept=None,
                                 medicationReference=f"Medication/{CANARY}"),
    "effective-list": _med("s-eff", effectiveDateTime=[CANARY]),
    "quantity-list": _med("s-vq", valueQuantity=[CANARY]),
    "quantity-unit-list": _med("s-vqu", valueQuantity={
        "value": 1, "unit": [CANARY], "code": {"c": CANARY}}),
    "allergy-statuses-list": _allergy("s-acs", clinicalStatus=[CANARY],
                                      verificationStatus=[CANARY]),
    "allergy-code-list": _allergy("s-acl", code=None) | {"code": [CANARY]},
    "allergy-recorded-list": _allergy("s-ard", recordedDate=[CANARY]),
    "condition-statuses": {
        "resourceType": "Condition", "id": "s-cond", "subject": PATIENT,
        "status": [CANARY], "clinicalStatus": {"coding": {"code": [CANARY]}},
        "verificationStatus": [{"coding": [CANARY]}],
        "code": {"coding": [{"system": SNOMED, "code": "38341003"}]}},
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_a_hostile_shape_never_loses_the_summary_or_reaches_the_model(
        cfg, svc, monkeypatch, caplog, shape):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch, allergy=True)
    _plant(chain, SHAPES[shape])
    out, _ = _first_turn(chain, monkeypatch)
    for key in ("conditions", "medications", "medication_statements",
                "allergies"):
        assert isinstance(out[key], list), (key, out[key])
    assert "note" not in out, out
    assert "Type 2 diabetes mellitus" in [c.get("name")
                                          for c in out["conditions"]]
    assert "Allergy to penicillin" in [a.get("name")
                                       for a in out["allergies"]]
    assert CANARY not in json.dumps(out)
    assert CANARY not in caplog.text


# --- 2. a skipped row beside the allergy entries read specially --------------

NKA = _allergy("nka", code="716186003", recordedDate="2024-01-02")
NOT_ASKED = _allergy("not-asked", code="1631000175102")
WITHDRAWN_NKA = _allergy("nka-gone", code="716186003", verificationStatus={
    "coding": [{"code": "entered-in-error"}]})
BOOM = _allergy("boom", code="91936005")


@pytest.mark.parametrize("raise_in", ["_is_allergy_status", "_nka_kinds",
                                      "_carries_absence_code"])
@pytest.mark.parametrize("siblings", [
    [NKA], [NOT_ASKED], [WITHDRAWN_NKA], [NKA, NOT_ASKED],
    [WITHDRAWN_NKA, NOT_ASKED]],
    ids=["nka", "not-asked", "withdrawn-nka", "nka+not-asked",
         "withdrawn+not-asked"])
@pytest.mark.parametrize("boom_first", [True, False],
                         ids=["boom-first", "boom-last"])
def test_a_skipped_allergy_row_never_reads_as_no_allergies(
        monkeypatch, caplog, raise_in, siblings, boom_first):
    caplog.set_level(logging.DEBUG)
    real = getattr(agent, raise_in)

    def raising(res, *a, **k):
        if res.get("id") == "boom":
            raise TypeError(f"unhashable type: {CANARY}")
        return real(res, *a, **k)
    monkeypatch.setattr(agent, raise_in, raising)
    rows = [BOOM, *siblings] if boom_first else [*siblings, BOOM]
    out = _summarize_bundle({"entry": [{"resource": r} for r in rows]},
                            limit=agent.SUMMARY_LIMIT)
    text = json.dumps(out)
    # Never quoted as a fact, never "the records don't say".
    assert "lists no known" not in text, out
    assert ALLERGY_STATUS_STATEMENT not in text, out
    assert "No allergy entries can be shown" not in text, out
    # Always one allergy entry on file that cannot be read: present, never
    # absent, and never a list cut short (#1101 QA).
    unreadable = [i for i in out if i.get("unreadable")]
    assert [(i["type"], i["name"]) for i in unreadable] == [
        ("AllergyIntolerance", "unreadable record")], out
    assert "one entry has no readable name here" in unreadable[0]["note"]
    assert not any(i.get("truncated") for i in out), out
    # Beside it, a "no known allergy" entry reads as a disagreement.
    for i in out:
        if i.get("no_known_allergy_record"):
            assert "disagrees" in i["statement"], i
    assert ("1 AllergyIntolerance record(s) listed as unreadable (TypeError)"
            in caplog.text)
    assert CANARY not in caplog.text and "boom" not in caplog.text


# --- 3. valueQuantity.value reaches the model only as a number ---------------

@pytest.mark.parametrize("rt", ["Observation", "MedicationRequest",
                                "AllergyIntolerance"])
def test_a_free_text_quantity_value_never_reaches_the_model(
        cfg, svc, monkeypatch, rt):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch, allergy=True)
    _plant(chain, {"resourceType": rt, "id": "vq-text",
                   "subject": PATIENT, "status": "final",
                   "effectiveDateTime": "2026-10-01",
                   "code": {"coding": [{"system": LOINC, "code": "4548-4"}]},
                   "valueQuantity": {"value": f"{CANARY} Jane Q Synthetic",
                                     "code": "%"}})
    out = _execute_tool(chain.hc, TENANT, "search_records",
                        {"resource_type": rt}, [])
    assert CANARY not in out, out


@pytest.mark.parametrize("value", [7.1, 7])
def test_a_numeric_quantity_value_still_reaches_the_model(value):
    # The guard is a type check, not a block: a number still passes.
    out = _summarize_bundle({"entry": [{"resource": {
        "resourceType": "Observation", "id": "vq-num", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": "4548-4"}]},
        "valueQuantity": {"value": value, "code": "%"}}}]})
    assert out[0]["value"] == f"{value} %", out


@pytest.mark.parametrize("value", [True, "7.1", [7.1], {"v": 7.1}])
def test_a_quantity_value_that_is_not_a_number_is_no_value(value):
    # A bool is an int to Python, and never a measurement.
    out = _summarize_bundle({"entry": [{"resource": {
        "resourceType": "Observation", "id": "vq-odd", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": "4548-4"}]},
        "valueQuantity": {"value": value, "code": "%"}}}]})
    assert "value" not in out[0], out


# --- 4. an unreadable row whose type is not one we list ----------------------

@pytest.mark.parametrize("sibling", [NKA, NOT_ASKED, WITHDRAWN_NKA],
                         ids=["nka", "not-asked", "withdrawn-nka"])
def test_an_unreadable_row_of_another_type_never_lets_nka_be_quoted(
        monkeypatch, sibling):
    # Defense in depth, not reachable through the engine's API today
    # (ingest and PUT refuse a resourceType that does not match): a row
    # that raises with a type we do not list is listed as type "record",
    # and still counts as a recorded allergy.
    real = agent._carries_absence_code

    def raising(res, *a, **k):
        if res.get("id") == "boom":
            raise TypeError(f"unhashable type: {CANARY}")
        return real(res, *a, **k)
    monkeypatch.setattr(agent, "_carries_absence_code", raising)
    out = _summarize_bundle({"entry": [
        {"resource": sibling},
        {"resource": {"resourceType": f"AllergyIntolerance {CANARY}",
                      "id": "boom"}}]}, limit=agent.SUMMARY_LIMIT)
    text = json.dumps(out)
    assert CANARY not in text, out
    assert "lists no known" not in text, out
    assert ALLERGY_STATUS_STATEMENT not in text, out
    assert "No allergy entries can be shown" not in text, out
    assert [i["type"] for i in out if i.get("unreadable")] == ["record"], out
