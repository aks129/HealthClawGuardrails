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

THE RULE, in one sentence: the error half of `validate_step_up_token`'s tuple
may be passed to a logger or to `r6.access.public_step_up_reason`, and to
nothing else — however the caller gets hold of it.

## The word "anywhere" was not true (#630 F6)

This docstring used to end `MUTATION: interpolate the raw reason into any
response anywhere -> red`, and that line had never been executed. The scanner
recognised exactly ONE shape — a two-element TUPLE ASSIGNMENT whose value is a
direct `validate_step_up_token(...)` call — so the same leak written as

    res = validate_step_up_token(token, tenant_id)
    if res[0]:
        return None
    return jsonify({"error": f"step-up token rejected: {res[1]}"}), 401

walked straight past it. Applied to `_authz_write` in
`r6/command_center/routes.py` that mutation put a *token tenant mismatch* back
on the wire and the whole suite stayed green: 3157 passed, byte-identical to
baseline (2026-09-04). #508, undetected, in the file this guard was written
for.

The shape is not hypothetical bad style. `res = validate_step_up_token(...)`
is one keystroke from `if res:` — the truthiness test on the tuple that
CLAUDE.md names as a silent auth bypass, since a 2-tuple is always truthy.
A guard that cannot see the binding cannot see either failure.

THE SHAPES RECOGNISED, so a reader knows the boundary rather than trusting
the word "anywhere":

  1. `valid, err = validate_step_up_token(...)` -> reads of `err`
     (`_reason_names` + `_guarded_uses`)
  2. `res = validate_step_up_token(...)`        -> reads of `res[1]`, and
     bare reads of `res` (which carry both halves, and cover `if res:`).
     `res[0]` is the boolean and is always allowed.
     (`_tuple_names` + `_guarded_tuple_uses`)
  3. `validate_step_up_token(...)[1]` with no name at all
     (`_direct_reason_subscripts`)

NOT recognised, and deliberately left rather than guessed at: a reason that
travels through another function's return value, a dict, or `*rest`
unpacking. `test_a_token_for_another_tenant_is_refused_without_naming_why`
below is the backstop that does not care about syntax at all — it drives the
two #508 sites over the wire.

MUTATION (run 2026-09-04, both directions, see PR): rewrite either
command_center site to bind the tuple and interpolate `res[1]` -> red. Run
on BOTH — `_authz_write` and `api_generate_link` — rather than one and an
assumption about the other, since "either" is the kind of word this file now
exists to stop anyone from writing untested.
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


def _tuple_names(func):
    """Names bound to the WHOLE tuple: `res = validate_step_up_token(...)`.

    Shape 2 of the three in the module docstring. `_reason_names` above is
    blind to it, which is what #630 F6 found.
    """
    names = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        called = (getattr(value.func, 'id', None)
                  or getattr(value.func, 'attr', None))
        if called == 'validate_step_up_token':
            names.add(target.id)
    return names


def _sink_positions(func, predicate):
    """(lineno, col_offset) of every node inside an allowed sink call.

    Position rather than identity because `ast.walk` visits a node once per
    walk and the two passes below need to agree on which reads were already
    accounted for.
    """
    positions = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        called = (getattr(node.func, 'id', None)
                  or getattr(node.func, 'attr', None))
        if called not in _ALLOWED_SINKS:
            continue
        for inner in ast.walk(node):
            if predicate(inner):
                positions.add((inner.lineno, inner.col_offset))
    return positions


def _guarded_tuple_uses(func, name):
    """Line numbers where the tuple bound to `name` can carry the reason out.

    `name[0]` is the boolean half — always fine. `name[1]` is the raw reason
    and is fine only inside an allowed sink. A bare read of `name` is
    reported because it carries both halves, which also makes this the check
    that catches `if name:` — the truthiness test on the tuple that CLAUDE.md
    calls a silent auth bypass.
    """
    allowed_lines = _sink_positions(
        func, lambda n: isinstance(n, ast.Name) and n.id == name)

    for node in ast.walk(func):
        if not isinstance(node, ast.Subscript):
            continue
        inner = node.value
        if (isinstance(inner, ast.Name) and inner.id == name
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == 0):
            allowed_lines.add((inner.lineno, inner.col_offset))

    return [node.lineno for node in ast.walk(func)
            if isinstance(node, ast.Name) and node.id == name
            and isinstance(node.ctx, ast.Load)
            and (node.lineno, node.col_offset) not in allowed_lines]


def _direct_reason_subscripts(func):
    """Line numbers of `validate_step_up_token(...)[1]` — shape 3, no name."""
    allowed = _sink_positions(func, lambda n: isinstance(n, ast.Subscript))

    bad = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Subscript):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        called = (getattr(call.func, 'id', None)
                  or getattr(call.func, 'attr', None))
        if called != 'validate_step_up_token':
            continue
        if not (isinstance(node.slice, ast.Constant) and node.slice.value == 1):
            continue
        if (node.lineno, node.col_offset) in allowed:
            continue
        bad.append(node.lineno)
    return bad


