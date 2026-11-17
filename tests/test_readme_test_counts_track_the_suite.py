"""Drift guard: the README's test counts must not outrun the suite.

The README once claimed "1,490+" and "4,000+" Python tests in three places
long after both were wrong. The count now lives in one place, the tests
badge, and these tests make it a floor the suite must actually reach.

A floor is compared with the number of test functions, not test cases:
parametrized cases are not countable without running the suite.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()

_BADGE = re.compile(
    r"badge/tests-([\d%C]+)%2B%20Python%20%2B%20(\d+)%20Node-")
_PY_TEST = re.compile(r"^\s*(?:async\s+)?def test_", re.M)
_NODE_TEST = re.compile(r"^\s*(?:it|test)(?:\.each\([^)]*\))?\(", re.M)


def _badge_floors():
    found = _BADGE.findall(README)
    assert len(found) == 1, "expected exactly one tests badge in README.md"
    python, node = found[0]
    return int(python.replace("%2C", "")), int(node)


def _python_test_functions():
    return sum(len(_PY_TEST.findall(p.read_text()))
               for p in (ROOT / "tests").rglob("test_*.py"))


def _node_tests():
    base = ROOT / "services" / "agent-orchestrator"
    files = [p for pat in ("*.test.*", "*.spec.*") for p in base.rglob(pat)
             if "node_modules" not in p.parts]
    return sum(len(_NODE_TEST.findall(p.read_text())) for p in files)


def test_the_python_floor_is_reached():
    floor, _ = _badge_floors()
    assert floor <= _python_test_functions(), (
        "README tests badge claims more Python tests than tests/ defines")


def test_the_node_floor_is_reached():
    _, floor = _badge_floors()
    assert floor <= _node_tests(), (
        "README tests badge claims more Node tests than the MCP server defines")


def test_the_python_count_is_stated_only_in_the_badge():
    prose = _BADGE.sub("", README)
    stray = re.findall(r"[\d,]{3,}\+?\s+(?:Python\s+)?tests\b", prose)
    assert not stray, (
        "a test count outside the badge will go stale: %r" % stray)
