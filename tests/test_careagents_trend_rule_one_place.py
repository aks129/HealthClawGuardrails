"""#942 (FHI-13): how a lab change is told, on every surface, and the chart.

1. On iMessage the model called an A1c drop "a move in a good direction" and
   an LDL drop "encouraging", 2 runs of 2. #938 told the web timeline not to
   call a change good or bad; the text note never got the sentence. The rule
   now lives in one constant that both notes are built from, and the tests
   below go red if either surface loses it or the constant loses its words.
2. A chat reopened from history showed the model's markdown as literal
   asterisks: chat.js rendered only live bubbles.
3. The chart's y-axis rounded to whole numbers, so an A1c chart read
   "9 / 9 / 8".
4. The chart's footnote was the engine's clinician wording.
5. The LDL answer offered HDL and triglycerides, which the records lack.
6. Dates reached the person as 2025-04-21.

The chat.js parts run under node, cut out of the file by name, as
tests/test_chat_markdown.py does. Synthetic data only.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from careagents import agent
from careagents.agent import _execute_tool
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _interpret_payload, cfg, svc)

CHAT_JS = (pathlib.Path(__file__).resolve().parents[1]
           / "careagents" / "static" / "chat.js")

SURFACES = ("web", "imessage", "sms")


class _LabsHC:
    def interpret_labs(self, _tenant):
        return _interpret_payload()


def _timeline(hc, topic, surface, tenant="t"):
    return json.loads(_execute_tool(hc, tenant, "show_lab_timeline",
                                    {"topic": topic}, [], surface=surface))


# --- 1. one trend rule, every surface ---------------------------------------

def test_the_rule_says_not_to_judge_the_change():
    """MUTATION: empty the constant, or drop its "good or bad" -> red. The
    surface tests below would pass on an empty string; this one would not."""
    rule = agent._TREND_NO_VERDICT
    assert "good or bad" in rule
    for word in ("encouraging", "good direction"):
        assert word in rule, word


@pytest.mark.parametrize("surface", SURFACES)
def test_every_timeline_surface_carries_the_one_rule(surface):
    """MUTATION: leave the constant out of the text note, or out of the web
    note -> red for that surface.

    Both are checked by inclusion of the same object, not a copy of its
    words, so a second, drifting wording cannot satisfy this."""
    out = _timeline(_LabsHC(), "cholesterol", surface)
    assert out["series"], out
    assert agent._TREND_NO_VERDICT in out["note"]


def test_no_note_carries_its_own_copy_of_the_rule():
    """The drift that caused #942 was two hand-written notes. The words live
    once in agent.py: in the constant, and nowhere else."""
    src = pathlib.Path(agent.__file__).read_text()
    assert src.count("good or bad") == 1, (
        "the trend rule is written out more than once in agent.py")


# --- 5. only the tests on file are offered ----------------------------------

@pytest.mark.parametrize("surface", ("web", "imessage"))
def test_the_ldl_answer_is_told_which_other_tests_are_on_file(
        cfg, svc, monkeypatch, surface):  # noqa: F811
    """The sample records have no HDL or triglycerides. The tool names the
    other charted tests that ARE on file, and the note limits any offer to
    those (the real engine, the real sample records).

    MUTATION: list every analyte the chart knows -> red on HDL."""
    chain = Chain(cfg, svc, monkeypatch)
    out = _timeline(chain.hc, "ldl", surface, tenant=TENANT)
    assert [s["name"] for s in out["series"]] == ["LDL cholesterol"]
    others = out["other_tests_on_file"]
    assert "LDL cholesterol" not in others
    assert "Hemoglobin A1c" in others
    assert "HDL cholesterol" not in others
    assert "Triglycerides" not in others
    assert agent._ONLY_TESTS_ON_FILE in out["note"]


# --- 6. readable dates ------------------------------------------------------

@pytest.mark.parametrize("surface", SURFACES)
def test_the_model_gets_dates_in_words(surface):
    """MUTATION: hand the model the ISO date again -> red."""
    out = _timeline(_LabsHC(), "cholesterol", surface)
    (s,) = out["series"]
    assert s["first"]["date"] == "January 1, 2025"
    assert s["latest"]["date"] == "March 1, 2026"


def test_a_date_that_is_not_a_calendar_date_is_not_rewritten():
    assert agent._date_in_words("2025-02-30") == "2025-02-30"
    assert agent._date_in_words("") == ""


# --- 4. the chart footnote --------------------------------------------------

def test_the_chart_footnote_is_plain(cfg, svc, monkeypatch):  # noqa: F811
    """MUTATION: pass the engine's disclaimer through again -> red."""
    from careagents.app import CHART_FOOTNOTE
    app, c, fake, agent_id, _tenant, _conn = _chat_app(cfg, svc, monkeypatch)
    payload = _interpret_payload()
    payload["disclaimer"] = (
        "Advisory decision support, not a diagnosis. The performing lab's "
        "own reference range takes precedence.")
    fake.interpret_labs = lambda t: payload
    body = c.get(f"/api/labs/timeline?agent={agent_id}").get_json()
    assert body["disclaimer"] == CHART_FOOTNOTE
    for jargon in ("Advisory", "decision support", "performing lab",
                   "precedence"):
        assert jargon not in CHART_FOOTNOTE, jargon
    assert "not a diagnosis" in CHART_FOOTNOTE


