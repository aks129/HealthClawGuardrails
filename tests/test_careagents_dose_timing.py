"""When to take a medication: from the record's structured timing, or, when
the record has none, labelled general information and a pharmacist.

Beta feedback (2026-10-22): a tester on the sample records asked "do I need
to take metformin at a certain time?" and was told the agent did not have
that data. Four things lined up to say so:

- the sample metformin order (r6/seed.py) carried no dosageInstruction;
- the engine strips dosage free text (`dosageInstruction[].text`,
  `patientInstruction`) on every read, as it must: real feeds put names
  there. Structured timing survives, and nothing pinned that it does;
- `_summarize_bundle` copied no dosage field at all, so even structured
  timing never reached the model;
- the safety core said only "if the records don't show something, say the
  records don't show it", so the answer stopped there.

Timing is rendered by our own code from closed maps and bounded numbers.
Nothing upstream wrote as text reaches the model.
"""

from __future__ import annotations

import json

import pytest

from careagents import personas
from careagents.agent import _execute_tool, _summarize_bundle
from r6.redaction import apply_redaction
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

CANARY = "CANARY-DOSE-Zebulon-Quixtran"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"


def _med(dosage, rt="MedicationRequest"):
    res = {"resourceType": rt, "id": "m1", "status": "active",
           "medicationCodeableConcept": {"coding": [
               {"system": RXNORM, "code": "860975"}]}}
    res["dosageInstruction" if rt == "MedicationRequest" else "dosage"] = \
        dosage
    return res


def _item(res):
    bundle = {"resourceType": "Bundle", "total": 1,
              "entry": [{"resource": res}]}
    [item] = _summarize_bundle(bundle)
    return item


# --- 1. the sample record carries structured timing ---------------------------

def test_the_sample_metformin_order_carries_structured_timing():
    from r6.seed import _built_in_resources
    [med] = [r for r in _built_in_resources()
             if r.get("id") == "demo-medreq-metformin"]
    [dosage] = med["dosageInstruction"]
    assert dosage["timing"]["repeat"]["when"] == ["CM", "CV"]
    assert dosage["timing"]["repeat"]["frequency"] == 2
    # Structured only: no free text that a read would strip anyway.
    assert "text" not in dosage and "patientInstruction" not in dosage


# --- 2. the engine keeps structured timing and strips the free text -----------

def test_redaction_keeps_structured_timing_and_strips_dosage_text():
    res = _med([{
        "text": CANARY, "patientInstruction": CANARY,
        "timing": {"code": {"text": CANARY, "coding": [
                       {"code": "BID", "display": CANARY}]},
                   "repeat": {"frequency": 2, "period": 1, "periodUnit": "d",
                              "when": ["CM", "CV"]}},
        "asNeededBoolean": False}])
    out = apply_redaction(res)
    assert CANARY not in json.dumps(out)
    [dosage] = out["dosageInstruction"]
    assert dosage["timing"]["repeat"] == {
        "frequency": 2, "period": 1, "periodUnit": "d", "when": ["CM", "CV"]}
    assert dosage["asNeededBoolean"] is False


# --- 3. the summary renders timing from closed maps only ----------------------

@pytest.mark.parametrize("repeat, expected", [
    ({"frequency": 2, "period": 1, "periodUnit": "d", "when": ["CM", "CV"]},
     "twice a day, with breakfast and with dinner"),
    ({"frequency": 1, "period": 1, "periodUnit": "d", "when": ["HS"]},
     "once a day, at bedtime"),
    ({"frequency": 3, "period": 1, "periodUnit": "d"}, "3 times a day"),
    ({"frequency": 1, "period": 8, "periodUnit": "h"}, "once every 8 hours"),
    ({"frequency": 1, "period": 1, "periodUnit": "wk"}, "once a week"),
    ({"when": ["C"]}, "with meals"),
])
def test_structured_timing_reaches_the_model_in_plain_words(repeat, expected):
    item = _item(_med([{"timing": {"repeat": repeat}}]))
    assert item["timing"] == [expected]


def test_as_needed_is_said():
    item = _item(_med([{"asNeededBoolean": True}]))
    assert item["timing"] == ["as needed"]


def test_a_statement_carries_its_dosage_timing_too():
    item = _item(_med([{"timing": {"repeat": {"when": ["MORN"]}}}],
                      rt="MedicationStatement"))
    assert item["timing"] == ["in the morning"]


