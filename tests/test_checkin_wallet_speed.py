"""SPEED dry eye form in the SMART Health Check-in wallet (#1049).

A Verifier we pair with for the connectathon sends the SPEED questionnaire
inline: two `group` items, each a display line and four single-choice
questions (e2e/fixtures/checkin-request-form-speed.json: the Verifier's
published request structure exactly, with made-up wording, so the tests
prove the shape and carry none of the instrument's text). What these pin, by
running the shipped careagents/static/checkin_answer.js under node, as
tests/test_checkin_wallet_phq2.py does:

- One level of `group` is read; every rule a top-level question is held to
  holds inside it, and the group's heading goes through the same words
  check (no bidi control, not blank).
- A nested group, or a group carrying enableWhen, answerValueSet, initial
  or repeats, refuses the whole form. The 10-question cap counts across
  groups.
- `required` and an option's `extension` do not refuse; the option's
  coding is what is sent.
- The QuestionnaireResponse nests each group's answers under its linkId.
- The contact-lens form (free text and numbers) stays unsupported.

Synthetic data only.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.test_checkin_wallet_phq2 import _run, _status

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "e2e" / "fixtures"
SPEED = json.loads((FIXTURES / "checkin-request-form-speed.json").read_text())
CL_TRIAL = json.loads((FIXTURES / "checkin-request-form-cl-trial.json")
                      .read_text())
CANONICAL = "https://eye-care.verifier.example/fhir/Questionnaire/speed"
SYSTEM = ("https://eye-care.verifier.example/fhir/CodeSystem/"
          "eye-care-response")
SYMPTOMS = ["dryness", "soreness", "burning", "fatigue"]
ALL_FIRST = {f"{g}-{s}": 0 for g in ("frequency", "severity")
             for s in SYMPTOMS}


def _speed_only():
    req = copy.deepcopy(SPEED)
    req["items"] = [i for i in req["items"] if i["id"] == "speed"]
    return req


def _q(req):
    return req["items"][0]["content"]["questionnaire"]


def _mutated(fn):
    req = _speed_only()
    fn(_q(req))
    return req


# --- reading the form -------------------------------------------------------------

def test_the_speed_form_is_answered_from_the_questionnaire_sent():
    out = _run(SPEED)
    assert out["unsupported"] == [False, False]
    form = out["forms"][1]
    assert form["canonical"] == CANONICAL
    assert form["title"] == "Grouped symptom example"
    assert form["displays"] == []
    assert [(g["linkId"], g["text"], g["displays"]) for g in form["groups"]] == [
        ("frequency", "How often",
         ["0 Never · 1 Rarely · 2 Often · 3 Always"]),
        ("severity", "How bad",
         ["0 None · 1 Mild · 2 Moderate · 3 Severe · 4 Very severe"]),
    ]
    assert [q["text"] for q in form["questions"]] == [
        f"Symptom {s}, {w}" for w in ("how often", "how bad") for s in "ABCD"]
    assert [(q["linkId"], q["group"]) for q in form["questions"]] == [
        (f"{g}-{s}", gi) for gi, g in enumerate(("frequency", "severity"))
        for s in SYMPTOMS]
    assert [o["display"] for o in form["questions"][0]["options"]] == [
        "Never", "Rarely", "Often", "Always"]
    assert len(form["questions"][4]["options"]) == 5
    # The form asks the records relay for nothing.
    assert out["types"] == ["Patient"]


def test_a_flat_form_has_no_groups():
    """PHQ-2 reads as it did: every question at the top."""
    from tests.test_checkin_wallet_phq2 import REQUEST
    form = _run(REQUEST)["forms"][1]
    assert form["groups"] == []
    assert [q["group"] for q in form["questions"]] == [None, None]


# --- the answer -------------------------------------------------------------------

def test_answers_nest_under_each_groups_linkid():
    picked = dict(ALL_FIRST, **{"frequency-soreness": 3,
                                "severity-fatigue": 4})
    out = _run(SPEED, share={"speed": True}, answers={"speed": picked})
    assert out["problems"] == []
    assert _status(out) == {"patient": "unavailable", "speed": "fulfilled"}
    [art] = out["response"]["artifacts"]
    qr = art["value"]
    assert qr["questionnaire"] == CANONICAL
    assert [(g["linkId"], g["text"], "answer" in g) for g in qr["item"]] == [
        ("frequency", "How often", False),
        ("severity", "How bad", False)]
    frequency, severity = qr["item"]
    assert [i["linkId"] for i in frequency["item"]] == [
        f"frequency-{s}" for s in SYMPTOMS]
    assert [i["linkId"] for i in severity["item"]] == [
        f"severity-{s}" for s in SYMPTOMS]
    # The option's coding, exactly; its ordinalValue extension stays behind.
    assert frequency["item"][1]["answer"] == [{"valueCoding": {
        "system": SYSTEM, "code": "frequency-3", "display": "Always"}}]
    assert severity["item"][3]["answer"] == [{"valueCoding": {
        "system": SYSTEM, "code": "severity-4", "display": "Very severe"}}]
    assert "extension" not in json.dumps(qr)


def test_top_level_and_grouped_questions_keep_their_order():
    def mix(q):
        q["item"].insert(1, {"linkId": "top", "type": "choice",
                             "text": "A question at the top",
                             "answerOption": [
                                 {"valueCoding": {"code": "y", "display": "Yes"}},
                                 {"valueCoding": {"code": "n", "display": "No"}}]})
        q["item"][2]["item"] = q["item"][2]["item"][:2]   # one severity question
    req = _mutated(mix)
    picked = {"frequency-dryness": 0, "frequency-soreness": 0,
              "frequency-burning": 0, "frequency-fatigue": 0, "top": 1,
              "severity-dryness": 2}
    out = _run(req, share={"speed": True}, answers={"speed": picked})
    qr = out["response"]["artifacts"][0]["value"]
    assert [i["linkId"] for i in qr["item"]] == ["frequency", "top", "severity"]
    assert qr["item"][1]["answer"] == [{"valueCoding": {"code": "n",
                                                        "display": "No"}}]
    assert [i["linkId"] for i in qr["item"][2]["item"]] == ["severity-dryness"]


@pytest.mark.parametrize("answers", [
    {},
    {"speed": {k: v for k, v in ALL_FIRST.items() if k != "severity-fatigue"}},
    {"speed": dict(ALL_FIRST, **{"frequency-dryness": 4})},   # no such option
    # A group's linkId is not a question.
    {"speed": {k: v for k, v in ALL_FIRST.items()
               if k != "frequency-dryness"} | {"frequency": 0}},
])
def test_an_incomplete_set_of_grouped_answers_is_declined(answers):
    out = _run(_speed_only(), share={"speed": True}, answers=answers)
    assert _status(out) == {"speed": "declined"}
    assert out["response"]["artifacts"] == []


# --- what is not answered ---------------------------------------------------------

def _group_child(q, **changes):
    q["item"][0]["item"][1].update(changes)


@pytest.mark.parametrize("req", [
    pytest.param(_mutated(lambda q: q["item"][0]["item"].append(
        {"linkId": "deeper", "type": "group", "text": "More",
         "item": [copy.deepcopy(q["item"][0]["item"][1])
                  | {"linkId": "deeper-1"}]})), id="nested-group"),
    pytest.param(_mutated(lambda q: _group_child(
        q, item=[{"linkId": "x", "type": "display", "text": "nested"}])),
        id="child-with-items"),
    pytest.param(_mutated(lambda q: q["item"][0].update(
        enableWhen=[{"question": "x", "operator": "exists",
                     "answerBoolean": True}])), id="group-enable-when"),
    pytest.param(_mutated(lambda q: q["item"][0].update(repeats=True)),
                 id="group-repeats"),
    pytest.param(_mutated(lambda q: q["item"][0].update(
        answerValueSet="http://example.org/vs")), id="group-value-set"),
    pytest.param(_mutated(lambda q: q["item"][0].update(initial=[{}])),
                 id="group-initial"),
    pytest.param(_mutated(lambda q: _group_child(q, enableWhen=[{}])),
                 id="child-enable-when"),
    pytest.param(_mutated(lambda q: _group_child(q, repeats=True)),
                 id="child-repeats"),
    pytest.param(_mutated(lambda q: _group_child(
        q, answerValueSet="http://example.org/vs")), id="child-value-set"),
    pytest.param(_mutated(lambda q: _group_child(q, initial=[{}])),
                 id="child-initial"),
    pytest.param(_mutated(lambda q: _group_child(q, type="string")),
                 id="child-free-text"),
    pytest.param(_mutated(lambda q: _group_child(q, type="integer")),
                 id="child-integer"),
    pytest.param(_mutated(lambda q: _group_child(
        q, answerOption=q["item"][0]["item"][1]["answerOption"][:1])),
        id="child-one-option"),
    pytest.param(_mutated(lambda q: _group_child(q, text="​")),
                 id="child-blank-text"),
    pytest.param(_mutated(lambda q: _group_child(q, prefix="‮1.")),
                 id="child-bidi-prefix"),
    pytest.param(_mutated(lambda q: q["item"][0]["item"][1]["answerOption"][0]
                          ["valueCoding"].update(display="Never‮")),
                 id="child-option-bidi"),
    pytest.param(_mutated(lambda q: q["item"][0]["item"][0].update(
        text="⁦help⁩")), id="group-display-bidi"),
    pytest.param(_mutated(lambda q: q["item"][0].update(
        text="How often‮?")), id="group-heading-bidi"),
    pytest.param(_mutated(lambda q: q["item"][0].update(
        text="​⁣")), id="group-heading-invisible"),
    pytest.param(_mutated(lambda q: q["item"][0].update(text="   ")),
                 id="group-heading-blank"),
    # #1052 security F1: Hangul fillers are letters that draw nothing.
    *(pytest.param(_mutated(lambda q, c=c: q["item"][0].update(text=c * 2)),
                   id=f"group-heading-filler-{ord(c):04X}")
      for c in "ㅤᅟᅠﾠ"),
    pytest.param(_mutated(lambda q: _group_child(q, text="ㅤ")),
                 id="child-filler-text"),
    pytest.param(_mutated(lambda q: q["item"][0]["item"][1]["answerOption"][0]
                          ["valueCoding"].update(display="ﾠ")),
                 id="child-option-filler"),
    # #1052 security F2: implicit bidi marks reorder what is around them.
    *(pytest.param(_mutated(lambda q, c=c: q["item"][0].update(
        text=f"Days: {c}0 - 3")), id=f"group-heading-mark-{ord(c):04X}")
      for c in "؜‎‏"),
    pytest.param(_mutated(lambda q: q["item"][0]["item"][1]["answerOption"][0]
                          ["valueCoding"].update(display="Never ؜(0, 4)")),
                 id="child-option-mark"),
    pytest.param(_mutated(lambda q: q["item"][0].pop("text")),
                 id="group-heading-missing"),
    pytest.param(_mutated(lambda q: q["item"][0].update(text="x" * 501)),
                 id="group-heading-too-long"),
    pytest.param(_mutated(lambda q: q["item"][0].update(prefix="‮")),
                 id="group-prefix-bidi"),
    pytest.param(_mutated(lambda q: q["item"][0].update(
        answerOption=q["item"][0]["item"][1]["answerOption"])),
        id="group-with-options"),
    pytest.param(_mutated(lambda q: q["item"][0].update(item=[])),
                 id="group-empty"),
    pytest.param(_mutated(lambda q: q["item"][0].update(
        item=q["item"][0]["item"][:1])), id="group-display-only"),
    pytest.param(_mutated(lambda q: q["item"][0].update(item={"linkId": "x"})),
                 id="group-items-not-a-list"),
    pytest.param(_mutated(lambda q: q["item"][0]["item"].append(None)),
                 id="group-child-not-an-object"),
    pytest.param(_mutated(lambda q: q["item"][1].update(linkId="frequency")),
                 id="duplicate-group-linkid"),
    pytest.param(_mutated(lambda q: _group_child(q, linkId="severity")),
                 id="child-repeats-a-group-linkid"),
    pytest.param(_mutated(lambda q: q["item"][1]["item"][1].update(
        linkId="frequency-dryness")), id="duplicate-linkid-across-groups"),
    # 8 questions in the groups, 3 more at the top: 11, over the cap of 10.
    pytest.param(_mutated(lambda q: q["item"].extend(
        copy.deepcopy(q["item"][0]["item"][1]) | {"linkId": f"top{n}"}
        for n in range(3))), id="too-many-questions-across-groups"),
])
def test_anything_outside_one_level_of_choice_groups_is_unsupported(req):
    out = _run(req, share={"speed": True}, answers={"speed": ALL_FIRST})
    assert out["forms"] == [None]
    assert _status(out) == {"speed": "unsupported"}
    assert out["response"]["artifacts"] == []


def test_ten_questions_across_groups_is_still_answered():
    req = _mutated(lambda q: q["item"].extend(
        copy.deepcopy(q["item"][0]["item"][1]) | {"linkId": f"top{n}"}
        for n in range(2)))
    assert len(_run(req)["forms"][0]["questions"]) == 10


def test_required_and_option_extensions_do_not_refuse():
    """The partner's form carries both; neither changes what is asked."""
    q = _q(_speed_only())
    assert q["item"][0]["item"][1]["required"] is True
    assert q["item"][0]["item"][1]["answerOption"][0]["extension"]
    req = _mutated(lambda q: q["item"][0].update(required=True))
    assert _run(req)["forms"][0] is not None


def test_the_contact_lens_form_stays_unsupported():
    """Free text and numbers inside groups (#1049 out of scope)."""
    out = _run(CL_TRIAL, share={"eyeClTrial": True})
    assert out["forms"] == [None, None]
    assert _status(out) == {"patient": "unavailable",
                            "eyeClTrial": "unsupported"}
    # On the free-text question alone, not only on its other refusals.
    req = copy.deepcopy(CL_TRIAL)
    q = req["items"][1]["content"]["questionnaire"]
    wear = next(i for i in q["item"] if i["linkId"] == "wear")
    q["item"] = [wear]
    wear["item"] = [i for i in wear["item"]
                    if i["type"] == "choice" or i["linkId"] == "trial-lens"]
    assert [i["type"] for i in wear["item"]] == [
        "string", "choice", "choice", "choice"]
    assert _run(req)["forms"][1] is None
    wear["item"] = wear["item"][1:]
    assert _run(req)["forms"][1] is not None
