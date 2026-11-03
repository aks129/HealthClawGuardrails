"""The physician-authored form in the SMART Health Check-in wallet.

The connectathon's `physician-form` case (spec §5.4.2) sends a semaglutide
check-in inline (e2e/fixtures/checkin-request-form-semaglutide.json, the
organizers' form-semaglutide.json as published, synthetic). It has a whole
number, yes/no, single choices, a pick-several, free text, a question that
takes picks and typed words at once, and two items shown only when an
earlier answer calls for them (`enableWhen`). What these pin, by running the
shipped careagents/static/checkin_answer.js under node:

- Every item kind is read, and each answer goes as the type the form
  declares, under the item's canonical exactly.
- enableWhen is evaluated with the plain FHIR operators only, against
  answers given earlier in this form; `all` and `any` both hold.
- A hidden question's answer is never sent, whatever was given for it.
- Anything else (an unknown operator, a later or unknown question, an
  expression, a contained ValueSet, too many conditions) makes the whole
  form unsupported, and a typed answer out of bounds declines it.

Synthetic data only.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_checkin_wallet_phq2 import STATIC, _run, _status

pytestmark = pytest.mark.extension

ROOT = Path(__file__).resolve().parent.parent
REQUEST = json.loads((ROOT / "e2e" / "fixtures"
                      / "checkin-request-form-semaglutide.json").read_text())
CANONICAL = ("https://smart-health-checkin.org/connectathon/Questionnaire/"
             "semaglutide-4-week-checkin.json")
SYSTEM = ("https://smart-health-checkin.org/connectathon/CodeSystem/"
          "semaglutide-checkin")
# The single choices, each answered with its first option ("No", "None"...).
MUST = {"missed": 0, "nausea": 0, "vomiting": 0, "bowels": 0, "appetite": 0,
        "next-dose": 0}

_STATE = """
const m = await import(process.argv[1]);
const form = m.readForm(JSON.parse(process.argv[2]).items[0]);
const s = form && m.formState(form, JSON.parse(process.argv[3]));
console.log(JSON.stringify(s && { shown: [...s.shown], missing: s.missing,
  invalid: s.invalid, answered: [...s.answers.keys()] }));
