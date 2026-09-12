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

# The columns no bulk writer in this codebase may name: the executable
# payload (#528) and the digest the human gate verifies it against (#658;
# the QA pass on that PR forged both in one transaction).
SEALED_COLUMNS = {'payload_json', 'payload_digest'}


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


RAIL_MODELS = {'ProposedAction', 'ActionConfirmation'}


def _writers_in(source, label):
    """Every `.update(...)` / `.values(...)` call in one module, with what
    the scan can tell about it: where it is, whether its receiver names a
    rail model, and the literal keys it writes (None when unreadable)."""
    tree = ast.parse(source, filename=label)
    owner = _enclosing(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in ('update', 'values')):
            continue
        where = '%s:%s' % (label, owner.get(id(node), '<module>'))
        yield where, node.lineno, bool(RAIL_MODELS & _names(f.value)), _mapping_keys(node)


def _violations(writers):
    """The scan is keyed on the column, not the receiver (#680): a literal
    mapping naming a sealed column is the defect wherever it is called —
    no other model has a column of that name — so binding the query to a
    variable first walks past nothing. A mapping the scan cannot read is
    judged by its receiver, as before: on a rail model it must be a site
    that refuses the sealed columns at runtime and is listed for it."""
    seen, violations = 0, []
    for where, line, on_rail, keys in writers:
        if keys is None:
            if on_rail:
                seen += 1
                if where not in RUNTIME_GUARDED:
                    violations.append('%s (line %d): non-literal mapping at a '
                                      'site not listed as runtime-guarded'
                                      % (where, line))
            continue
        if on_rail:
            seen += 1
        if keys & SEALED_COLUMNS:
            violations.append('%s (line %d): bulk update names %s'
                              % (where, line, ', '.join(sorted(keys & SEALED_COLUMNS))))
    return seen, violations


def _bulk_updates():
    for path in sorted((ROOT / 'r6').rglob('*.py')):
        label = path.relative_to(ROOT).as_posix()
        yield from _writers_in(path.read_text(encoding='utf-8'), label)


def test_no_bulk_update_on_the_action_rail_can_carry_a_sealed_column():
    seen, violations = _violations(_bulk_updates())
    assert seen >= 4, 'the scan found %d bulk writers; it used to find 4' % seen
    assert not violations, (
        'A bulk writer can reach payload_json past the ORM seal (#620):\n  '
        + '\n  '.join(violations))


ALIASED = """
def swap(action_id, payload_json):
    query = ProposedAction.query.filter_by(id=action_id)
    query.update({'payload_json': payload_json}, synchronize_session=False)
"""


def test_binding_the_query_to_a_variable_does_not_walk_past_the_ratchet():
    # #680: the receiver `query` never says ProposedAction; the mapping key
    # does, and the SQL is identical to the form the ratchet always caught.
    _seen, violations = _violations(_writers_in(ALIASED, 'aliased.py'))
    assert violations == [
        "aliased.py:swap (line 4): bulk update names payload_json"]


def test_a_plain_mapping_update_is_not_a_bulk_writer():
    # Two limits, written down: dict.update() with a non-literal argument is
    # everywhere and says nothing about columns, so only its literal keys
    # are read; and the scan walks r6/ only, so a writer under careagents/
    # or scripts/, or raw SQL through text(), is outside it. Neither has a
    # live call path today.
    _seen, violations = _violations(_writers_in(
        "def f(d, other):\n    d.update(other)\n    d.update({'status': 1})\n", 'p.py'))
    assert violations == []


def test_the_runtime_guarded_sites_still_exist():
    found = {where for where, _, on_rail, _ in _bulk_updates() if on_rail}
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
