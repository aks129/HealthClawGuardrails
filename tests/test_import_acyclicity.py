"""The auth stack imports in one direction.

`r6.routes` sat in a strongly-connected component with `rate_limit`,
`read_auth`, `oauth` and `health_compliance` — four nested cycles sharing two
back-edges, both of which reached back into the 3,900-line module for a
utility that had nowhere else to live:

    oauth.py             -> routes._read_auth_enabled
    health_compliance.py -> routes.json_body_within_depth

Both were written as function-local imports to keep the process from
deadlocking at load time, each with a comment apologising for it. That worked,
and it hid the coupling from every tool that reads imports — including the
reader deciding whether `routes.py` can be split.

The cycles were never a runtime bug. They were a structural one: nothing
could move out of `r6/routes.py` while two other modules reached into it, so
the split kept not happening.

This test walks the real import graph with `ast` and fails on any cycle
inside the auth stack, counting deferred (function-local) imports the same as
module-level ones. A cycle you cannot see is the kind that comes back.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The modules that formed the SCC, plus the two the fix introduces or
#: extends. Scoped rather than repo-wide on purpose: `r6.actions.rails` has a
#: second, benign star-shaped SCC (a package importing its own submodules for
#: their registration side effect), and the plan retires that one by promoting
#: its shared transport helper, not by breaking it here.
_AUTH_STACK = frozenset({
    "r6.routes", "r6.rate_limit", "r6.read_auth", "r6.oauth",
    "r6.health_compliance", "r6.runtime_config", "r6.body_guard",
    "r6.stepup",
})


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(REPO_ROOT)
    parts = list(rel.parts)
    parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _edges() -> dict[str, set[str]]:
    """Every in-stack import edge, deferred ones included."""
    out: dict[str, set[str]] = {name: set() for name in _AUTH_STACK}
    for name in _AUTH_STACK:
        path = REPO_ROOT / (name.replace(".", "/") + ".py")
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                targets = [module] + [f"{module}.{a.name}" for a in node.names]
            for target in targets:
                for candidate in _AUTH_STACK:
                    if target == candidate or target.startswith(candidate + "."):
                        if candidate != name:
                            out[name].add(candidate)
    return out


def _cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Every simple cycle, as paths, via depth-first search."""
    found: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def walk(node: str, path: list[str]) -> None:
        for nxt in sorted(edges.get(node, ())):
            if nxt in path:
                cycle = path[path.index(nxt):] + [nxt]
                key = tuple(sorted(set(cycle)))
                if key not in seen:
                    seen.add(key)
                    found.append(cycle)
                continue
            walk(nxt, path + [nxt])

    for start in sorted(edges):
        walk(start, [start])
    return found


def test_the_auth_stack_has_no_import_cycles():
    """MUTATION: restore `from r6.routes import _read_auth_enabled` in
    r6/oauth.py, or the lazy json_body_within_depth import in
    r6/health_compliance.py -> red, naming the cycle. Ran both, saw red.
    """
    edges = _edges()
    # A graph with no edges would pass forever.
    assert sum(len(v) for v in edges.values()) > 5, (
        f"the import scan found almost no edges ({edges}) — it is broken, and "
        "a green result here means nothing")

    cycles = _cycles(edges)
    assert not cycles, "import cycles in the auth stack:\n" + "\n".join(
        " -> ".join(c) for c in cycles)


def test_nothing_in_the_auth_stack_imports_the_god_module():
    """`r6.routes` may depend on the stack; the stack may not depend on it.

    That direction is what lets routes.py be split. The moment a utility
    inside it acquires an importer, the split acquires a blocker — which is
    exactly how the two back-edges arrived.
    """
    edges = _edges()
    importers = sorted(name for name, targets in edges.items()
                       if "r6.routes" in targets)
    assert importers == [], (
        f"{importers} import from r6/routes.py; move the symbol out instead")
