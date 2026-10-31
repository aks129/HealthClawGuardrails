"""QA for #1049 / PR #1052: the grouped form's QuestionnaireResponse holds
to the FHIR R4 rules a validator applies against the Questionnaire.

tests/test_checkin_wallet_speed.py pins the SPEED answer by hand. These
walk any response against the Questionnaire that produced it, the way the
HL7 validator does, so a shape the hand-written assertions do not name
still has to be right:

- every response item's linkId is an item at the same level of the
  Questionnaire, in the Questionnaire's order, with its text;
- a group carries `item` and never `answer` (qrs-1), a question carries one
  `answer` and never `item`, and no display line is answered;
- an answer is a valueCoding that is one of that question's own options;
- every `required` item is present.

Plus the declined path for a grouped form, and a group's prefix.

Synthetic data only.
"""
from __future__ import annotations

import copy

import pytest

from tests.test_checkin_wallet_phq2 import REQUEST, _run, _status
from tests.test_checkin_wallet_speed import ALL_FIRST, SPEED, _mutated, _q, _speed_only

pytestmark = pytest.mark.extension


def _conformance(qr_items, q_items, path=""):
    problems = []
    by_id = {i["linkId"]: i for i in q_items}
    order = [i["linkId"] for i in q_items]
    last = -1
    for it in qr_items:
        lid = it["linkId"]
        where = f"{path}/{lid}"
        if lid not in by_id:
            problems.append(f"{where}: not in the Questionnaire at this level")
            continue
        qi = by_id[lid]
        pos = order.index(lid)
        if pos <= last:
            problems.append(f"{where}: out of the Questionnaire's order")
        last = pos
        if it.get("text") != qi.get("text"):
            problems.append(f"{where}: text differs from the Questionnaire")
        if qi["type"] == "group":
            if "answer" in it:
                problems.append(f"{where}: a group carries an answer")
            if not it.get("item"):
                problems.append(f"{where}: a group with no items")
            problems += _conformance(it.get("item", []), qi.get("item", []),
                                     where)
        elif qi["type"] == "display":
            problems.append(f"{where}: a display line is answered")
        else:
            if "item" in it:
                problems.append(f"{where}: a question carries item")
            answers = it.get("answer", [])
            if len(answers) != 1:
                problems.append(f"{where}: {len(answers)} answers")
            options = [o["valueCoding"] for o in qi["answerOption"]]
            for a in answers:
                if [k for k in a if k.startswith("value")] != ["valueCoding"]:
                    problems.append(f"{where}: not a valueCoding")
                elif a["valueCoding"] not in options:
                    problems.append(f"{where}: not one of its options")
    present = {i["linkId"] for i in qr_items}
    problems += [f"{path}/{qi['linkId']}: required, missing"
                 for qi in q_items
                 if qi.get("required") and qi["linkId"] not in present]
    return problems


def _answered(req, form_id, answers):
    out = _run(req, share={form_id: True}, answers={form_id: answers})
    assert out["problems"] == []
    [art] = out["response"]["artifacts"]
    qr = art["value"]
    q = next(i for i in req["items"]
             if i["id"] == form_id)["content"]["questionnaire"]
    assert qr["resourceType"] == "QuestionnaireResponse"
    assert qr["status"] == "completed"
    return qr, q


def _mixed(q):
    """A question at the very top, one between the groups, and a group
    with a prefix: every placement the QR builder has to get right."""
    top = {"linkId": "top", "type": "choice", "text": "A question at the top",
           "answerOption": [{"valueCoding": {"code": "y", "display": "Yes"}},
                            {"valueCoding": {"code": "n", "display": "No"}}]}
    q["item"].insert(1, top)
    q["item"].insert(0, copy.deepcopy(top) | {"linkId": "first"})
    q["item"][2]["prefix"] = "B."


@pytest.mark.parametrize("req,answers", [
    pytest.param(SPEED, dict(ALL_FIRST, **{"frequency-soreness": 3,
                                           "severity-fatigue": 4}), id="speed"),
    pytest.param(_mutated(_mixed), dict(ALL_FIRST, top=1, first=0),
                 id="questions-around-and-between-groups"),
])
def test_the_grouped_response_conforms_to_its_questionnaire(req, answers):
    qr, q = _answered(req, "speed", answers)
    assert _conformance(qr["item"], q["item"]) == []


def test_the_flat_phq2_response_still_conforms():
    qr, q = _answered(REQUEST, "phq2", {"phq2-1": 1, "phq2-2": 2})
    assert _conformance(qr["item"], q["item"]) == []
    assert all("item" not in i for i in qr["item"])


def test_the_conformance_walk_catches_a_misplaced_answer():
    """The walk is not vacuous: an answer pulled out of its group, and a
    group given an answer, are both reported."""
    qr, q = _answered(SPEED, "speed", ALL_FIRST)
    flat = copy.deepcopy(qr["item"])
    flat.append(flat[0]["item"].pop())
    assert _conformance(flat, q["item"])
    answered_group = copy.deepcopy(qr["item"])
    answered_group[0]["answer"] = answered_group[0]["item"][0]["answer"]
    assert _conformance(answered_group, q["item"])


def test_a_grouped_form_not_chosen_is_declined_and_sends_nothing():
    out = _run(_speed_only(), share={"speed": False},
               answers={"speed": ALL_FIRST})
    assert _status(out) == {"speed": "declined"}
    assert out["response"]["artifacts"] == []


def test_a_groups_prefix_is_read_with_its_heading():
    form = _run(_mutated(lambda q: q["item"][1].update(prefix="2.")))[
        "forms"][0]
    assert [(g["prefix"], g["text"]) for g in form["groups"]] == [
        ("", "How often"), ("2.", "How bad")]
    assert _q(_speed_only())["item"][1].get("prefix") is None
