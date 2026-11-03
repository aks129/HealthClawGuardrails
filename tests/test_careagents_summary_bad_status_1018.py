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
record: a row it cannot read is listed as one entry with no readable name
(#207, #949), never absent and never a list cut short. The log names the list
and the exception class, never the record or the exception's message.

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
from tests.test_beta_acceptance_rows import TENANT, Chain
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


def _one_unreadable(items, rt):
    """Exactly one entry that could not be read, shaped as every unreadable
    record is, and no truncated marker: one entry is on file, the list is
    not longer than shown (#1101 QA)."""
    unreadable = [i for i in items if i.get("name") == "unreadable record"]
    assert len(unreadable) == 1, items
    [row] = unreadable
    assert row["type"] == rt and row["unreadable"] is True, row
    assert "one entry has no readable name here" in row["note"], row
    assert not any(i.get("truncated") for i in items), items


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


def test_a_row_that_cannot_be_read_is_listed_unreadable_not_lost(
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
    assert "Metformin 500 mg" in [i.get("name") for i in meds], meds
    _one_unreadable(meds, "MedicationRequest")
    # The list and the exception class, and nothing from the record.
    assert ("1 MedicationRequest record(s) listed as unreadable "
            "(AttributeError)") in caplog.text
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
    # Whatever raised, the unread row may be an allergy: the "no known
    # allergy" entry beside it must read as a disagreement, never a fact.
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
    assert "disagrees" in nka["statement"], nka
    assert "no known" not in nka["statement"], nka
    _one_unreadable(out, "AllergyIntolerance")
    assert ("1 AllergyIntolerance record(s) listed as unreadable (TypeError)"
            in caplog.text)
    assert "boom-1018" not in caplog.text


# --- QA (#1101): the resolver route, real ingest, a failed read ----------------

def test_a_medication_whose_code_is_a_list_is_left_out_not_the_summary(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    # A second route to the backstop through the real engine, and through
    # the real ingest rather than a planted row: a Medication whose `code`
    # is a list is accepted, survives redaction (the list holds a
    # CodeableConcept the engine even labels), and is read by the
    # medicationReference resolver, which raised AttributeError on it. On
    # main that dropped the whole summary, as #1018's status did.
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch, allergy=True)
    ingested = chain.hc.ingest_bundle(TENANT, {
        "resourceType": "Bundle", "type": "collection",
        **_bundle(
            {"resourceType": "Medication", "id": "med-list-1018",
             "code": [{"coding": [{"system": RXNORM, "code": "860975"}]}]},
            {"resourceType": "MedicationRequest", "id": "ref-list-code-1018",
             "subject": PATIENT, "intent": "order", "status": "active",
             "medicationReference": {
                 "reference": "Medication/med-list-1018"}})})
    assert ingested.get("ingested") == 2 and not ingested.get("errors"), \
        ingested
    out, _ = _first_turn(chain, monkeypatch)
    _rest_intact(out)
    meds = out["medications"]
    assert "Metformin 500 mg" in [i.get("name") for i in meds], meds
    _one_unreadable(meds, "MedicationRequest")
    # The in-process engine logs its own request paths; CareAgents' line
    # is the one held to a count.
    ours = " ".join(r.getMessage() for r in caplog.records
                    if r.name == "careagents.agent")
    assert ("1 MedicationRequest record(s) listed as unreadable "
            "(AttributeError)") in ours
    assert "med-list-1018" not in ours
    assert "ref-list-code-1018" not in ours


def test_a_failed_engine_read_inside_a_row_still_fails_the_list():
    # The backstop catches a row's shape, never an engine failure: that
    # still fails the list, so the tool marks it could_not_read rather than
    # reading it as a list longer than shown.
    from careagents.healthclaw import HealthClawError

    def unavailable(ref):
        raise HealthClawError("read failed (503)", 503)
    with pytest.raises(HealthClawError):
        _summarize_bundle(_bundle(
            {"resourceType": "MedicationRequest", "id": "ref-1018",
             "subject": PATIENT, "intent": "order", "status": "active",
             "medicationReference": {"reference": "Medication/m-1018"}}),
            resolve_ref=unavailable)
