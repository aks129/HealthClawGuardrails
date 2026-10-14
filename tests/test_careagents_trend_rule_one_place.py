"""#942 (FHI-13): how a lab change is told, on every surface, and the chart.

1. On iMessage the model called an A1c drop "a move in a good direction" and
   an LDL drop "encouraging", 2 runs of 2. #938 told the web timeline not to
   call a change good or bad; the text note never got the sentence. The rule
   now lives in one constant, in the system prompt every turn gets and in
   every lab tool's note, and the tests below go red if any of them loses
   it or the constant loses its words.
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

from careagents import agent, personas
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


# --- 1. one trend rule, every turn ------------------------------------------
#
# QA on #953: with the rule in the timeline notes only, the follow-up "So is
# that good news?" got "Partly, yes", "encouraging" and "Moving in a good
# direction" in 3 of 4 real-model runs. That turn calls get_labs, or no tool
# at all, and the sunny persona said "Celebrate what's going well". So the
# rule is in the system prompt every turn gets, and in every lab tool's note.

def test_the_rule_says_not_to_judge_a_result_or_a_change():
    """MUTATION: empty the constant, or drop its "good or bad" -> red. The
    inclusion tests below would pass on an empty string; this one would not."""
    rule = personas.NO_TREND_VERDICT
    assert "good or bad" in rule
    for word in ("encouraging", "good direction", "good news"):
        assert word in rule, word
    # It holds when the person asks for a verdict, which is the QA case.
    assert "asks" in rule


def test_the_tool_notes_use_the_same_rule_as_the_prompt():
    """One wording, so the two cannot drift as the two notes did."""
    assert agent._TREND_NO_VERDICT is personas.NO_TREND_VERDICT


@pytest.mark.parametrize("persona", sorted(personas.PERSONAS))
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("sample", (False, True))
def test_every_system_prompt_carries_the_rule(persona, surface, sample):
    """MUTATION: drop the rule from SAFETY_CORE -> red for every persona,
    surface and source."""
    prompt = personas.system_prompt("Juniper", persona, surface=surface,
                                    sample=sample)
    assert personas.NO_TREND_VERDICT in prompt


def test_the_upbeat_voices_no_longer_ask_to_celebrate_results():
    """The sunny persona celebrated "what's going well in the records", and
    the healthy-habits routine "improvements": both read a lab or vital
    change as a win to cheer. They may still celebrate what the person did."""
    from careagents.advisors import ADVISORS
    sunny = personas.PERSONAS["sunny"]["voice"]
    assert "going well in the records" not in sunny
    assert "never call a lab" in sunny.lower()
    habits = ADVISORS["healthy-habits"]["guidance"]
    assert "improvements" not in habits
    assert "improved" not in habits


@pytest.mark.parametrize("surface", SURFACES)
def test_every_timeline_surface_carries_the_rule(surface):
    """MUTATION: leave the constant out of the text note, or out of the web
    note -> red for that surface.

    Checked by inclusion of the same object, not a copy of its words, so a
    second, drifting wording cannot satisfy this."""
    out = _timeline(_LabsHC(), "cholesterol", surface)
    assert out["series"], out
    assert agent._TREND_NO_VERDICT in out["note"]


@pytest.mark.parametrize("surface", SURFACES)
def test_get_labs_carries_the_rule(surface):
    """The follow-up QA caught reads labs through get_labs.

    MUTATION: drop the rule from get_labs' note -> red."""
    out = json.loads(_execute_tool(_LabsHC(), "t", "get_labs", {}, [],
                                   surface=surface))
    # Its own key: a fully scored answer carries no `note` at all
    # (test_labs_with_every_result_scored_carry_no_note).
    assert out["no_verdict_rule"] == agent._TREND_NO_VERDICT


@pytest.mark.parametrize("surface", SURFACES)
def test_the_visit_brief_carries_the_rule(surface):
    """The brief carries lab trends. MUTATION: drop the rule from the
    brief's notes -> red."""
    from tests.test_careagents_brief_lab_trends import _brief_with_trend

    class _HC:
        def fetch_appointment_brief(self, _t):
            return _brief_with_trend()
    out = json.loads(_execute_tool(_HC(), "t", "appointment_brief", {}, [],
                                   surface=surface))
    assert agent._TREND_NO_VERDICT in out["note"]


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
// Replayed text is the page's DOM text, which CodeQL treats as tainted
// (alert 124): every link it yields must carry one of two fixed hrefs.
const linked = bubble('**Hi** - write to contactus@healthclaw.io or ' +
  'mailto:x@evil.test, javascript:alert(1), or go to your hub. ' +
  'contactus@healthclaw.io.evil.test and your hubx too.');
renderReplayed(linked);
const hrefs = [];
(function walk(n) {
  if (n.nodeType !== 1) return;
  if (n.tagName === 'A') hrefs.push({ href: n.href, text: n.textContent });
  n.children.forEach(walk);
})(linked);
process.stdout.write(JSON.stringify({
  marked: tree(marked), plain: tree(plain), hrefs,
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


def test_a_replayed_bubble_links_only_to_the_two_fixed_hrefs():
    """CodeQL alert 124 (js/xss-through-dom): the href was built from the
    regex match, which comes from replayed DOM text. Both hrefs are fixed
    strings now, so no text in a bubble can choose where a link goes.

    MUTATION: build the mailto from the match again -> the source check is
    red; derive the href from the text -> the href check is red."""
    got = _node()["hrefs"]
    assert got, "the bubble produced no links at all"
    assert {h["href"] for h in got} == {"/home",
                                        "mailto:contactus@healthclaw.io"}
    assert all(h["text"] in ("your hub", "contactus@healthclaw.io")
               for h in got), got
    assert '"mailto:" + m[0]' not in CHAT_JS.read_text()
