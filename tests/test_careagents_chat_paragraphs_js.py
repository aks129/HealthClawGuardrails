"""An assistant answer's paragraphs stay apart in its text content (#878).

`renderMarkdown` (careagents/static/chat.js) separated lines with `<br>`
elements, which add nothing to `textContent`. A screen reader reading the
message as text, and anything that copies it, got "...promptly.Your A1c..."
run together. The patient tester flagged it on the creatinine sentence.

Run under node against the real functions cut from chat.js, with a minimal
DOM: element and text nodes, `appendChild`, and `textContent`.
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
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; }
  appendChild(c) { this.children.push(c); return c; }
  get textContent() { return this.children.map(c => c.textContent).join(''); }
  set textContent(t) { this.children = t ? [new Text(t)] : []; }
  set className(_) {}
  set href(_) {}
}
const document = {
  createElement: t => new Elem(t),
  createTextNode: t => new Text(t),
};
const code = cutConst('BOLD_OR_ITALIC') + cutConst('LIST_ITEM')
  + cutConst('LINKS') + cutFn('el') + cutFn('appendLinked')
  + cutFn('appendInline') + cutFn('renderMarkdown')
  + 'return renderMarkdown;';
const renderMarkdown = new Function('document', code)(document);
const out = {};
for (const [k, text] of Object.entries(JSON.parse(process.argv[2]))) {
  const node = new Elem('div');
  renderMarkdown(node, text);
  out[k] = {text: node.textContent,
            tags: node.children.map(c => c.tagName || '#text')};
}
process.stdout.write(JSON.stringify(out));
"""

_CASES = {
    "paragraphs": ("Between Sep 1 and Sep 7, your creatinine rose. Contact "
                   "your clinician promptly.\n\nYour A1c is above the "
                   "typical range."),
    "lines": "First line.\nSecond line.",
    "list": "Your results:\n- **A1c** high\n- LDL normal\n\nAsk about both.",
}


def _run() -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        ["node", "-e", _HARNESS, "--", str(CHAT_JS), json.dumps(_CASES)],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_paragraphs_stay_apart_in_the_text_content():
    got = _run()["paragraphs"]["text"]
    assert "promptly.Your" not in got
    assert "promptly.\n\nYour A1c" in got


def test_lines_stay_apart_in_the_text_content():
    assert _run()["lines"]["text"] == "First line.\nSecond line."


def test_lists_and_emphasis_still_render_as_elements():
    got = _run()["list"]
    assert "UL" in got["tags"]
    assert "Ask about both." in got["text"]
    assert "**" not in got["text"]