# --- 2, 3, 6: chat.js under node --------------------------------------------

_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');

function textNode(t) { return { nodeType: 3, text: String(t) }; }
function element(tag) {
  const n = { nodeType: 1, tagName: tag.toUpperCase(), children: [],
              className: '',
              appendChild(c) { this.children.push(c); return c; } };
  Object.defineProperty(n, 'textContent', {
    set(v) { this.children = v === '' ? [] : [textNode(v)]; },
    get() { return this.children.map(c => c.nodeType === 3 ? c.text
                                                     : c.textContent).join(''); },
  });
  return n;
}
globalThis.document = { createElement: element, createTextNode: textNode,
                        querySelector: () => null };

function cut(from, to) {
  const a = src.indexOf(from), b = src.indexOf(to, a);
  if (a < 0 || b < 0) throw new Error('marker not found: ' + from);
  return src.slice(a, b);
}
const code = 'function scroll() {}\n'
  + cut('function el(', 'function addUser(')
  + cut('function typewrite(', 'function addChip(')
  + cut('function axisLabels(', 'function drawSeries(')
  + 'return { renderReplayed, axisLabels, shortDate };';
const { renderReplayed, axisLabels, shortDate } = new Function(code)();

function tree(n) {
  if (n.nodeType === 3) return { text: n.text };
  return { tag: n.tagName.toLowerCase(), children: n.children.map(tree) };
}
// A replayed bubble as chat.html writes it: one <p> holding the text.
function bubble(text) {
  const b = element('div'); const p = element('p');
  p.textContent = text; b.appendChild(p); return b;
}
const marked = bubble('Your **A1c** went from 9.2% to 8.1%.');
renderReplayed(marked);
const plain = bubble('Nothing to lay out here.');
renderReplayed(plain);
process.stdout.write(JSON.stringify({
  marked: tree(marked), plain: tree(plain),
  a1c: axisLabels(7.935, 9.365),
  chol: axisLabels(204.9, 249.1),
  flat: axisLabels(5, 7),
  date: shortDate('2025-04-21'),
  bad: shortDate('2025-13-40'),
}));
"""


def _node() -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", _HARNESS, "--", str(CHAT_JS)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_replayed_answer_lays_out_its_markdown():
    """MUTATION: make renderReplayed a no-op -> red (the asterisks stay)."""
    got = _node()["marked"]
    assert got == {"tag": "div", "children": [
        {"text": "Your "},
        {"tag": "strong", "children": [{"text": "A1c"}]},
        {"text": " went from 9.2% to 8.1%."}]}


def test_a_replayed_answer_with_no_markdown_is_left_as_written():
    """The greeting bubbles carry their own <strong>: a bubble with nothing
    to lay out is not rebuilt, so they keep it."""
    got = _node()["plain"]
    assert got == {"tag": "div", "children": [
        {"tag": "p", "children": [{"text": "Nothing to lay out here."}]}]}


def test_the_page_lays_out_the_replayed_answers_on_load():
    src = CHAT_JS.read_text()
    assert 'log.querySelectorAll(".msg.agent").forEach(renderReplayed)' in src


def test_the_y_axis_never_repeats_a_label():
    """MUTATION: round every label to a whole number again -> red on A1c."""
    got = _node()
    assert len(set(got["a1c"])) == 3, got["a1c"]
    assert got["a1c"] == ["9.4", "8.7", "7.9"]
    # Whole numbers when they are already distinct.
    assert got["chol"] == ["249", "227", "205"]
    assert got["flat"] == ["7", "6", "5"]


def test_the_chart_dates_are_readable():
    got = _node()
    assert got["date"] == "Apr 21, 2025"
    assert got["bad"] == "2025-13-40"
