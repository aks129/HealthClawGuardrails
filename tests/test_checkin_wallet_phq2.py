"""PHQ-2 in the SMART Health Check-in wallet (FHI-55).

The connectathon's "fill in a form" scenario sends the PHQ-2 inline, as a
`form.fhir` item carrying the Questionnaire itself
(e2e/fixtures/checkin-request-form-phq2.json is the organizers'
form-phq2.json, as published). What these pin, by running the shipped
careagents/static/checkin_answer.js under node:

- The questions and answer options come from the Questionnaire the website
  sent, never from a copy of ours.
- The answer is one QuestionnaireResponse whose `questionnaire` is the
  item's `questionnaireCanonical` exactly, a `|1` version included, and the
  vendored library's own pre-flight check finds the response clean.
- Per-item consent: unchosen is declined, and so is an incomplete or
  tampered set of answers. Nothing is guessed.
- Only a small, flat, single-choice form is answered. Anything else (no
  inline Questionnaire, too large, nested, free text, a value set, too many
  questions) is `unsupported`, never half-rendered.
- The answers never reach CareAgents: the form asks the relay for nothing,
  and the page posts nothing new.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "careagents" / "static"
REQUEST = json.loads((ROOT / "e2e" / "fixtures"
                      / "checkin-request-form-phq2.json").read_text())
CANONICAL = ("https://smart-health-checkin.org/connectathon/Questionnaire/"
             "phq-2.json")

_RUN = """
const m = await import(process.argv[1]);
const lib = await import(process.argv[2]);
const request = JSON.parse(process.argv[3]);
const share = JSON.parse(process.argv[4]);
const answers = JSON.parse(process.argv[5]);
const matches = m.matchItems(request, [], m.prepareRecords({ entry: [] }));
const response = m.buildResponse(request, matches, share, answers);
console.log(JSON.stringify({
  types: m.requestedTypes(request, []),
  forms: matches.map((x) => x.form || null),
  unsupported: matches.map((x) => x.unsupported),
  response,
  problems: lib.checkWalletResponse(request, response),
}));
"""


def _run(request, share=None, answers=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "--input-type=module", "-e", _RUN,
         str(STATIC / "checkin_answer.js"),
         str(STATIC / "vendor" / "smart-health-checkin-client" / "0.4.4"
             / "wallet.js"),
         json.dumps(request), json.dumps(share or {}),
         json.dumps(answers or {})],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def _phq2_only(**content_changes):
    req = copy.deepcopy(REQUEST)
    req["items"] = [i for i in req["items"] if i["id"] == "phq2"]
    req["items"][0]["content"].update(content_changes)
    return req


def _status(out):
    return {s["item"]: s["status"] for s in out["response"]["requestStatus"]}


def _q(req):
    return req["items"][0]["content"]["questionnaire"]


# --- reading the form -------------------------------------------------------------

def test_the_form_is_read_from_the_questionnaire_the_website_sent():
    out = _run(REQUEST)
    patient, form = out["forms"]
    assert patient is None and out["unsupported"] == [False, False]
    assert form["canonical"] == CANONICAL
    assert form["title"] == "PHQ-2 depression screen"
    assert form["displays"] == ["Over the last 2 weeks, how often have you "
                                "been bothered by the following problems?"]
    assert [(q["linkId"], q["prefix"], q["text"]) for q in form["questions"]] \
        == [("phq2-1", "1.", "Little interest or pleasure in doing things"),
            ("phq2-2", "2.", "Feeling down, depressed, or hopeless")]
    for q in form["questions"]:
        assert [o["display"] for o in q["options"]] == [
            "Not at all", "Several days", "More than half the days",
            "Nearly every day"]
    # The website's own words, so a changed option is what is shown.
    req = copy.deepcopy(REQUEST)
    opt = req["items"][1]["content"]["questionnaire"]["item"][1][
        "answerOption"][0]["valueCoding"]
    opt["display"] = "Never"
    assert _run(req)["forms"][1]["questions"][0]["options"][0][
        "display"] == "Never"


def test_the_form_asks_the_records_relay_for_nothing():
    assert _run(REQUEST)["types"] == ["Patient"]
    assert _run(_phq2_only())["types"] == []


# --- the answer -------------------------------------------------------------------

def test_answers_go_back_as_one_questionnaire_response_the_library_accepts():
    out = _run(REQUEST, share={"patient": False, "phq2": True},
               answers={"phq2": {"phq2-1": 1, "phq2-2": 3}})
    assert out["problems"] == []
    assert _status(out) == {"patient": "unavailable", "phq2": "fulfilled"}
    [art] = out["response"]["artifacts"]
    assert art["fulfills"] == ["phq2"]
    assert art["mediaType"] == "application/fhir+json"
    qr = art["value"]
    assert qr["resourceType"] == "QuestionnaireResponse"
    assert qr["status"] == "completed"
    assert qr["questionnaire"] == CANONICAL
    assert [(i["linkId"], i["answer"]) for i in qr["item"]] == [
        ("phq2-1", [{"valueCoding": {"system": "http://loinc.org",
                                     "code": "LA6569-3",
                                     "display": "Several days"}}]),
        ("phq2-2", [{"valueCoding": {"system": "http://loinc.org",
                                     "code": "LA6571-9",
                                     "display": "Nearly every day"}}]),
    ]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", qr["authored"])


def test_a_versioned_canonical_is_echoed_exactly():
    req = _phq2_only(questionnaireCanonical=CANONICAL + "|1")
    out = _run(req, share={"phq2": True},
               answers={"phq2": {"phq2-1": 0, "phq2-2": 0}})
    assert out["problems"] == []
    assert out["response"]["artifacts"][0]["value"]["questionnaire"] == \
        CANONICAL + "|1"


def test_without_a_canonical_the_inline_url_and_version_name_it():
    req = _phq2_only()
    del req["items"][0]["content"]["questionnaireCanonical"]
    out = _run(req, share={"phq2": True},
               answers={"phq2": {"phq2-1": 2, "phq2-2": 1}})
    assert out["problems"] == []
    assert out["response"]["artifacts"][0]["value"]["questionnaire"] == \
        CANONICAL + "|1"


@pytest.mark.parametrize("share,answers", [
    ({}, {"phq2": {"phq2-1": 1, "phq2-2": 1}}),          # not chosen
    ({"phq2": False}, {"phq2": {"phq2-1": 1, "phq2-2": 1}}),
    ({"phq2": True}, {}),                                  # no answers
    ({"phq2": True}, {"phq2": {"phq2-1": 1}}),             # one missing
    ({"phq2": True}, {"phq2": {"phq2-1": 1, "phq2-2": 4}}),  # no such option
    ({"phq2": True}, {"phq2": {"phq2-1": 1, "phq2-2": -1}}),
    ({"phq2": True}, {"phq2": {"phq2-1": "1", "phq2-2": 1}}),
    ({"phq2": True}, {"phq2": {"phq2-1": 1, "phq2-2": 1.5}}),
])
def test_unchosen_or_incomplete_answers_are_declined(share, answers):
    out = _run(_phq2_only(), share=share, answers=answers)
    assert _status(out) == {"phq2": "declined"}
    assert out["response"]["artifacts"] == []
    assert out["problems"] == []


# --- what is not answered ---------------------------------------------------------

def _without_inline():
    req = _phq2_only()
    del req["items"][0]["content"]["questionnaire"]
    return req


def _mutated(fn):
    req = _phq2_only()
    fn(_q(req))
    return req


def _big(q):
    q["item"][0]["text"] = "x" * 400
    q["item"].extend(copy.deepcopy(q["item"][0]) for _ in range(120))


@pytest.mark.parametrize("req", [
    pytest.param(_without_inline(), id="canonical-only"),
    pytest.param(_mutated(_big), id="oversized"),
    pytest.param(_mutated(lambda q: q["item"][1].update(
        item=[{"linkId": "x", "type": "display", "text": "nested"}])),
        id="nested"),
    pytest.param(_mutated(lambda q: q["item"][1].update(type="string")),
                 id="free-text"),
    pytest.param(_mutated(lambda q: q["item"][1].update(
        answerValueSet="http://example.org/vs")), id="value-set"),
    pytest.param(_mutated(lambda q: q["item"][1].update(repeats=True)),
                 id="repeats"),
    pytest.param(_mutated(lambda q: q["item"][1].update(enableWhen=[{}])),
                 id="enable-when"),
    pytest.param(_mutated(lambda q: q["item"][1]["answerOption"][0].update(
        valueCoding={"system": "http://loinc.org"})), id="option-no-code"),
    pytest.param(_mutated(lambda q: q["item"][1]["answerOption"][0].update(
        valueCoding={"code": "x", "display": {"div": "<b>x</b>"}})),
        id="option-display-not-text"),
    pytest.param(_mutated(lambda q: q["item"][1].update(text=["x"])),
                 id="text-not-a-string"),
    pytest.param(_mutated(lambda q: q["item"][2].update(linkId="phq2-1")),
                 id="duplicate-linkid"),
    pytest.param(_mutated(lambda q: q.update(item=[q["item"][0]])),
                 id="no-questions"),
    pytest.param(_mutated(lambda q: q["item"].extend(
        copy.deepcopy(q["item"][1]) | {"linkId": f"q{n}"}
        for n in range(12))), id="too-many-questions"),
    pytest.param(_mutated(lambda q: q.update(resourceType="Patient")),
                 id="not-a-questionnaire"),
])
def test_anything_but_a_small_flat_choice_form_is_unsupported(req):
    out = _run(req, share={"phq2": True},
               answers={"phq2": {"phq2-1": 0, "phq2-2": 0}})
    assert out["forms"] == [None]
    assert _status(out) == {"phq2": "unsupported"}
    assert out["response"]["artifacts"] == []


# --- whose heading ----------------------------------------------------------------

_HEADINGS = """
const m = await import(process.argv[1]);
const request = JSON.parse(process.argv[2]);
const matches = m.matchItems(request, [], m.prepareRecords({ entry: [] }));
console.log(JSON.stringify(matches.map((x) => ({
  heading: m.itemHeading(x), website: m.websiteHeading(x) }))));
