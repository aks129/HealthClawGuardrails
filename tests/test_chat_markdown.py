"""The chat renders a small, safe subset of the model's markdown.

Found filming the demo: `addAgentText` set `textContent`, so a reply the
model wrote as `**Metformin** 500 mg` showed the asterisks literally. The fix
renders bold, italics, lists and line breaks, and it builds them from text
nodes and fixed elements only. Nothing the model writes is ever parsed as
HTML, so a `<script>` in a reply stays text. (No markup sink may appear in
chat.js at all: tests/test_careagents_census_gaps.py guards that.)

These run chat.js's real renderer under node with a stub DOM. The renderer is
cut out of the file by its markers rather than copied here, so the test
cannot drift from what ships.
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
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const text = process.argv[2];

// A stub DOM that records structure. `textContent = x` replaces the
// children with one text node, as the real DOM does.
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
globalThis.document = { createElement: element, createTextNode: textNode };

function cut(from, to) {
  const a = src.indexOf(from), b = src.indexOf(to, a);
  if (a < 0 || b < 0) throw new Error('marker not found: ' + from);
  return src.slice(a, b);
}
// The page's own entry point: addAgentText, its typewriter and renderer.
// `log`, `scroll` and a synchronous animation frame are all it needs.
const log = element('div');
globalThis.requestAnimationFrame = (fn) => fn();
const code = 'const log = arguments[0]; function scroll() {}\n'
  + cut('function el(', 'function addUser(')
  + cut('function typewrite(', 'function addChip(')
  + 'return { addAgentText, withoutMarkers };';
const { addAgentText, withoutMarkers } = new Function(code)(log);

function tree(n) {
  if (n.nodeType === 3) return { text: n.text };
  return { tag: n.tagName.toLowerCase(), children: n.children.map(tree) };
}
addAgentText(text);
const root = log.children[0];
process.stdout.write(JSON.stringify({
  tree: tree(root), typed: withoutMarkers(text),
}));
"""


def _render(text: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", _HARNESS, "--", str(CHAT_JS), text],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _tags(node) -> list[str]:
    found = [node["tag"]] if "tag" in node else []
    for child in node.get("children", []):
        found += _tags(child)
    return found


def _texts(node) -> list[str]:
    if "text" in node:
        return [node["text"]]
    return [t for c in node["children"] for t in _texts(c)]


def test_bold_renders_as_strong_without_the_asterisks():
    """MUTATION: drop the renderMarkdown call in addAgentText -> red."""
    result = _render("Take **Metformin** twice daily.")
    assert "strong" in _tags(result["tree"])
    assert "**" not in "".join(_texts(result["tree"]))
    assert result["typed"] == "Take Metformin twice daily."


def test_a_script_tag_from_the_model_stays_text():
    """The model's text never becomes markup, whatever it contains."""
    payload = '<script>alert(1)</script> **bold** <img src=x onerror=alert(1)>'
    result = _render(payload)
    tags = _tags(result["tree"])
    assert "script" not in tags and "img" not in tags
    assert set(tags) <= {"div", "strong"}
    joined = "".join(_texts(result["tree"]))
    assert "<script>alert(1)</script>" in joined
    assert "<img src=x onerror=alert(1)>" in joined


def test_italics_lists_and_line_breaks():
    result = _render("Your *recent* labs are in.\nHere:\n- LDL **130**\n"
                     "- HDL 55\n\n"
                     "1. First\n2. Second\nThat is all.")
    tags = _tags(result["tree"])
    assert "em" in tags and "strong" in tags
    assert tags.count("ul") == 1 and tags.count("ol") == 1
    assert tags.count("li") == 4
    assert "br" in tags
    assert "- " not in "".join(_texts(result["tree"]))


def test_a_lone_asterisk_and_snake_case_are_left_alone():
    result = _render("5 * 3 = 15 and a_b_c stays")
    assert _tags(result["tree"]) == ["div"]
    assert "".join(_texts(result["tree"])) == "5 * 3 = 15 and a_b_c stays"
