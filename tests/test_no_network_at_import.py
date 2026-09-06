"""No test module performs network I/O at import time (#635).

tests/test_public_fhir_servers.py used to probe two public FHIR servers at
module load, so every `pytest` invocation in this repository made two live
HTTP calls, whether or not those tests were selected: measured, a run with
every test in that file deselected still spent two seconds on the network,
and twenty with both hosts unreachable. A probe at import is also a
point-in-time answer that the test then trusts minutes later, which is how
a reachable-at-collection server produced a red suite at 412 seconds.

This scans every test module's top-level statements (module scope only:
functions, classes and fixtures are where calls belong) for a call to one
of the HTTP/socket entry points, directly or through a helper defined in
the same module that calls one.

MUTATION: in tests/test_public_fhir_servers.py, put back
`_hapi_available = _server_reachable(HAPI_FHIR_R4)` at module scope -> red,
naming the file, the line and the helper that reaches the network.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'tests'

_NETWORK_CALLS = {
    'httpx.get', 'httpx.post', 'httpx.request', 'httpx.Client',
    'requests.get', 'requests.post', 'requests.request', 'requests.Session',
    'urllib.request.urlopen', 'socket.create_connection', 'socket.socket',
}


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return '.'.join(reversed(parts))
    return None


def _calls_in(node: ast.AST) -> set[str]:
    return {name for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and (name := _dotted(sub.func))}


def _network_reaching_helpers(tree: ast.Module) -> set[str]:
    """Module-level functions whose body calls a network entry point."""
    return {fn.name for fn in tree.body
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _calls_in(fn) & _NETWORK_CALLS}


def _module_scope_network_calls(path: Path, root: Path = ROOT) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    helpers = _network_reaching_helpers(tree)
    reaching = _NETWORK_CALLS | helpers
    findings = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for name in sorted(_calls_in(stmt) & reaching):
            findings.append(f'{path.relative_to(root)}:{stmt.lineno} calls {name}')
    return findings


def test_no_test_module_reaches_the_network_at_import():
    findings = []
    scanned = 0
    for path in sorted(TESTS.glob('test_*.py')):
        scanned += 1
        findings.extend(_module_scope_network_calls(path))
    assert scanned >= 50, f'scan broken: only {scanned} test modules found'
    assert not findings, (
        'network I/O at module scope; every pytest run pays for it whether or '
        'not these tests are selected (#635):\n  ' + '\n  '.join(findings))


def test_the_scan_sees_a_helper_that_reaches_the_network(tmp_path):
    """Self-check: a module-level call to a helper that calls httpx is found,
    and a call inside a test function is not (that is where calls belong)."""
    sample = tmp_path / 'test_sample.py'
    sample.write_text(
        'import httpx\n'
        'def _probe(url):\n'
        '    return httpx.get(url).status_code == 200\n'
        'AVAILABLE = _probe("https://example.invalid")\n'
        'def test_x():\n'
        '    assert httpx.get("https://example.invalid")\n')
    assert _module_scope_network_calls(sample, root=tmp_path) == [
        'test_sample.py:4 calls _probe']
