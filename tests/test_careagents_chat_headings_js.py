"""A markdown heading in a chat answer is laid out, never parsed as HTML,
and "This wasn't right" follows the turn's language (live 2026-10-26).

An older answer showed a raw "### What the Guidelines..." line: chat.js
rendered **bold**, *italic* and lists, and no heading. A heading line now
becomes a fixed <h3> built from text nodes, so a hostile heading is text.

Run under node against the real functions cut from chat.js, with a minimal
DOM, as tests/test_careagents_chat_paragraphs_js.py does.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

CHAT_JS = (pathlib.Path(__file__).resolve().parents[1]
           / "careagents" / "static" / "chat.js")

_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function cutConst(name) {
  const m = src.match(new RegExp('const ' + name + ' = [\\s\\S]*?;\\n'));
  if (!m) throw new Error('not found: ' + name);
  return m[0];
}
function cutFn(name) {
  const m = src.match(new RegExp('function ' + name + '\\([\\s\\S]*?\\n  }\\n'));
  if (!m) throw new Error('not found: ' + name);
  return m[0];
}
class Text { constructor(t) { this.data = t; }
  get textContent() { return this.data; } }
class Elem {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = [];
                     this.after = []; }
  appendChild(c) { this.children.push(c); return c; }
  get textContent() { return this.children.map(c => c.textContent).join(''); }
  set textContent(t) { this.children = t ? [new Text(t)] : []; }
  set className(_) {}
  set href(_) {}
  insertAdjacentElement(_, n) { this.after.push(n); }
  addEventListener() {}
}
function tags(n) {
  return [n.tagName || '#text'].concat(
    (n.children || []).flatMap(tags));
}
const document = {
  createElement: t => new Elem(t),
  createTextNode: t => new Text(t),
};
const code = cutConst('BOLD_OR_ITALIC') + cutConst('LIST_ITEM')
  + cutConst('HEADING') + cutConst('LINKS') + cutConst('FLAG_ERRORS')
  + cutConst('FLAG_WORDS')
  + cutFn('el') + cutFn('appendLinked') + cutFn('appendInline')
  + cutFn('renderMarkdown') + cutFn('withoutMarkers')
  + 'let rendered = 0; const real = renderMarkdown;'
  + 'renderMarkdown = function (n, t) { rendered++; return real(n, t); };'
  + cutFn('renderReplayed') + cutFn('addFlag')
  + 'return {renderMarkdown: real, withoutMarkers, renderReplayed, addFlag,'
  + ' count: () => rendered};';
const f = new Function('document', code)(document);
const cases = JSON.parse(process.argv[2]);
const out = {markdown: {}, stripped: {}, replayed: {}, flag: {}};
for (const [k, text] of Object.entries(cases.markdown)) {
  const node = new Elem('div');
  f.renderMarkdown(node, text);
  out.markdown[k] = {text: node.textContent, tags: tags(node).slice(1)};
  out.stripped[k] = f.withoutMarkers(text);
}
for (const [k, text] of Object.entries(cases.replayed)) {
  const node = new Elem('div');
  node.textContent = text;
  const before = f.count();
  f.renderReplayed(node);
  out.replayed[k] = {laid_out: f.count() > before,
                     tags: tags(node).slice(1)};
}
for (const lang of cases.flag) {
  const bubble = new Elem('div');
  f.addFlag(bubble, 'run-1', lang === 'none' ? undefined : lang);
  out.flag[lang] = bubble.after[0].children[0].textContent;
}
process.stdout.write(JSON.stringify(out));
"""

HOSTILE = "### <img src=x onerror=alert(1)><script>alert(2)</script>"

_CASES = {
    "markdown": {
        "h3": "### What the Guidelines Say\nScreening starts at 45.",
        "h1": "# Your labs\nA1c 6.1%",
        "h2_bold": "## **Next** steps\n- Ask about it",
        "hostile": HOSTILE,
        "hostile_bold": "## **<img src=x onerror=alert(3)>**",
        "not_heading": "#1 thing to ask: your A1c",
        "no_space": "###nospace",
    },
    "replayed": {
        "h3": "### What the Guidelines Say\nScreening starts at 45.",
        "plain": "Hello there.",
        "hostile": HOSTILE,
    },
    "flag": ["en", "es", "none", "fr"],
}


def _run() -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        ["node", "-e", _HARNESS, "--", str(CHAT_JS), json.dumps(_CASES)],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def got():
    return _run()


def test_a_heading_line_is_laid_out_as_a_heading(got):
    h3 = got["markdown"]["h3"]
    assert h3["tags"][0] == "H3"
    assert "###" not in h3["text"]
    # Its own line in the text content, not run into the next (#878).
    assert h3["text"] == "What the Guidelines Say\nScreening starts at 45."


@pytest.mark.parametrize("case", ["h1", "h2_bold"])
def test_every_heading_level_is_one_small_heading(got, case):
    """A chat bubble is no place for a page-size heading: # and ## lay out
    as the same <h3>, and a heading keeps its bold."""
    md = got["markdown"][case]
    assert md["tags"][0] == "H3"
    assert "#" not in md["text"]
    if case == "h2_bold":
        assert "STRONG" in md["tags"]
        assert "**" not in md["text"]


@pytest.mark.parametrize("case", ["hostile", "hostile_bold"])
def test_a_hostile_heading_is_text_never_html(got, case):
    """MUTATION: build the heading with innerHTML -> an IMG or SCRIPT."""
    md = got["markdown"][case]
    assert set(md["tags"]) <= {"H3", "STRONG", "#text"}
    assert "<img src=x onerror=alert(" in md["text"]
    if case == "hostile":
        assert "<script>alert(2)</script>" in md["text"]


@pytest.mark.parametrize("case", ["not_heading", "no_space"])
def test_a_hash_that_is_not_a_heading_is_left_alone(got, case):
    md = got["markdown"][case]
    assert "H3" not in md["tags"]
    assert md["text"] == _CASES["markdown"][case]


def test_the_typewriter_never_shows_the_hashes(got):
    assert got["stripped"]["h3"].startswith("What the Guidelines Say\n")
    assert got["stripped"]["not_heading"] == "#1 thing to ask: your A1c"


def test_an_older_answer_with_a_heading_is_laid_out(got):
    """The live case: a replayed answer whose only markdown is a heading
    was left as written, so "###" showed."""
    assert got["replayed"]["h3"]["laid_out"]
    assert got["replayed"]["h3"]["tags"][0] == "H3"
    assert not got["replayed"]["plain"]["laid_out"]
    assert set(got["replayed"]["hostile"]["tags"]) <= {"H3", "#text"}


def test_this_wasnt_right_follows_the_turns_language(got):
    assert got["flag"]["en"] == "This wasn't right"
    assert got["flag"]["none"] == "This wasn't right"
    assert got["flag"]["fr"] == "This wasn't right"
    assert got["flag"]["es"] == "Algo no está bien en esta respuesta"
