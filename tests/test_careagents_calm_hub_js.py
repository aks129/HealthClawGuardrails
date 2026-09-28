"""The hub's small word and matching rules, run under node (PR #843 QA).

The functions are cut out of home.js by their names rather than copied here,
so the test cannot drift from what ships (the same approach as
tests/test_chat_markdown.py).
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

HOME_JS = (pathlib.Path(__file__).resolve().parents[1]
           / "careagents" / "static" / "home.js")

_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function cut(name) {
  const m = src.match(new RegExp('const ' + name + ' = [\\s\\S]*?;\\n'));
  if (!m) throw new Error('not found: ' + name);
  return m[0];
}
const code = cut('requests') + cut('orphanLine') + cut('deleteTyped')
  + 'return { orphanLine, deleteTyped };';
const { orphanLine, deleteTyped } = new Function(code)();
const typed = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify({
  one: orphanLine({ name: 'Juniper', count: 1 }),
  two: orphanLine({ name: 'Coach', count: 2 }),
  typed: typed.map(deleteTyped),
}));
"""

_TYPED = ["DELETE", "Delete", "delete", "  delete  ", "Delete\n",
          "DELET", "deleted", "", "   ", "DE LETE"]


def _run() -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        ["node", "-e", _HARNESS, "--", str(HOME_JS), json.dumps(_TYPED)],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_orphan_line_is_singular_for_one_request():
    got = _run()
    assert got["one"] == ("1 request waiting on Juniper. "
                          "Start a chat to review it.")
    assert got["two"] == ("2 requests waiting on Coach. "
                          "Start a chat to review them.")


def test_delete_is_accepted_in_any_case_with_the_whitespace_trimmed():
    """Phones capitalise the first letter: "Delete" must count."""
    got = dict(zip(_TYPED, _run()["typed"]))
    assert got == {"DELETE": True, "Delete": True, "delete": True,
                   "  delete  ": True, "Delete\n": True,
                   "DELET": False, "deleted": False, "": False,
                   "   ": False, "DE LETE": False}


def test_both_delete_gates_use_the_same_rule():
    """The button is enabled by the rule and the click checks it again, so a
    markup change that drops `disabled` still cannot make it one tap."""
    js = HOME_JS.read_text()
    assert len(re.findall(r"deleteTyped\(input\.value\)", js)) == 2
    assert 'input.value !== "DELETE"' not in js
    assert 'input.value === "DELETE"' not in js
