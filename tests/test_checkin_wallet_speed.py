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
  or repeats, refuses the whole form. The 30-question cap counts across
  groups.
- `required` and an option's `extension` do not refuse; the option's
  coding is what is sent.
- The QuestionnaireResponse nests each group's answers under its linkId.
- The contact-lens form (e2e/fixtures/checkin-request-form-cl-trial.json,
  the Verifier's structure exactly) is answered: its single-choice
  questions are, and each optional question of another kind (free text, a
  number, a pick-several) is shown as one this wallet does not answer and
  left out of the response. Such a question that is `required` still
  refuses the whole form: a completed response cannot leave it out.

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


def _two_option_copy(q, link_id):
    """A copy of the first grouped question, with two of its options."""
    child = copy.deepcopy(q["item"][0]["item"][1])
    child["answerOption"] = child["answerOption"][:2]
    return child | {"linkId": link_id}


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
    # Required, as SPEED's questions all are, so the form cannot be
    # completed without it. An optional one is left out (contact lens).
    pytest.param(_mutated(lambda q: _group_child(q, repeats=True)),
                 id="child-repeats-required"),
    pytest.param(_mutated(lambda q: _group_child(
        q, answerValueSet="http://example.org/vs")), id="child-value-set"),
    pytest.param(_mutated(lambda q: _group_child(q, initial=[{}])),
                 id="child-initial"),
    pytest.param(_mutated(lambda q: _group_child(q, type="string")),
                 id="child-free-text-required"),
    pytest.param(_mutated(lambda q: _group_child(q, type="integer")),
                 id="child-integer-required"),
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
    # 8 questions in the groups, 23 more at the top: 31, over the cap of 30
    # (two options each, so it is the count, not the size cap, that refuses).
    pytest.param(_mutated(lambda q: q["item"].extend(
        _two_option_copy(q, f"top{n}") for n in range(23))),
        id="too-many-questions-across-groups"),
])
def test_anything_outside_one_level_of_choice_groups_is_unsupported(req):
    out = _run(req, share={"speed": True}, answers={"speed": ALL_FIRST})
    assert out["forms"] == [None]
    assert _status(out) == {"speed": "unsupported"}
    assert out["response"]["artifacts"] == []


def test_thirty_questions_across_groups_is_still_answered():
    req = _mutated(lambda q: q["item"].extend(
        _two_option_copy(q, f"top{n}") for n in range(22)))
    assert len(_run(req)["forms"][0]["questions"]) == 30


def test_required_and_option_extensions_do_not_refuse():
    """The partner's form carries both; neither changes what is asked."""
    q = _q(_speed_only())
    assert q["item"][0]["item"][1]["required"] is True
    assert q["item"][0]["item"][1]["answerOption"][0]["extension"]
    req = _mutated(lambda q: q["item"][0].update(required=True))
    assert _run(req)["forms"][0] is not None


# --- the contact-lens form: choices answered, the rest left out ------------------

# Each question the wallet does not answer, and the index of its group.
CL_SKIPPED = [("trial-lens", 1), ("days-worn", 1), ("hours-worn", 1),
              ("hours-comfortable", 1), ("problems", 3), ("concerns", 3),
              ("improvement", 4)]


def _cl_only():
    req = copy.deepcopy(CL_TRIAL)
    req["items"] = [i for i in req["items"] if i["id"] == "eyeClTrial"]
    return req


def _cl_mutated(fn):
    req = _cl_only()
    fn(_q(req))
    return req


def _cl_item(q, link_id):
    """The item with this linkId, at the top or one level down."""
    for it in q["item"]:
        if it["linkId"] == link_id:
            return it
        for child in it.get("item", []):
            if child["linkId"] == link_id:
                return child
    raise KeyError(link_id)


def _cl_all_first(form):
    return {q["linkId"]: 0 for q in form["questions"]}


def test_the_contact_lens_form_answers_its_choices_and_skips_the_rest():
    """Before, the whole form was unsupported: 19 questions over the cap
    of 10, and free text, numbers and a pick-several among them."""
    out = _run(CL_TRIAL)
    assert out["unsupported"] == [False, False]
    form = out["forms"][1]
    assert [g["linkId"] for g in form["groups"]] == [
        "safety", "wear", "vision", "comfort", "impression"]
    assert len(form["questions"]) == 19
    # Only single choices are questions to answer. Each other question is
    # listed, in the group it sits in, in its own words.
    assert [(s["linkId"], s["group"]) for s in form["skipped"]] == CL_SKIPPED
    for s in form["skipped"]:
        assert s["text"] == _cl_item(_q(_cl_only()), s["linkId"])["text"]
    # Where it sits: before the question that follows it on the form.
    links = [q["linkId"] for q in form["questions"]]
    at = {s["linkId"]: s["at"] for s in form["skipped"]}
    assert links[at["trial-lens"]] == "trial-status"
    assert links[at["days-worn"]] == "compared"
    assert links[at["hours-comfortable"]] == "compared"
    assert links[at["concerns"]] == "overall"     # the end of its group
    assert links[at["improvement"]] == "supply-options"


