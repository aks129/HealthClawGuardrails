"""The payload seal is closed as a class, not per call site (#620).

Two gaps the adversarial review of #550 left open after the #528 repro
itself was closed:

1. The seal is an ORM validator. A bulk `Query.update()` or a Core
   `update(ProposedAction)` compiles straight to SQL and never fires it.
   No live call path carries payload_json that way today, and nothing went
   red if one did. The AST ratchet below scans every bulk writer on
   ProposedAction: a literal mapping may not name payload_json, and a
   non-literal mapping is allowed only at a site that refuses payload_json
   at runtime and is listed here for it (transition_action, #528).
2. `PayloadSealed` had no registered handler, so any raise site other than
   the one review route that catches it answered an unhandled 500. It is
   now an app-wide 409 with a fixed, allowlist-constructed message that
   never reflects the exception text.

MUTATIONS: add `'payload_json': ...` to the literal mapping of any bulk
update in r6/actions/routes.py -> the ratchet goes red naming file, line
and function. Delete the register_error_handler line in
r6/actions/errors.py -> the handler test goes red (the exception escapes).
"""

import ast
import pathlib

from r6.actions.errors import SEALED_MESSAGE
from r6.actions.models import PayloadSealed

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Bulk writers allowed to pass a NON-literal mapping to update(): each one
# refuses payload_json at runtime and pins that refusal in its own tests.
RUNTIME_GUARDED = {'r6/actions/state.py:transition_action'}


def _names(node):
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _enclosing(tree):
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(fn):
                owner.setdefault(id(sub), fn.name)
    return owner


def _mapping_keys(call):
    """Keys a bulk-update call writes, or None when the mapping is not a
    literal and cannot be read from the source."""
    keys = set()
    literal = False
    if call.func.attr == 'update' and call.args:
        arg = call.args[0]
        if isinstance(arg, ast.Dict):
            literal = True
            keys |= {k.value for k in arg.keys if isinstance(k, ast.Constant)}
        else:
            return None
    for kw in call.keywords:
        if kw.arg is None:
            return None            # **mapping
        if kw.arg == 'synchronize_session':
            continue
        if kw.arg == 'values' and isinstance(kw.value, ast.Dict):
            literal = True
            keys |= {k.value for k in kw.value.keys if isinstance(k, ast.Constant)}
        elif kw.arg == 'values':
            return None
        else:
            literal = True
            keys.add(kw.arg)       # .values(col=...) / .update(col=...)
    return keys if literal else None


def _bulk_updates():
    for path in sorted((ROOT / 'r6').rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        owner = _enclosing(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr in ('update', 'values')):
                continue
            if 'ProposedAction' not in _names(f.value):
                continue
            where = '%s:%s' % (path.relative_to(ROOT).as_posix(),
                               owner.get(id(node), '<module>'))
            yield where, node.lineno, node


def test_no_bulk_update_on_proposed_action_can_carry_payload_json():
    seen, violations = 0, []
    for where, line, call in _bulk_updates():
        seen += 1
        keys = _mapping_keys(call)
        if keys is None:
            if where not in RUNTIME_GUARDED:
                violations.append('%s (line %d): non-literal mapping at a site '
                                  'not listed as runtime-guarded' % (where, line))
        elif 'payload_json' in keys:
            violations.append('%s (line %d): bulk update names payload_json'
                              % (where, line))
    assert seen >= 3, 'the scan found %d bulk writers; it used to find 3' % seen
    assert not violations, (
        'A bulk writer can reach payload_json past the ORM seal (#620):\n  '
        + '\n  '.join(violations))


def test_the_runtime_guarded_sites_still_exist():
    found = {where for where, _, _ in _bulk_updates()}
    missing = RUNTIME_GUARDED - found
    assert not missing, 'listed as runtime-guarded but no bulk update there: %s' % sorted(missing)


def test_payload_sealed_is_a_clean_409_anywhere(app):
    exc = PayloadSealed('payload_json is sealed: a confirmation exists for '
                        'action abc-123-secret')
    with app.test_request_context('/anywhere'):
        rendered = app.make_response(app.handle_user_exception(exc))
    assert rendered.status_code == 409
    assert rendered.get_json() == {'error': SEALED_MESSAGE}
    assert 'abc-123-secret' not in rendered.get_data(as_text=True)