"""


def _state(req, picked):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "--input-type=module", "-e", _STATE,
         str(STATIC / "checkin_answer.js"), json.dumps(req),
         json.dumps(picked)],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def _q(req):
    return req["items"][0]["content"]["questionnaire"]


def _item(q, link_id):
    return next(i for i in q["item"] if i["linkId"] == link_id)


def _mutated(fn):
    req = copy.deepcopy(REQUEST)
    fn(_q(req))
    return req


def _send(answers, req=REQUEST):
    return _run(req, share={"semaglutide": True},
                answers={"semaglutide": answers})


def _qr(out):
    [art] = out["response"]["artifacts"]
    return art["value"]


def _sent(out):
    return {i["linkId"]: i["answer"] for i in _qr(out)["item"]}


def _coding(code):
    return {"valueCoding": {"system": SYSTEM, "code": code,
                            "display": next(
                                o["valueCoding"]["display"]
                                for i in _q(REQUEST)["item"]
                                for o in i.get("answerOption", [])
                                if o["valueCoding"]["code"] == code)}}


# --- every item kind --------------------------------------------------------------

def test_every_item_kind_on_the_form_is_read():
    form = _run(REQUEST)["forms"][0]
    assert form["canonical"] == CANONICAL
    assert form["skipped"] == []
    assert [(q["linkId"], q["kind"], q["repeats"]) for q in form["questions"]] \
        == [("doses-taken", "integer", False), ("missed", "choice", False),
            ("missed-why", "text", False),
            ("pen-trouble", "open-choice", True),
            ("nausea", "choice", False), ("vomiting", "choice", False),
            ("bowels", "choice", False), ("fluids", "boolean", False),
            ("appetite", "choice", False), ("low-sugar", "boolean", False),
            ("red-flags", "choice", True), ("next-dose", "choice", False),
            ("questions", "text", False)]
    # The intro is shown above the questions; the red-flag note only in
    # its place, and only when it holds.
    assert form["displays"] == [_item(_q(REQUEST), "intro")["text"]]
    assert [n["linkId"] for n in form["notes"]] == ["red-flags-note"]


def test_a_full_set_of_answers_goes_as_the_declared_types():
    out = _send(MUST | {
        "doses-taken": " 4 ", "missed": 1, "missed-why": "I was travelling",
        "pen-trouble": {"picked": [1], "other": "The needle bent"},
        "fluids": True, "low-sugar": False, "red-flags": [0, 4],
        "questions": "Can I take it at night?"})
    assert out["problems"] == []
    assert _status(out) == {"semaglutide": "fulfilled"}
    qr = _qr(out)
    assert qr["questionnaire"] == CANONICAL and qr["status"] == "completed"
    sent = _sent(out)
    assert sent["doses-taken"] == [{"valueInteger": 4}]
    assert sent["missed"] == [_coding("once")]
    assert sent["missed-why"] == [{"valueString": "I was travelling"}]
    assert sent["pen-trouble"] == [_coding("site"),
                                   {"valueString": "The needle bent"}]
    assert sent["fluids"] == [{"valueBoolean": True}]
    assert sent["low-sugar"] == [{"valueBoolean": False}]
    assert sent["red-flags"] == [_coding("belly-pain"), _coding("vision")]
    assert sent["questions"] == [{"valueString": "Can I take it at night?"}]
    # In the form's order; display lines are never sent.
    assert list(sent) == [q["linkId"] for q in out["forms"][0]["questions"]]


def test_typed_words_alone_answer_the_pen_question():
    out = _send(MUST | {"pen-trouble": {"picked": [], "other": "Hard to click"}})
    assert _sent(out)["pen-trouble"] == [{"valueString": "Hard to click"}]


def test_only_what_was_answered_is_sent():
    out = _send(MUST)
    assert _status(out) == {"semaglutide": "fulfilled"}
    assert list(_sent(out)) == list(MUST)


def test_a_single_choice_left_blank_declines_the_form():
    out = _send({k: v for k, v in MUST.items() if k != "nausea"})
    assert _status(out) == {"semaglutide": "declined"}


# --- enableWhen -------------------------------------------------------------------

def test_the_why_question_shows_for_once_or_more_than_once():
    """enableBehavior `any` over two `=` conditions on a coding."""
    assert "missed-why" not in _state(REQUEST, MUST)["shown"]
    assert "missed-why" not in _state(REQUEST, {})["shown"]
    for pick in (1, 2):
        assert "missed-why" in _state(REQUEST, MUST | {"missed": pick})["shown"]


def test_all_needs_every_condition():
    req = _mutated(lambda q: _item(q, "missed-why").update(
        enableBehavior="all"))
    # "once" and "more" cannot both be the one answer.
    for pick in (0, 1, 2):
        assert "missed-why" not in _state(req, MUST | {"missed": pick})["shown"]


def test_the_red_flag_note_shows_only_when_one_is_checked():
    assert "red-flags-note" not in _state(REQUEST, MUST)["shown"]
    assert "red-flags-note" in _state(
        REQUEST, MUST | {"red-flags": [2]})["shown"]
    assert "red-flags-note" not in _state(
        REQUEST, MUST | {"red-flags": []})["shown"]


def _gated(operator, key, value, kind="integer", options=None):
    """A form with one question `n` of `kind`, and `g` shown when
    `n <operator> value`."""
    def fn(q):
        n = {"linkId": "n", "type": kind, "text": "N?"}
        if options:
            n["answerOption"] = options
        q["item"] = [
            _item(q, "nausea"), n,
            {"linkId": "g", "type": "string", "text": "Gated?",
             "enableWhen": [{"question": "n", "operator": operator,
                             key: value}]}]
    return _mutated(fn)


@pytest.mark.parametrize("operator,value,answer,shown", [
    ("exists", True, "5", True), ("exists", True, None, False),
    ("exists", False, None, True), ("exists", False, "5", False),
    ("=", 5, "5", True), ("=", 4, "5", False), ("=", 5, None, False),
    ("!=", 4, "5", True), ("!=", 5, "5", False), ("!=", 5, None, True),
    (">", 4, "5", True), (">", 5, "5", False),
    ("<", 6, "5", True), ("<", 5, "5", False),
    (">=", 5, "5", True), (">=", 6, "5", False),
    ("<=", 5, "5", True), ("<=", 4, "5", False),
])
def test_each_operator_on_a_number(operator, value, answer, shown):
    key = "answerBoolean" if operator == "exists" else "answerInteger"
    picked = {"nausea": 0} | ({} if answer is None else {"n": answer})
    assert ("g" in _state(_gated(operator, key, value), picked)["shown"]) is shown


def test_a_decimal_compares_with_an_integer_condition():
    req = _gated(">", "answerInteger", 2, kind="decimal")
    assert "g" in _state(req, {"nausea": 0, "n": "2.5"})["shown"]
    assert "g" not in _state(req, {"nausea": 0, "n": "1.5"})["shown"]


@pytest.mark.parametrize("operator,key,value,kind,answer,shown", [
    ("=", "answerBoolean", True, "boolean", True, True),
    ("=", "answerBoolean", True, "boolean", False, False),
    ("!=", "answerBoolean", True, "boolean", False, True),
    ("=", "answerString", "yes", "text", "yes", True),
    ("=", "answerString", "yes", "text", "no", False),
    ("!=", "answerString", "yes", "text", "no", True),
])
def test_equality_on_booleans_and_words(operator, key, value, kind, answer,
                                        shown):
    req = _gated(operator, key, value, kind=kind)
    assert ("g" in _state(req, {"nausea": 0, "n": answer})["shown"]) is shown


def test_a_coding_compares_by_system_and_code_never_its_display():
    options = _item(_q(REQUEST), "missed")["answerOption"]
    other_display = {"system": SYSTEM, "code": "once", "display": "Whatever"}
    req = _gated("=", "answerCoding", other_display, kind="choice",
                 options=options)
    assert "g" in _state(req, {"nausea": 0, "n": 1})["shown"]
    wrong_system = {"system": "http://example.org", "code": "once"}
    req = _gated("=", "answerCoding", wrong_system, kind="choice",
                 options=options)
    assert "g" not in _state(req, {"nausea": 0, "n": 1})["shown"]


def test_a_hidden_question_counts_as_unanswered_for_the_items_after_it():
    """missed-why is hidden while "missed" is "No"; an item gated on
    missed-why existing stays hidden, whatever was typed into it."""
    def fn(q):
        q["item"].append({"linkId": "after", "type": "string", "text": "And?",
                          "enableWhen": [{"question": "missed-why",
                                          "operator": "exists",
                                          "answerBoolean": True}]})
    req = _mutated(fn)
    s = _state(req, MUST | {"missed-why": "typed before", "after": "x"})
    assert "missed-why" not in s["shown"] and "after" not in s["shown"]
    assert s["answered"] == list(MUST)


# --- a hidden answer is never sent ------------------------------------------------

def test_an_answer_to_a_hidden_question_is_dropped():
    """The person typed why, then changed "missed" back to "No": the why is
    hidden, so it is not sent.

    MUTATION: in formState, read and keep the answer of a row whose
    enableWhen does not hold -> red."""
    out = _send(MUST | {"missed-why": "I forgot"})
    assert _status(out) == {"semaglutide": "fulfilled"}
    assert "missed-why" not in _sent(out)
    assert "I forgot" not in json.dumps(out["response"])


def test_a_bad_answer_to_a_hidden_question_does_not_block_sending():
    out = _send(MUST | {"missed-why": ["not", "a", "string"]})
    assert _status(out) == {"semaglutide": "fulfilled"}


def test_a_hidden_required_question_is_not_missing():
    req = _mutated(lambda q: _item(q, "missed-why").update(required=True))
    assert _status(_send(MUST, req)) == {"semaglutide": "fulfilled"}
    assert _status(_send(MUST | {"missed": 1}, req)) == {
        "semaglutide": "declined"}
    out = _send(MUST | {"missed": 1, "missed-why": "Away"}, req)
    assert _sent(out)["missed-why"] == [{"valueString": "Away"}]


# --- bounded answers --------------------------------------------------------------

@pytest.mark.parametrize("answer", [
    pytest.param({"doses-taken": "1234567890"}, id="ten-digit-integer"),
    pytest.param({"doses-taken": "4.5"}, id="integer-with-a-point"),
    pytest.param({"doses-taken": "1e3"}, id="integer-exponent"),
    pytest.param({"doses-taken": 4}, id="integer-not-typed"),
    pytest.param({"questions": "x" * 2001}, id="text-too-long"),
    pytest.param({"fluids": "yes"}, id="boolean-not-boolean"),
    pytest.param({"red-flags": [0, 0]}, id="pick-twice"),
    pytest.param({"red-flags": [5]}, id="pick-out-of-range"),
    pytest.param({"red-flags": 1}, id="pick-several-not-a-list"),
    pytest.param({"pen-trouble": {"picked": [0], "other": 7}},
                 id="typed-not-a-string"),
    pytest.param({"pen-trouble": [0]}, id="pick-or-type-not-an-object"),
])
def test_an_answer_out_of_bounds_declines_the_form(answer):
    assert _status(_send(MUST | answer)) == {"semaglutide": "declined"}


def test_the_bounds_themselves_are_answers():
    out = _send(MUST | {"doses-taken": "-999999999", "questions": "x" * 2000})
    assert _sent(out)["doses-taken"] == [{"valueInteger": -999999999}]
    req = _mutated(lambda q: _item(q, "doses-taken").update(type="decimal"))
    out = _send(MUST | {"doses-taken": "999999999.123456"}, req)
    assert _sent(out)["doses-taken"] == [{"valueDecimal": 999999999.123456}]
    out = _send(MUST | {"doses-taken": "1.1234567"}, req)
    assert _status(out) == {"semaglutide": "declined"}


def test_one_pick_or_typed_words_but_not_both_when_it_does_not_repeat():
    req = _mutated(lambda q: _item(q, "pen-trouble").pop("repeats"))
    both = {"pen-trouble": {"picked": [0], "other": "And this"}}
    assert _status(_send(MUST | both, req)) == {"semaglutide": "declined"}
    one = {"pen-trouble": {"picked": [], "other": "Just this"}}
    assert _sent(_send(MUST | one, req))["pen-trouble"] == [
        {"valueString": "Just this"}]


# --- what makes the form unsupported ----------------------------------------------

def _why(**changes):
    return _mutated(lambda q: _item(q, "missed-why").update(**changes))


def _cond(**changes):
    def fn(q):
        _item(q, "missed-why")["enableWhen"][0].update(changes)
    return _mutated(fn)


EXPRESSION = {"valueExpression": {"language": "text/fhirpath",
                                  "expression": "true"}}
SDC = "http://hl7.org/fhir/uv/sdc/StructureDefinition/sdc-questionnaire-"


@pytest.mark.parametrize("req", [
    pytest.param(_cond(operator="matches"), id="unknown-operator"),
    pytest.param(_cond(operator="in"), id="unknown-operator-in"),
    pytest.param(_cond(operator=None), id="no-operator"),
    pytest.param(_cond(operator=">"), id="ordering-a-coding"),
    pytest.param(_cond(question="later-one"), id="unknown-question"),
    pytest.param(_cond(question="questions"), id="a-later-question"),
    pytest.param(_cond(question="missed-why"), id="itself"),
    pytest.param(_cond(question="intro"), id="a-display-line"),
    pytest.param(_cond(answerInteger=1), id="two-answers"),
    pytest.param(_mutated(lambda q: _item(q, "missed-why")["enableWhen"][0]
                          .pop("answerCoding")), id="no-answer"),
    pytest.param(_mutated(lambda q: _item(q, "missed-why").update(
        enableWhen=[{"question": "missed", "operator": "=",
                     "answerInteger": 1}])), id="type-mismatch"),
    pytest.param(_mutated(lambda q: _item(q, "missed-why").update(
        enableWhen=[{"question": "missed", "operator": "exists",
                     "answerCoding": {"code": "once"}}])),
        id="exists-without-a-boolean"),
    pytest.param(_mutated(lambda q: _item(q, "missed-why").update(
        enableWhen=[{"question": "missed", "operator": "=",
                     "answerExpression": {"expression": "true"}}])),
        id="answer-is-an-expression"),
    pytest.param(_cond(extension=[{"url": "http://example.org/x",
                                   "valueBoolean": True}]),
                 id="condition-with-extension"),
    pytest.param(_mutated(lambda q: _item(q, "missed-why").pop(
        "enableBehavior")), id="two-conditions-no-behavior"),
    pytest.param(_why(enableBehavior="most"), id="unknown-behavior"),
    pytest.param(_mutated(lambda q: _item(q, "questions").update(
        enableBehavior="all")), id="behavior-without-conditions"),
    pytest.param(_why(enableWhen=[]), id="no-conditions"),
    pytest.param(_why(enableWhen={"question": "missed"}),
                 id="conditions-not-a-list"),
    pytest.param(_why(enableWhen=[_item(_q(REQUEST), "missed-why")
                                  ["enableWhen"][0]] * 6),
                 id="six-conditions"),
    # Expressions, anywhere, on any item: never run, never ignored.
    pytest.param(_mutated(lambda q: _item(q, "nausea").update(extension=[
        {"url": SDC + "enableWhenExpression"} | EXPRESSION])),
        id="enable-when-expression-on-a-choice"),
    pytest.param(_mutated(lambda q: _item(q, "intro").update(extension=[
        {"url": SDC + "calculatedExpression"} | EXPRESSION])),
        id="calculated-expression-on-a-display"),
    pytest.param(_mutated(lambda q: _item(q, "nausea").update(extension=[
        {"url": SDC + "answerExpression"} | EXPRESSION])),
        id="answer-expression"),
    pytest.param(_mutated(lambda q: _item(q, "nausea").update(extension=[
        {"url": SDC + "initialExpression"} | EXPRESSION])),
        id="initial-expression"),
    pytest.param(_mutated(lambda q: q.update(extension=[
        {"url": "http://hl7.org/fhir/StructureDefinition/variable"}
        | EXPRESSION])), id="variable-on-the-form"),
    pytest.param(_mutated(lambda q: q.update(contained=[
        {"resourceType": "ValueSet", "id": "vs"}])), id="contained-value-set"),
    pytest.param(_mutated(lambda q: _item(q, "nausea").update(
        answerValueSet="#vs")), id="value-set-reference"),
    pytest.param(_mutated(lambda q: _item(q, "questions").update(
        initial=[{"valueString": "prefilled"}])), id="prefilled"),
])
def test_a_form_this_wallet_will_not_evaluate_is_unsupported(req):
    out = _send(MUST, req)
    assert out["forms"] == [None]
    assert _status(out) == {"semaglutide": "unsupported"}
    assert out["response"]["artifacts"] == []


@pytest.mark.parametrize("operator", ["matches", "in", "~", "", None, 1])
def test_an_unknown_operator_on_a_number_is_not_evaluated(operator):
    """On a number every comparison type is allowed, so only the operator
    allow-list stands between an unknown operator and an evaluation.

    MUTATION: drop `!FORM_OPERATORS.includes(operator)` in readCondition
    -> red."""
    req = _gated(operator, "answerInteger", 5)
    out = _run(req, share={"semaglutide": True},
               answers={"semaglutide": {"nausea": 0, "n": "5"}})
    assert out["forms"] == [None]
    assert _status(out) == {"semaglutide": "unsupported"}


def _many_conditions(n):
    """`n` single-condition items gated on "missed", sized well under the
    32 KB cap, so the count is what refuses."""
    def fn(q):
        q["item"] = [_item(q, "missed")] + [
            {"linkId": f"w{i}", "type": "display", "text": f"Note {i}",
             "enableWhen": [{"question": "missed", "operator": "exists",
                             "answerBoolean": True}]}
            for i in range(n)]
    return _mutated(fn)


def test_thirty_conditions_are_read_and_thirty_one_are_not():
    assert len(json.dumps(_many_conditions(31))) < 16000
    assert _run(_many_conditions(30))["forms"][0] is not None
    assert _run(_many_conditions(31))["forms"] == [None]


def test_five_conditions_on_one_item_are_read():
    cond = _item(_q(REQUEST), "missed-why")["enableWhen"][0]
    assert _run(_why(enableWhen=[cond] * 5))["forms"][0] is not None


def test_nothing_is_filled_in_to_start_with():
    """No answer is taken from the form: with none given, nothing but
    `missing` single choices comes back, and nothing is sent."""
    s = _state(REQUEST, {})
    assert s["answered"] == [] and s["invalid"] == []
    assert s["missing"] == list(MUST)
    out = _run(REQUEST, share={"semaglutide": True})
    assert _status(out) == {"semaglutide": "declined"}