def test_the_contact_lens_answers_are_sent_without_the_skipped_questions():
    form = _run(CL_TRIAL)["forms"][1]
    out = _run(CL_TRIAL, share={"eyeClTrial": True},
               answers={"eyeClTrial": _cl_all_first(form)})
    assert out["problems"] == []
    assert _status(out) == {"patient": "unavailable",
                            "eyeClTrial": "fulfilled"}
    [art] = out["response"]["artifacts"]
    qr = art["value"]
    assert qr["status"] == "completed"
    assert qr["questionnaire"] == (
        CL_TRIAL["items"][1]["content"]["questionnaireCanonical"])
    assert [g["linkId"] for g in qr["item"]] == [
        "safety", "wear", "vision", "comfort", "impression"]
    sent = [i["linkId"] for g in qr["item"] for i in g["item"]]
    assert sent == [q["linkId"] for q in form["questions"]]
    assert not set(sent) & {link for link, _ in CL_SKIPPED}
    for g in qr["item"]:
        for i in g["item"]:
            assert list(i["answer"][0]) == ["valueCoding"]


def test_a_skipped_question_is_never_answered_even_if_an_answer_is_given():
    form = _run(CL_TRIAL)["forms"][1]
    answers = _cl_all_first(form) | {"trial-lens": 0, "problems": 1}
    out = _run(_cl_only(), share={"eyeClTrial": True},
               answers={"eyeClTrial": answers})
    qr = json.dumps(out["response"]["artifacts"][0]["value"])
    assert '"trial-lens"' not in qr and '"problems"' not in qr


def test_a_contact_lens_choice_left_blank_declines_the_form():
    """Every choice is asked, optional on the form or not: a form is sent
    whole or not at all."""
    form = _run(CL_TRIAL)["forms"][1]
    answers = _cl_all_first(form)
    del answers["supply-options"]
    out = _run(_cl_only(), share={"eyeClTrial": True},
               answers={"eyeClTrial": answers})
    assert _status(out) == {"eyeClTrial": "declined"}
    assert out["response"]["artifacts"] == []


@pytest.mark.parametrize("req", [
    # Required: a completed response cannot leave it out.
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "trial-lens").update(
        required=True)), id="required-free-text"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "problems").update(
        required=True)), id="required-pick-several"),
    # A kind FHIR does not have is not a question to leave out.
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "days-worn").update(
        type="slider")), id="unknown-type"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "days-worn").pop("type")),
                 id="no-type"),
    # Its words are shown, so they are held to the same check.
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        text="Anything else‮?")), id="skipped-text-bidi"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        text="Notes ؜(0, 4)")), id="skipped-text-mark"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        text="Notes א (0, 4)")), id="skipped-text-rtl"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        text="​ㅤ")), id="skipped-text-blank"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").pop("text")),
                 id="skipped-text-missing"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        text="x" * 501)), id="skipped-text-too-long"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        prefix="‮")), id="skipped-prefix-bidi"),
    # The other refusals hold for it as for any question.
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        enableWhen=[{}])), id="skipped-enable-when"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        initial=[{"valueString": "presumed"}])), id="skipped-initial"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        answerValueSet="http://example.org/vs")), id="skipped-value-set"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        item=[{"linkId": "x", "type": "display", "text": "nested"}])),
        id="skipped-with-items"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        linkId="overall")), id="skipped-duplicate-linkid"),
    # A heading over nothing to answer, or too much left unanswered.
    pytest.param(_cl_mutated(lambda q: q["item"].append(
        {"linkId": "notes", "type": "group", "text": "Notes",
         "item": [{"linkId": "note-1", "type": "text", "text": "Anything"}]})),
        id="group-of-skipped-only"),
    # 7 on the form, 4 more: 11, over the cap of 10.
    pytest.param(_cl_mutated(lambda q: q["item"].extend(
        {"linkId": f"note-{n}", "type": "string", "text": f"Note {n}"}
        for n in range(4))), id="too-many-skipped"),
])
def test_a_form_whose_other_questions_cannot_be_left_out_is_unsupported(req):
    out = _run(req, share={"eyeClTrial": True})
    assert out["forms"] == [None]
    assert _status(out) == {"eyeClTrial": "unsupported"}
    assert out["response"]["artifacts"] == []


def test_ten_skipped_questions_is_still_answered():
    req = _cl_mutated(lambda q: q["item"].extend(
        {"linkId": f"note-{n}", "type": "string", "text": f"Note {n}"}
        for n in range(3)))
    assert len(_run(req)["forms"][0]["skipped"]) == 10


def test_a_form_with_nothing_skipped_lists_nothing():
    assert _run(_speed_only())["forms"][0]["skipped"] == []