"""


def test_a_heading_from_the_website_is_known_as_the_websites():
    """A records item is named by our heading; one with no kind of record
    to name it by (a form) carries the website's title, which the page
    shows attributed (#1030 security F3 follow-up)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    req = copy.deepcopy(REQUEST)
    req["items"][1]["title"] = "CareAgents verified clinic"
    req["items"].append({"id": "odd", "title": {"not": "text"},
                         "accept": ["application/fhir+json"],
                         "content": {"kind": "form.fhir"}})
    out = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", _HEADINGS,
         str(STATIC / "checkin_answer.js"), json.dumps(req)],
        capture_output=True, text=True, check=True, timeout=30).stdout)
    assert out == [
        {"heading": "Your name, birth date and contact details",
         "website": None},
        {"heading": "CareAgents verified clinic",
         "website": "CareAgents verified clinic"},
        # Never a non-string from the request: its id instead.
        {"heading": "odd", "website": "odd"},
    ]


# --- the page ---------------------------------------------------------------------

def _code(path):
    """Source without comments."""
    src = path.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def test_the_page_never_parses_website_text_as_markup():
    page = _code(STATIC / "checkin_wallet.js")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                   "document.write", "DOMParser", "createContextualFragment"):
        assert banned not in page, banned
    answer = _code(STATIC / "checkin_answer.js")
    # The Questionnaire's generated narrative is never read.
    assert '"div"' not in answer and "'div'" not in answer
