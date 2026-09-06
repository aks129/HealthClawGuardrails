"""Canonical state transition for actions. The ONLY sanctioned way to change
ProposedAction.status. Combines the guarded single-UPDATE claim pattern (from
routes.py) with an in-transaction ActionEvent append, so state and audit can
never diverge."""
from models import db
from r6.actions.models import ProposedAction, _TRANSITIONS
from r6.actions.events import ActionEvent

class IllegalTransition(Exception):
    pass

def transition_action(action_id, from_states, to_state, actor, detail=None,
                      extra_criteria=None, **fields):
    """Guarded transition. Flips action_id from any of from_states to to_state
    only if the row currently matches (atomic WHERE). Returns True if it moved
    (and writes one ActionEvent in the same commit), False if the WHERE matched
    nothing (concurrent claim / already advanced). Raises IllegalTransition if
    to_state isn't reachable from every from_state, if from_states is empty,
    or if 'status' or 'payload_json' is passed via fields.

    extra_criteria: optional iterable of SQLAlchemy predicates appended to
    the guarded UPDATE's WHERE, making the claim STRICTER — e.g. the expiry
    re-check on the commit/confirm claims closes the TOCTOU between a
    route's snapshot check and the claim. A False return then means the row
    failed ANY criterion; the caller disambiguates by refreshing the row.

    Commits the session in all cases (including the False path); do not call
    with unrelated pending changes staged. Caller updates belong in **fields,
    which apply atomically with the transition."""
    if 'status' in fields:
        raise IllegalTransition('status cannot be passed via fields')
    if 'payload_json' in fields:
        # The executable payload is what the human saw; no transition may
        # rewrite it (#528). The bulk UPDATE below bypasses the model-level
        # seal, so this is the only place this writer can be refused — and
        # it is unconditional because the claim runs BEFORE the confirmation
        # row is minted (see the confirm route), so "a confirmation exists"
        # is not yet true at the one moment a swap would matter.
        raise IllegalTransition('payload_json cannot be passed via fields')
    if not from_states:
        raise IllegalTransition('from_states must be non-empty')
    for fs in from_states:
        if to_state not in _TRANSITIONS.get(fs, set()):
            raise IllegalTransition('%s -> %s not permitted' % (fs, to_state))
    updates = dict(fields)
    updates['status'] = to_state
    criteria = [
        ProposedAction.id == action_id,
        ProposedAction.status.in_(tuple(from_states)),
    ]
    if extra_criteria is not None:
        criteria.extend(extra_criteria)
    moved = ProposedAction.query.filter(*criteria).update(
        updates, synchronize_session=False)
    if moved:
        db.session.add(ActionEvent(
            action_id=action_id, from_status=','.join(from_states),
            to_status=to_state, actor=actor, detail=detail))
    db.session.commit()
    return bool(moved)