def _guarded_uses(func, name):
    """Line numbers where `name` is read somewhere other than an allowed sink.

    A read is allowed when its nearest enclosing Call is a logger method or
    the classifier. Everything else — an f-string in a response, a dict value,
    a `.format` argument, a bare return — is a use that can reach a caller.
    """
    allowed_lines = _sink_positions(
        func, lambda n: isinstance(n, ast.Name) and n.id == name)

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
            where = path.relative_to(REPO_ROOT)
            for name in _reason_names(func):
                for lineno in _guarded_uses(func, name):
                    yield (f'{where}:{lineno}',
                           f'{func.name}() reads `{name}` outside a logger '
                           'or public_step_up_reason')
            for name in _tuple_names(func):
                for lineno in _guarded_tuple_uses(func, name):
                    yield (f'{where}:{lineno}',
                           f'{func.name}() reads the validator tuple `{name}` '
                           'outside a logger or public_step_up_reason '
                           f'(`{name}[0]` is the only free read)')
            for lineno in _direct_reason_subscripts(func):
                yield (f'{where}:{lineno}',
                       f'{func.name}() subscripts '
                       'validate_step_up_token(...)[1] outside a logger or '
                       'public_step_up_reason')
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


def _only_function(source):
    return next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.FunctionDef))


def test_the_leak_detector_sees_the_tuple_reached_by_index_too():
    """#630 F6: the shape the scanner could not see, proven visible.

    Each case states what the OLD detector returned as well, because "the new
    helper finds it" is only half the claim being repaired — the other half is
    that the old one did not, which is why the census row exists.
    """
    indexed = _only_function(
        'def handler():\n'
        '    res = validate_step_up_token(t, tid)\n'
        '    if res[0]:\n'
        '        return None\n'
        '    return jsonify({"error": f"rejected: {res[1]}"}), 401\n')
    assert _reason_names(indexed) == set(), (
        'the tuple-unpack detector was never blind to this; if it sees it '
        'now, the #630 F6 premise changed and this repair needs re-deriving')
    assert _tuple_names(indexed) == {'res'}
    assert _guarded_tuple_uses(indexed, 'res') == [5]

    classified = _only_function(
        'def handler():\n'
        '    res = validate_step_up_token(t, tid)\n'
        '    if res[0]:\n'
        '        return None\n'
        '    logger.info("refused: %s", res[1])\n'
        '    return jsonify({"error": public_step_up_reason(res[1])}), 401\n')
    assert _guarded_tuple_uses(classified, 'res') == [], (
        'the classified spelling of the same shape must stay green, or the '
        'guard forces callers away from the one safe way to write it')

    truthy = _only_function(
        'def handler():\n'
        '    res = validate_step_up_token(t, tid)\n'
        '    if res:\n'
        '        return None\n')
    assert _guarded_tuple_uses(truthy, 'res') == [3], (
        'a bare read of the tuple is the CLAUDE.md non-negotiable — a 2-tuple '
        'is always truthy, so `if res:` authorizes every caller')

    direct = _only_function(
        'def handler():\n'
        '    return jsonify(\n'
        '        {"error": validate_step_up_token(t, tid)[1]}), 401\n')
    assert _direct_reason_subscripts(direct) == [3]

    sunk = _only_function(
        'def handler():\n'
        '    logger.info("refused: %s", validate_step_up_token(t, tid)[1])\n')
    assert _direct_reason_subscripts(sunk) == []


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


# --- the backstop that does not read source at all ------------------------

#: Neither row may use the `tenant_id` fixture: `test-tenant` is in
#: PUBLIC_TENANTS under the test config, and `/api/generate-link` skips the
#: step-up branch entirely for a public tenant (200, a minted link). That is
#: the documented demo carve-out, not a defect — but a probe pointed at a
#: public tenant would have measured nothing.
_PRIVATE_TENANT = 'probe-private-tenant'


@pytest.mark.parametrize('path, payload', [
    ('/command-center/api/conversations',
     {'role': 'user', 'text': 'hello'}),
    ('/command-center/api/generate-link', {}),
])
def test_a_token_for_another_tenant_is_refused_without_naming_why(
        client, path, payload):
    """The two #508 sites, driven over the wire.

    Every other row in this file reads Python source, so all of them share one
    failure mode: a leak spelled in a syntax the scanner does not model stays
    invisible no matter how many shapes get added. This row cares only about
    the bytes on the wire. It is the reason the scanner may honestly say which
    three shapes it recognises instead of claiming "anywhere".

    A token that is real, unexpired and correctly signed — but minted for a
    DIFFERENT tenant — must come back as the generic refusal. Telling this
    caller the token is merely issued elsewhere is the one carve-out in the
    owner's 2026-08-10 ruling, and it is what #508 put back on the wire.

    MUTATION (run 2026-09-04): bind the validator's tuple and interpolate
    `res[1]` -> red. Run separately against each site: mutating
    `_authz_write` reddens the conversations row, mutating
    `api_generate_link` reddens the generate-link row, each with
    'Token tenant mismatch' in the body.
    """
    from r6.command_center import access
    from r6.stepup import generate_step_up_token
    assert not access.is_public(_PRIVATE_TENANT), (
        '%s is in PUBLIC_TENANTS, so generate-link never reaches the step-up '
        'branch and this row measures nothing' % _PRIVATE_TENANT)
    other = generate_step_up_token('a-different-tenant-entirely')

    response = client.post(path,
                           json={'tenant_id': _PRIVATE_TENANT, **payload},
                           headers={'X-Step-Up-Token': other})

    assert response.status_code == 401, (
        'the refusal this row measures did not happen, so the assertions '
        'below prove nothing: %s %s'
        % (response.status_code, response.get_data(as_text=True)[:200]))
    body = response.get_data(as_text=True)
    assert 'mismatch' not in body.lower(), (
        'the raw validator reason reached the caller: ' + body[:200])
    assert _DENIED_REJECTED in body, (
        'the refusal should carry the classified sentence; a different '
        'refusal means this row stopped measuring the step-up gate: '
        + body[:200])
