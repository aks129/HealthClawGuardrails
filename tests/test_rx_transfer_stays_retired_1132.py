"""The controlled-substance check stays uncalled until #1132 lands.

With the terminology lookup on, two RxNorm codes (541892, a name over the
label limit, and 84815, a brand name) get no label from r6/rxnav.py, and the
label was what made `_is_schedule_ii` in r6/actions/rx_transfer.py refuse
them. #1132 moves that check onto ingredient codes. Until it lands, the gap
is closed only because nothing reaches the check: the pharmacy-transfer
propose route answers 410 and the check has no caller.

These two tests hold that in code. Each reads the route or the source tree,
not a description. When #1132 lands, it removes this file together with the
two strict xfails in tests/test_qa_1126_rxnav.py.

MUTATION, each run on this branch and red:
- return anything but 410 from propose_rx_transfer in r6/actions/routes.py:
  test_the_propose_route_still_answers_410;
- import or call `_is_schedule_ii` anywhere outside tests (by name or as
  `rx_transfer._is_schedule_ii`): test_the_schedule_ii_check_has_no_caller.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECK = "_is_schedule_ii"
HOME = "r6/actions/rx_transfer.py"
SKIPPED = frozenset({"tests", "node_modules", "e2e"})


def _source_files():
    """Every Python file that ships: not tests, not a dot or vendor dir."""
    for path in sorted(ROOT.rglob("*.py")):
        parts = path.relative_to(ROOT).parts
        if any(p.startswith(".") or p in SKIPPED for p in parts[:-1]):
            continue
        yield path


def _references(tree: ast.AST) -> list[int]:
    """Line numbers where the check is named, other than its own `def`."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == CHECK:
            lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == CHECK:
            lines.append(node.lineno)
        elif isinstance(node, ast.alias) and CHECK in (node.name, node.asname):
            lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and node.value == CHECK:
            lines.append(node.lineno)  # getattr(rx_transfer, "<name>")
    return lines


def test_the_propose_route_still_answers_410(client):
    resp = client.post("/r6/actions/rx-transfer/propose", json={})

    if resp.status_code != 410:
        pytest.fail(
            "POST /r6/actions/rx-transfer/propose answered %d, not 410. "
            "This route must stay retired until #1132 lands: with the "
            "terminology lookup on, RxNorm 541892 and 84815 get no label, "
            "and the label is what made the controlled-substance check "
            "refuse them. Land #1132 first." % resp.status_code)


def test_the_schedule_ii_check_has_no_caller():
    defined = False
    found = []
    for path in _source_files():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        if rel == HOME:
            defined = any(isinstance(n, ast.FunctionDef) and n.name == CHECK
                          for n in ast.walk(tree))
        found.extend("%s:%d" % (rel, line) for line in _references(tree))

    if not defined:
        pytest.fail("%s no longer defines %s, so this pin reads nothing. "
                    "If #1132 moved or replaced the check, remove this file "
                    "with it." % (HOME, CHECK))
    if found:
        pytest.fail(
            "%s is referenced at %s. It must have no caller until #1132 "
            "lands: with the terminology lookup on, RxNorm 541892 and 84815 "
            "get no label, so a name-keyed check lets them through. Land "
            "#1132 first." % (CHECK, ", ".join(found)))
