"""No r6 log line writes a tenant id, agent id or `X-Source` value raw (FHI-60).

A tenant id is not a secret in itself, but the value in that slot is often
caller-supplied, and `secrets.token_urlsafe` values (the engine's own OAuth
tokens) pass the tenant pattern. A token pasted into X-Tenant-Id was written
to the log verbatim. So every logging call in r6/ passes these values through
`r6.log_safety.loggable_id`, which writes a short keyed hash; the owner
recomputes one with `flask --app main log-id-hash <id>`.

This walks the AST of every module under r6/ and fails on any `logger.*` /
`log.*` / `logging.*` call whose arguments, or whose f-string, name a
tenant-ish variable outside a `loggable_id(...)` call. A new site fails CI.

A site that is safe for a stated reason goes in `_EXCEPTIONS`, keyed by
`path:function:expression`, with the reason beside it. Keep it short.

MUTATION: unwrap `loggable_id(tenant.id)` in r6/access.py -> red here, and
tests/test_sec_fhi60_other_loggers_probe.py goes red too.
"""

from __future__ import annotations

import ast
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_LOG_METHODS = frozenset({'debug', 'info', 'warning', 'warn', 'error',
                          'exception', 'critical', 'log'})
_LOGGER_NAMES = frozenset({'logger', 'log', 'logging', '_logger',
                           'request_logger'})

#: A dotted expression naming a tenant id, an agent id or the X-Source value:
#: `tenant_id`, `tenant.id`, `conn.tenant_id`, `selected_tenant`,
#: `agent_id`, `source`.
_ID_NAME = re.compile(r'tenant|agent_id|(?:^|\.)source$')

#: path:function:expression -> why it may be logged as-is.
_EXCEPTIONS: dict[str, str] = {}

_SCAN_FLOOR = 50


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return '.'.join(reversed(parts))
    return None


def _is_log_call(node):
    if not (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_METHODS):
        return False
    owner = _dotted(node.func.value)
    return owner is not None and owner.split('.')[-1] in _LOGGER_NAMES


def _is_loggable_id_call(node):
    return (isinstance(node, ast.Call)
            and _dotted(node.func) is not None
            and _dotted(node.func).split('.')[-1] == 'loggable_id')


def _raw_id_expressions(node):
    """Tenant-ish names reachable from `node` without passing through
    loggable_id(...)."""
    if _is_loggable_id_call(node):
        return
    name = _dotted(node)
    if name is not None and _ID_NAME.search(name):
        yield name
        return
    for child in ast.iter_child_nodes(node):
        # A call's function is not a value that gets logged.
        if isinstance(node, ast.Call) and child is node.func:
            continue
        yield from _raw_id_expressions(child)


def _sites():
    found = set()
    scanned = 0
    for path in sorted((REPO_ROOT / 'r6').rglob('*.py')):
        if '__pycache__' in path.parts:
            continue
        scanned += 1
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(), filename=rel)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not _is_log_call(node):
                    continue
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    for expr in _raw_id_expressions(arg):
                        found.add(f'{rel}:{func.name}:{expr}')
    return found, scanned


def test_no_log_line_in_r6_writes_a_raw_tenant_agent_or_source_id():
    sites, scanned = _sites()
    assert scanned >= _SCAN_FLOOR, (
        f'walked only {scanned} modules; the scan is broken and this test is '
        'reporting a green it did not measure')
    unexplained = sorted(sites - set(_EXCEPTIONS))
    assert not unexplained, (
        'log lines that write a tenant id, agent id or X-Source value raw. '
        'Wrap each in r6.log_safety.loggable_id(...), or add it to '
        '_EXCEPTIONS with the reason:\n  ' + '\n  '.join(unexplained))


def test_every_exception_still_exists():
    """A stale exception is a hole a new site could reuse silently."""
    sites, _ = _sites()
    stale = sorted(set(_EXCEPTIONS) - sites)
    assert not stale, f'exceptions naming no current site: {stale}'


def test_the_scan_sees_a_raw_tenant_in_a_log_call():
    """The detector itself, on the shapes it must catch and must pass."""
    tree = ast.parse(
        'def f(tenant, conn, source):\n'
        '    logger.info("a %s", tenant.id)\n'
        '    log.warning(f"b {conn.tenant_id}")\n'
        '    logger.info("c %s", source)\n'
        '    logger.info("d %s", loggable_id(tenant.id))\n'
        '    logger.info("e %s", tenant_count)\n'
        '    other.info("f %s", tenant.id)\n')
    hits = []
    for node in ast.walk(tree):
        if _is_log_call(node):
            for arg in node.args:
                hits.extend(_raw_id_expressions(arg))
    assert hits == ['tenant.id', 'conn.tenant_id', 'source', 'tenant_count']
