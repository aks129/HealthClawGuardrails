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
  questions must be, and its free text, numbers and pick-several may be,
  each sent as the type the form declares. An optional question of a kind
  this wallet does not answer (a date, say) is shown as one and left out
  of the response; a `required` one refuses the whole form, since a
  completed response cannot leave it out.

Synthetic data only.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.test_checkin_wallet_phq2 import _run, _status

pytestmark = pytest.mark.extension

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
    pytest.param(_mutated(lambda q: _group_child(
        q, answerValueSet="http://example.org/vs")), id="child-value-set"),
    pytest.param(_mutated(lambda q: _group_child(q, initial=[{}])),
                 id="child-initial"),
    # Required, as SPEED's questions all are, and of a kind this wallet
    # does not answer: the form cannot be completed without it. An
    # optional one is left out.
    pytest.param(_mutated(lambda q: _group_child(q, type="date")),
                 id="child-date-required"),
    pytest.param(_mutated(lambda q: _group_child(q, type="time")),
                 id="child-time-required"),
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


# --- the contact-lens form: every question answered ------------------------------

# Its questions that are not one pick from a list, with their kinds. They
# were shown as not answered here (#1061); now each is answered, optionally.
CL_TYPED = [("trial-lens", "string"), ("days-worn", "integer"),
            ("hours-worn", "decimal"), ("hours-comfortable", "decimal"),
            ("problems", "choice"), ("concerns", "text"),
            ("improvement", "text")]


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
    """The first option of every single choice: the questions that must be
    answered."""
    return {q["linkId"]: 0 for q in form["questions"]
            if q["kind"] == "choice" and not q["repeats"]}


def _as_date(*link_ids):
    """The CL form with these questions made dates: still not answered
    here, so shown and left out."""
    def fn(q):
        for link in link_ids:
            it = _cl_item(q, link)
            it["type"] = "date"
            it.pop("answerOption", None)
            it.pop("repeats", None)
    return _cl_mutated(fn)


def test_the_contact_lens_form_answers_every_question():
    """Before #1061 the whole form was unsupported; #1061 answered its 19
    single choices and showed 7 as not answered. Now all 26 are asked."""
    out = _run(CL_TRIAL)
    assert out["unsupported"] == [False, False]
    form = out["forms"][1]
    assert [g["linkId"] for g in form["groups"]] == [
        "safety", "wear", "vision", "comfort", "impression"]
    assert len(form["questions"]) == 26
    assert len(_cl_all_first(form)) == 19
    assert form["skipped"] == []
    kinds = {q["linkId"]: q["kind"] for q in form["questions"]}
    assert [(link, kinds[link]) for link, _ in CL_TYPED] == CL_TYPED
    assert all(not q["required"] for q in form["questions"]
               if q["linkId"] in dict(CL_TYPED))


def test_the_contact_lens_form_is_sent_with_only_what_was_answered():
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
    sent = [i["linkId"] for g in qr["item"] for i in g["item"]]
    assert sent == list(_cl_all_first(form))
    assert not set(sent) & {link for link, _ in CL_TYPED}


def test_the_contact_lens_typed_answers_go_as_the_types_the_form_declares():
    form = _run(CL_TRIAL)["forms"][1]
    typed = {"trial-lens": "Brand X", "days-worn": "7",
             "hours-worn": "10.5", "problems": [0, 2],
             "concerns": "  Itchy at night  "}
    out = _run(_cl_only(), share={"eyeClTrial": True},
               answers={"eyeClTrial": _cl_all_first(form) | typed})
    assert out["problems"] == []
    qr = out["response"]["artifacts"][0]["value"]
    got = {i["linkId"]: i["answer"] for g in qr["item"] for i in g["item"]}
    assert got["trial-lens"] == [{"valueString": "Brand X"}]
    assert got["days-worn"] == [{"valueInteger": 7}]
    assert got["hours-worn"] == [{"valueDecimal": 10.5}]
    assert got["concerns"] == [{"valueString": "Itchy at night"}]
    options = _cl_item(_q(_cl_only()), "problems")["answerOption"]
    assert got["problems"] == [{"valueCoding": options[0]["valueCoding"]},
                               {"valueCoding": options[2]["valueCoding"]}]
    assert "improvement" not in got and "hours-comfortable" not in got


def test_a_skipped_question_is_never_answered_even_if_an_answer_is_given():
    req = _as_date("trial-lens", "problems")
    form = _run(req)["forms"][0]
    assert [s["linkId"] for s in form["skipped"]] == ["trial-lens", "problems"]
    answers = _cl_all_first(form) | {"trial-lens": "2026-01-01",
                                     "problems": 1}
    out = _run(req, share={"eyeClTrial": True},
               answers={"eyeClTrial": answers})
    assert _status(out) == {"eyeClTrial": "fulfilled"}
    qr = json.dumps(out["response"]["artifacts"][0]["value"])
    assert '"trial-lens"' not in qr and '"problems"' not in qr


