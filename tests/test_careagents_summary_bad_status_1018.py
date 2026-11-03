"""A record with a status that is not a string never drops the summary
(#1018).

Ingest stores whatever arrived, so a MedicationRequest can carry
`"status": ["active"]` or `{"value": "active"}`. `_summarize_bundle` tested
it for membership in a frozenset, raised `TypeError: unhashable type`, and
`_execute_tool` catches only HealthClawError: the whole health summary,
conditions and allergies too, was lost on every turn until the record was
removed.

A status that is not a string now reads as unknown, so the record is still
listed, with no status. The same unhashable test sat on a coding's `system`
(the unlabeled-record name) and on a LOINC coding's `code` (the unit lookup);
each is guarded the same way. And `_summarize_bundle` now fails closed per
record: a row it cannot read is left out and the list says it is longer than
shown, never complete. Nothing about the skipped row is logged but a count.

The status and the per-record backstop are driven through the real client
and the real engine, as the issue's repro was. The engine's redaction drops a
list-valued coding `system` or `code` before CareAgents sees it, so those two
guards are pinned on `_summarize_bundle` directly, with the shape a future
engine change could let through. Synthetic data only.
"""

from __future__ import annotations

import logging

import pytest

from careagents import agent
from careagents.agent import _summarize_bundle
from tests.test_beta_acceptance_rows import Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_first_answer import _first_turn
from tests.test_sec_945_first_answer import _plant

pytestmark = pytest.mark.extension

RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
PATIENT = {"reference": "Patient/demo-patient-rivera"}
#: Planted in the bad rows; must never reach a log line.
CANARY = "QZX1018CANARY"


def _med(rid, **fields):
    return {"resourceType": "MedicationRequest", "id": rid,
            "subject": PATIENT, "intent": "order",
            "medicationCodeableConcept": {"coding": [
                {"system": RXNORM, "code": "860975"}]},
            **fields}


def _bundle(*resources):
    return {"entry": [{"resource": r} for r in resources]}


def _rest_intact(out):
    """The other lists came back whole: the sample's condition and the
    planted allergy, and no list marked could_not_read."""
    assert [c["name"] for c in out["conditions"]] == [
        "Type 2 diabetes mellitus"], out
    assert [a.get("name") for a in out["allergies"]] == [
        "Allergy to penicillin"], out
    assert "note" not in out, out
    for key in ("conditions", "medications", "medication_statements",
                "allergies"):
        assert isinstance(out[key], list), (key, out[key])


# --- through the real client and engine --------------------------------------

@pytest.mark.parametrize("status", [["active"], {"value": "active"}],
                         ids=["list", "object"])
def test_a_status_that_is_not_a_string_keeps_the_summary(
        cfg, svc, monkeypatch, status):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch, allergy=True)
    _plant(chain, _med("bad-status-1018", status=status))
    out, _ = _first_turn(chain, monkeypatch)
    _rest_intact(out)
    meds = out["medications"]
    # The bad row is still listed, by name, as unknown status: neither
    # skipped (no truncated marker) nor its status passed on.
    assert not any(i.get("truncated") for i in meds), meds
    named = [i for i in meds if i.get("name") == "Metformin 500 mg"]
    assert len(named) == 2, meds
    assert sorted(i.get("status", "") for i in named) == ["", "active"], meds


def test_a_row_that_cannot_be_read_is_left_out_not_the_summary(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    # A list in medicationReference: no single guard covers it, so this is
    # the per-record backstop's own trip-wire.
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch, allergy=True)
    _plant(chain, {"resourceType": "MedicationRequest", "id": "bad-ref-1018",
                   "subject": PATIENT, "intent": "order", "status": "active",
                   "medicationReference": [f"Medication/{CANARY}"]})
    out, _ = _first_turn(chain, monkeypatch)
    _rest_intact(out)
    meds = out["medications"]
    assert [i.get("name") for i in meds if "type" in i] == [
        "Metformin 500 mg"], meds
    # Left out, so the list says it is longer than shown: never complete.
    assert meds[-1].get("truncated") is True, meds
    # A count, and nothing from the record.
    assert "1 unreadable record(s) left out" in caplog.text
    assert CANARY not in caplog.text
    assert "bad-ref-1018" not in caplog.text


# --- shapes the engine does not pass today ------------------------------------

def test_a_coding_system_that_is_not_a_string_reads_as_unlabeled():
    out = _summarize_bundle(_bundle(_med("sys-1018", medicationCodeableConcept={
        "coding": [{"system": [RXNORM], "code": "860975"}]})))
    assert out == [{"type": "MedicationRequest", "name": "unlabeled record",
                    "unreadable": True, "note": out[0]["note"]}], out


def test_a_loinc_code_that_is_not_a_string_keeps_the_row():
    out = _summarize_bundle(_bundle(_med(
        "loinc-1018", status="active",
        code={"coding": [{"system": LOINC, "code": [CANARY]}]},
        valueQuantity={"value": 5, "unit": "mg"})))
    assert not any(i.get("truncated") for i in out), out
    [row] = out
    assert row["type"] == "MedicationRequest"
    assert row["status"] == "active"
    assert CANARY not in repr(out)


def test_a_skipped_allergy_row_never_lets_no_known_allergies_be_quoted(
        monkeypatch, caplog):
    # Whatever raised, the skipped row may be an allergy: the "no known
    # allergy" entry beside it must read as a list longer than shown.
    caplog.set_level(logging.DEBUG)
    real = agent._nka_kinds

    def raising(res):
        if res.get("id") == "boom-1018":
            raise TypeError("unhashable type: 'list'")
        return real(res)
    monkeypatch.setattr(agent, "_nka_kinds", raising)
    out = _summarize_bundle(_bundle(
        {"resourceType": "AllergyIntolerance", "id": "nka-1018",
         "recordedDate": "2024-01-02",
         "code": {"coding": [{"system": SNOMED, "code": "716186003"}]}},
        {"resourceType": "AllergyIntolerance", "id": "boom-1018",
         "code": {"coding": [{"system": SNOMED, "code": "91936005"}]}}))
    [nka] = [i for i in out if i.get("no_known_allergy_record")]
    assert "longer than can be shown" in nka["statement"], nka
    assert out[-1].get("truncated") is True, out
    assert "1 unreadable record(s) left out" in caplog.text
    assert "boom-1018" not in caplog.text
