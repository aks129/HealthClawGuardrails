"""Guard: two `function name()` declarations in one browser script.

`careagents/static/home.js` declared `say` twice — `say(anchor, el, text)` at
module scope and `say(msg, text, cls)` 187 lines later. Hoisting means the
second wins *everywhere*, including at the four call sites written for the
first. Every one of those is an error path:

    say(tile, $("connect-msg"), "Couldn't connect that source.")
      -> tile.textContent = "[object HTMLElement]"     the label is destroyed
      -> tile.className   = "conn-refresh-msg Couldn't connect that source."
      -> the message element stays hidden               nothing is shown

So a person taps a connector, it fails, and the tile turns into unstyled
`[object HTMLButtonElement]` with no explanation — the dead end #224 exists to
remove, reintroduced by #224's own fix.

Nothing could have caught it. These files have no linter: `node-tests` covers
`services/agent-orchestrator` only, and there is no ESLint config in the repo.
Happy-path Playwright never renders an error state. ESLint `no-redeclare` is
the better long-term answer; this is the zero-dependency stand-in that runs in
the suite we already have.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SCRIPT_DIRS = [
    REPO_ROOT / "careagents" / "static",
    REPO_ROOT / "static" / "js",
]

# `function name(` — the hoisted form, which is the one that collides
# silently. Arrow functions assigned to const/let are block-scoped and error
# loudly on redeclaration, so they are not the risk here.
DECL = re.compile(r"^\s*function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)

# Strings and comments would otherwise contribute phantom declarations —
# home.js has a comment literally naming `say(anchor, el, text)`.
LINE_COMMENT = re.compile(r"//[^\n]*")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _scripts() -> list[Path]:
    out: list[Path] = []
    for d in SCRIPT_DIRS:
        if d.is_dir():
            out.extend(sorted(p for p in d.glob("*.js")))
    return out


def _strip(src: str) -> str:
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", src))


def test_there_are_browser_scripts_to_check():
    """Guards the guard: a moved directory must not silently pass."""
    assert _scripts(), "no browser scripts found — did static/ move?"


@pytest.mark.parametrize("path", _scripts(), ids=lambda p: p.name)
def test_no_function_is_declared_twice_in_one_script(path: Path):
    names = DECL.findall(_strip(path.read_text(encoding="utf-8")))
    dupes = {n: c for n, c in Counter(names).items() if c > 1}
    rel = path.relative_to(REPO_ROOT)
    assert not dupes, (
        f"{rel} declares the same function more than once: "
        + ", ".join(f"{n}×{c}" for n, c in sorted(dupes.items()))
        + ". Hoisting makes the LAST one win at every call site, including "
          "those written for the first signature. Rename one."
    )