def test_a_skipped_question_sits_before_the_question_after_it():
    form = _run(_as_date("trial-lens", "concerns"))["forms"][0]
    links = [q["linkId"] for q in form["questions"]]
    at = {s["linkId"]: s["at"] for s in form["skipped"]}
    assert links[at["trial-lens"]] == "trial-status"
    assert links[at["concerns"]] == "overall"     # the end of its group
    assert [(s["linkId"], s["group"]) for s in form["skipped"]] == [
        ("trial-lens", 1), ("concerns", 3)]


def test_a_contact_lens_choice_left_blank_declines_the_form():
    """Every single choice is asked, optional on the form or not: a form is
    sent whole or not at all."""
    form = _run(CL_TRIAL)["forms"][1]
    answers = _cl_all_first(form)
    del answers["supply-options"]
    out = _run(_cl_only(), share={"eyeClTrial": True},
               answers={"eyeClTrial": answers})
    assert _status(out) == {"eyeClTrial": "declined"}
    assert out["response"]["artifacts"] == []


@pytest.mark.parametrize("req", [
    # Required and not answered here: a completed response cannot leave it
    # out.
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "trial-lens").update(
        type="date", required=True)), id="required-skipped"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "problems").update(
        type="attachment", required=True)), id="required-skipped-with-options"),
    # A kind FHIR does not have is not a question to leave out.
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "days-worn").update(
        type="slider")), id="unknown-type"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "days-worn").pop("type")),
                 id="no-type"),
    # Its words are shown, so they are held to the same check.
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        text="Anything else‮?")), id="typed-text-bidi"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        text="Notes ؜(0, 4)")), id="typed-text-mark"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        text="Notes א (0, 4)")), id="typed-text-rtl"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        text="​ㅤ")), id="typed-text-blank"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").pop("text")),
                 id="typed-text-missing"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        text="x" * 501)), id="typed-text-too-long"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        prefix="‮")), id="typed-prefix-bidi"),
    # The other refusals hold for it as for any question.
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        enableWhen=[{}])), id="typed-bad-enable-when"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        initial=[{"valueString": "presumed"}])), id="typed-initial"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        answerValueSet="http://example.org/vs")), id="typed-value-set"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        item=[{"linkId": "x", "type": "display", "text": "nested"}])),
        id="typed-with-items"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        linkId="overall")), id="typed-duplicate-linkid"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        extension=[{"url": "http://example.org/x", "valueBoolean": True}])),
        id="typed-with-extension"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        required="true")), id="typed-required-not-boolean"),
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "days-worn").update(
        answerOption=[{"valueInteger": 1}, {"valueInteger": 2}])),
        id="number-with-options"),
    # A skipped question may not be conditional.
    pytest.param(_cl_mutated(lambda q: _cl_item(q, "concerns").update(
        type="date", enableWhen=[{"question": "overall", "operator": "exists",
                                  "answerBoolean": True}])),
        id="skipped-enable-when"),
    # A heading over nothing to answer, or too much left unanswered.
    pytest.param(_cl_mutated(lambda q: q["item"].append(
        {"linkId": "notes", "type": "group", "text": "Notes",
         "item": [{"linkId": "note-1", "type": "date", "text": "When"}]})),
        id="group-of-skipped-only"),
    pytest.param(_cl_mutated(lambda q: q["item"].extend(
        {"linkId": f"note-{n}", "type": "date", "text": f"Date {n}"}
        for n in range(11))), id="too-many-skipped"),
])
def test_a_form_whose_other_questions_cannot_be_left_out_is_unsupported(req):
    out = _run(req, share={"eyeClTrial": True})
    assert out["forms"] == [None]
    assert _status(out) == {"eyeClTrial": "unsupported"}
    assert out["response"]["artifacts"] == []


def test_ten_skipped_questions_is_still_answered():
    req = _cl_mutated(lambda q: q["item"].extend(
        {"linkId": f"note-{n}", "type": "date", "text": f"Date {n}"}
        for n in range(10)))
    assert len(_run(req)["forms"][0]["skipped"]) == 10


def test_a_skipped_questions_options_are_never_read():
    """A skipped question's answerOption is never read (readItems): options
    that would refuse an answered question (too many, a null, no coding, a
    bidi display) leave the form answered, the question skipped.

    MUTATION: read them before skipping (`if (... answerOption
    .map(readOption).includes(null)) return false;`) -> red."""
    def garbage(q):
        it = _cl_item(q, "problems")
        it["type"] = "attachment"
        it["answerOption"] = (
            [None, {}, {"valueCoding": {"code": "x", "display": "‮oops"}},
             {"valueString": "free"}]
            + [{"valueCoding": {"code": f"c{n}", "display": f"O{n}"}}
               for n in range(50)])
    form = _run(_cl_mutated(garbage))["forms"][0]
    assert form is not None
    assert "problems" in [s["linkId"] for s in form["skipped"]]
    out = _run(_cl_mutated(garbage), share={"eyeClTrial": True},
               answers={"eyeClTrial": _cl_all_first(form)})
    assert _status(out) == {"eyeClTrial": "fulfilled"}


def test_a_form_with_nothing_skipped_lists_nothing():
    assert _run(_speed_only())["forms"][0]["skipped"] == []