def test_no_dosage_text_ever_reaches_the_model():
    item = _item(_med([{
        "text": CANARY, "patientInstruction": CANARY,
        "timing": {"code": {"text": CANARY},
                   "repeat": {"when": [CANARY, "CM"]}}}]))
    assert CANARY not in json.dumps(item)
    assert item["timing"] == ["with breakfast"]


@pytest.mark.parametrize("repeat", [
    {"frequency": "2", "period": 1, "periodUnit": "d"},
    {"frequency": True, "period": 1, "periodUnit": "d"},
    {"frequency": 10**9, "period": 1, "periodUnit": "d"},
    {"frequency": 2, "period": 1, "periodUnit": CANARY},
    {"frequency": 2, "period": -1, "periodUnit": "d"},
    {"frequency": 2, "period": 1.5, "periodUnit": "d"},
    {"when": "CM"},
    {"when": [{"code": "CM"}]},
])
def test_malformed_timing_says_nothing(repeat):
    item = _item(_med([{"timing": {"repeat": repeat}}]))
    assert "timing" not in item
    assert CANARY not in json.dumps(item)


def test_a_medication_without_timing_carries_no_timing_key():
    assert "timing" not in _item(_med([{"text": CANARY}]))


def test_timing_is_only_read_on_medication_types():
    cond = {"resourceType": "Condition",
            "code": {"coding": [{"system": RXNORM, "code": "860975"}]},
            "dosageInstruction": [{"timing": {"repeat": {"when": ["CM"]}}}]}
    assert "timing" not in _item(cond)


# --- 4. the prompt: answer from timing, else labelled general information -----

def test_the_safety_core_says_how_to_answer_when_to_take_a_medication():
    core = " ".join(personas.SAFETY_CORE.split())
    assert "timing" in core
    assert "pharmacist or prescriber" in core
    assert "general information" in core
    assert "never stop at saying you don't have" in core.lower()


@pytest.mark.parametrize("clause", [
    # Unreadable is not absent: real feeds keep timing in stripped text.
    "say the timing isn't shown in what I can see here",
    # Timing only, labelled, and never an instruction.
    "one sentence of general information about timing only",
    'Begin it "In general,", say it is not advice for them',
    'Never word it as an instruction ("take it with...")',
    # Never dosing: amounts, frequency, missed doses, stopping, combining.
    "Give no general information about how much to take, how often, a "
    "missed or extra dose, stopping, splitting, or combining it with "
    "anything else: say it isn't shown in what I can see here",
    # "How should I take it?" is a timing question too, and only that.
    "When asked when or how to take a medication (time of day, or with "
    "or without food)",
    # An overdose or feeling unwell is the emergency rule's.
    "the emergency rule above comes first",
])
def test_the_medication_rule_keeps_its_fences(clause):
    assert clause in " ".join(personas.SAFETY_CORE.split())


def test_the_old_absence_wording_is_gone():
    core = " ".join(personas.SAFETY_CORE.split())
    assert "the records don't list when to take it" not in core


# --- 3b. as needed, ended orders, and many steps ------------------------------

PAIN = {"coding": [{"system": "http://snomed.info/sct", "code": "22253000",
                    "display": CANARY}], "text": CANARY}


Q6H_PRN = "as needed, no more often than every 6 hours"


@pytest.mark.parametrize("prn, expected", [
    ({"asNeededCodeableConcept": PAIN}, Q6H_PRN),
    ({"asNeededFor": [PAIN]}, Q6H_PRN),
    ({"asNeededBoolean": True}, Q6H_PRN),
])
def test_an_as_needed_order_says_its_schedule_is_a_limit(prn, expected):
    item = _item(_med([{"timing": {"repeat": {
        "frequency": 1, "period": 6, "periodUnit": "h"}}, **prn}]))
    assert item["timing"] == [expected]
    assert CANARY not in json.dumps(item)


def test_an_as_needed_range_says_the_most_often():
    item = _item(_med([{"asNeededBoolean": True, "timing": {"repeat": {
        "frequency": 1, "frequencyMax": 2, "period": 1, "periodUnit": "d"}}}]))
    assert item["timing"] == ["as needed, no more often than twice a day"]


@pytest.mark.parametrize("repeat, expected", [
    ({"frequency": 1, "frequencyMax": 3, "period": 1, "periodUnit": "d"},
     "1 to 3 times a day"),
    ({"frequency": 1, "period": 4, "periodMax": 6, "periodUnit": "h"},
     "once every 4 to 6 hours"),
    ({"frequency": 2, "period": 1, "periodUnit": "h"}, "twice an hour"),
])
def test_a_range_is_said_as_a_range(repeat, expected):
    assert _item(_med([{"timing": {"repeat": repeat}}]))["timing"] == [
        expected]


