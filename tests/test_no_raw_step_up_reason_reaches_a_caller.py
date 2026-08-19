"""The validator's raw refusal reason goes to a log or to the classifier.

Nowhere else. #478 found three write gates in r6/routes.py interpolating it
straight into the response:

    valid, err = validate_step_up_token(step_up_token, tenant_id)
    if not valid:
        return _operation_outcome(..., f'Step-up token rejected: {err}'), 401

One of the eleven values `err` can take is 'Token tenant mismatch', which
tells a caller presenting a token they should not have that the token is
VALID and merely issued to a different tenant. That separates "a real
credential I stole or guessed" from "junk" — the distinction a prober is
trying to draw, and the one carve-out in the owner's 2026-08-10 ruling that a
refusal states its reason.

#478 was closed after kernel slice 6 migrated those three gates. Two sites in
r6/command_center/routes.py were never in that diff and still carried it
(#508). A leak that comes back after its issue is closed is a leak with no
guard on it, so this file is the guard rather than another fix.

THE RULE, in one sentence: the name bound to the error half of
`validate_step_up_token`'s tuple may be passed to a logger or to
`r6.access.public_step_up_reason`, and to nothing else.

MUTATION: interpolate the raw reason into any response anywhere -> red.
"""

import ast
import pathlib

import pytest

from r6.access import _DENIED_REJECTED, public_step_up_reason

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_PRODUCTION_DIRS = ('r6', 'careagents', 'adapters', 'api', 'services',
                    'scripts', 'openclaw', 'hermes', 'migrations')
_PRODUCTION_ROOT_FILES = ('main.py', 'app.py', 'models.py')

#: The two destinations a raw reason may reach. A logger keeps it on our side
#: of the wire; the classifier is what decides whether the caller may see it.
_ALLOWED_SINKS = ('public_step_up_reason', 'debug', 'info', 'warning',
                  'error', 'exception', 'critical', 'log')


def _production_python_files():
    for name in _PRODUCTION_ROOT_FILES:
        path = REPO_ROOT / name
        if path.exists():
            yield path
    for folder in _PRODUCTION_DIRS:
        base = REPO_ROOT / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob('*.py')):
            if '__pycache__' not in path.parts:
                yield path


def _reason_names(func):
    """Names bound to the error half of a validate_step_up_token tuple."""
    names = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if not isinstance(target, ast.Tuple) or len(target.elts) != 2:
            continue
        if not isinstance(value, ast.Call):
            continue
        called = (getattr(value.func, 'id', None)
                  or getattr(value.func, 'attr', None))
        if called != 'validate_step_up_token':
            continue
        second = target.elts[1]
        if isinstance(second, ast.Name):
            names.add(second.id)
    return names


def _guarded_uses(func, name):
    """Line numbers where `name` is read somewhere other than an allowed sink.

    A read is allowed when its nearest enclosing Call is a logger method or
    the classifier. Everything else — an f-string in a response, a dict value,
    a `.format` argument, a bare return — is a use that can reach a caller.
    """
    allowed_lines = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        called = (getattr(node.func, 'id', None)
                  or getattr(node.func, 'attr', None))
        if called not in _ALLOWED_SINKS:
            continue
        for arg in ast.walk(node):
            if isinstance(arg, ast.Name) and arg.id == name:
                allowed_lines.add((arg.lineno, arg.col_offset))

    bad = []
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id == name \
                and isinstance(node.ctx, ast.Load) \
                and (node.lineno, node.col_offset) not in allowed_lines:
            bad.append(node.lineno)
    return bad


def _leaks():
    scanned = 0
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        scanned += 1
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for name in _reason_names(func):
                for lineno in _guarded_uses(func, name):
                    yield (f'{path.relative_to(REPO_ROOT)}:{lineno}',
                           f'{func.name}() reads `{name}` outside a logger '
                           'or public_step_up_reason')
    assert scanned > 100, f'the leak scan only walked {scanned} files'


def test_no_production_site_uses_the_raw_validator_reason():
    """MUTATION: drop public_step_up_reason() from either command_center site
    -> red. That is #508 going back in."""
    leaks = list(_leaks())
    assert not leaks, (
        "the validator's raw reason may only reach a logger or the "
        "classifier; one of its eleven values names another tenant's "
        'credential:\n  ' + '\n  '.join(f'{w} — {why}' for w, why in leaks))


def test_the_leak_detector_actually_detects_the_leak():
    """A guard that cannot fail is the defect it was written to catch.

    Proven on synthetic sources before the real scan above is trusted —
    this suite has shipped a check whose subject never ran.
    """
    leaking = ast.parse(
        'def handler():\n'
        '    valid, err = validate_step_up_token(t, tid)\n'
        '    if not valid:\n'
        '        return jsonify({"error": f"rejected: {err}"}), 401\n')
    func = next(n for n in ast.walk(leaking)
                if isinstance(n, ast.FunctionDef))
    assert _reason_names(func) == {'err'}
    assert _guarded_uses(func, 'err') == [4]

    classified = ast.parse(
        'def handler():\n'
        '    valid, err = validate_step_up_token(t, tid)\n'
        '    if not valid:\n'
        '        logger.info("refused: %s", err)\n'
        '        return jsonify({"error": public_step_up_reason(err)}), 401\n')
    func = next(n for n in ast.walk(classified)
                if isinstance(n, ast.FunctionDef))
    assert _guarded_uses(func, 'err') == []


# --- the wire behaviour, both sides of the ruling -------------------------

@pytest.mark.parametrize('reason', [
    'Step-up token expired',
    'Read-scoped token cannot authorize this operation',
    'Token audience mismatch',
    'Token operation mismatch',
])
def test_a_reason_about_the_callers_own_token_still_reaches_them(reason):
    """The other half, and the reason this is a classifier rather than a
    blanket redaction. Collapsing all eleven was the previous behaviour and
    the owner overruled it: a refusal nobody can name cannot be acted on.

    MUTATION: make public_step_up_reason return _DENIED_REJECTED always
    -> red.
    """
    assert public_step_up_reason(reason) == reason


def test_the_one_reason_about_someone_elses_credential_does_not():
    assert public_step_up_reason('Token tenant mismatch') == _DENIED_REJECTED