@pytest.mark.parametrize("repeat", [
    {"frequency": 3, "period": 1, "periodUnit": "d",
     "boundsDuration": {"value": 5, "unit": "d"}},
    {"frequency": 1, "period": 1, "periodUnit": "d", "count": 5},
    {"frequency": 3, "frequencyMax": 1, "period": 1, "periodUnit": "d"},
])
def test_a_schedule_that_cannot_be_said_in_full_says_nothing(repeat):
    assert "timing" not in _item(_med([{"timing": {"repeat": repeat}}]))


@pytest.mark.parametrize("status", [
    "stopped", "cancelled", "completed", "entered-in-error"])
def test_an_ended_order_carries_no_timing(status):
    res = _med([{"timing": {"repeat": {"when": ["CM"]}}}])
    res["status"] = status
    assert "timing" not in _item(res)


def test_more_than_three_steps_says_more_exist():
    steps = [{"timing": {"repeat": {"when": [w]}}}
             for w in ("MORN", "AFT", "EVE", "HS")]
    assert _item(_med(steps))["timing"] == [
        "in the morning", "in the afternoon", "in the evening",
        "and more steps"]


@pytest.mark.parametrize("status", [["active"], {"code": "active"}, 3])
def test_a_status_that_is_no_string_is_unknown_not_a_crash(status):
    # #1018: `status in <frozenset>` raised TypeError on a list or object.
    res = _med([{"timing": {"repeat": {"when": ["CM"]}}}])
    res["status"] = status
    item = _item(res)
    assert "status" not in item
    assert item["timing"] == ["with breakfast"]


def test_one_unreadable_row_never_drops_the_rest(monkeypatch):
    import careagents.agent as agent
    real = agent._only_absence_codes

    def boom(res):
        if res.get("id") == "bad":
            raise TypeError("surprise")
        return real(res)
    monkeypatch.setattr(agent, "_only_absence_codes", boom)
    good, bad = _med([]), _med([])
    bad["id"] = "bad"
    items = _summarize_bundle({"resourceType": "Bundle", "total": 2,
                               "entry": [{"resource": bad},
                                         {"resource": good}]})
    assert len(items) == 2
    # The bad row is still a record, never absence, and says nothing of
    # what it holds.
    assert items[0]["unreadable"] is True
    assert items[0]["type"] == "MedicationRequest"
    assert set(items[0]) == {"type", "name", "unreadable", "note"}
    assert items[1]["type"] == "MedicationRequest"


def test_a_list_status_through_the_engine_keeps_the_summary(
        cfg, svc, monkeypatch):  # noqa: F811
    """#1018 end to end: the engine accepts the row on ingest, and the
    summary still carries every list."""
    chain = Chain(cfg, svc, monkeypatch, allergy=True)
    res = _med([{"timing": {"repeat": {"when": ["CM"]}}}])
    res.update(id="qa-1018", status=["active"], intent="order",
               subject={"reference": "Patient/demo-patient-rivera"})
    result = chain.hc.ingest_bundle(TENANT, {
        "resourceType": "Bundle", "type": "collection",
        "entry": [{"resource": res}]})
    assert not result.get("errors"), result
    out = json.loads(_execute_tool(chain.hc, TENANT, "get_health_summary",
                                   {}, []))
    assert out["conditions"] and out["allergies"]
    assert len(out["medications"]) >= 2


def test_one_bad_row_never_takes_the_summary_down(monkeypatch):
    import careagents.agent as agent

    def boom(_dosage):
        raise RuntimeError("surprise")
    monkeypatch.setattr(agent, "_dose_timing", boom)
    item = _item(_med([{"timing": {"repeat": {"when": ["CM"]}}}]))
    assert item["type"] == "MedicationRequest" and item["name"]
    assert "timing" not in item


# --- 5. cross-layer: the real sample seed through the real engine -------------

def test_the_sample_summary_tells_the_model_when_metformin_is_taken(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    out = json.loads(_execute_tool(chain.hc, TENANT, "get_health_summary",
                                   {}, []))
    [met] = [m for m in out["medications"]
             if m.get("name") == "Metformin 500 mg"]
    assert met["timing"] == ["twice a day, with breakfast and with dinner"]
