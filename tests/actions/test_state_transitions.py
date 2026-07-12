import pytest

from models import db
from r6.actions.events import ActionEvent
from r6.actions.models import ProposedAction, _TRANSITIONS
from r6.actions.state import transition_action, IllegalTransition

# Actor vocabulary written by callers of transition_action (see the comment
# on ActionEvent.actor). Used to assert the column fits every value.
_ACTORS = ('commit-route', 'confirm', 'webhook', 'reaper', 'propose')


def _all_states():
    return _TRANSITIONS.keys() | set().union(*_TRANSITIONS.values())


def test_action_event_row_persists(app):
    with app.app_context():
        ev = ActionEvent(action_id='a1', from_status='proposed',
                         to_status='awaiting_confirmation', actor='commit-route',
                         detail='submitted')
        db.session.add(ev)
        db.session.commit()
        got = ActionEvent.query.filter_by(action_id='a1').one()
        assert got.from_status == 'proposed'
        assert got.to_status == 'awaiting_confirmation'
        assert got.actor == 'commit-route'
        assert got.created_at is not None


def _make(status='proposed'):
    a = ProposedAction(tenant_id='t1', kind='sms', payload={'body': 'hi'})
    a.status = status
    db.session.add(a)
    db.session.commit()
    return a.id


def test_guarded_transition_succeeds_and_logs(app):
    with app.app_context():
        aid = _make('proposed')
        ok = transition_action(aid, from_states=('proposed',),
                               to_state='awaiting_confirmation', actor='commit-route')
        assert ok is True
        assert db.session.get(ProposedAction, aid).status == 'awaiting_confirmation'
        assert ActionEvent.query.filter_by(action_id=aid).count() == 1


def test_guarded_transition_noop_when_state_mismatch(app):
    with app.app_context():
        aid = _make('executing')
        ok = transition_action(aid, from_states=('proposed',),
                               to_state='awaiting_confirmation', actor='commit-route')
        assert ok is False
        assert db.session.get(ProposedAction, aid).status == 'executing'
        assert ActionEvent.query.filter_by(action_id=aid).count() == 0


def test_multi_state_guard_transition(app):
    # from_states=('executing','unknown') is the webhook's first-verdict-wins
    # guard: 'completed' must be reachable from BOTH for the call to be legal.
    with app.app_context():
        aid = _make('unknown')
        ok = transition_action(aid, from_states=('executing', 'unknown'),
                               to_state='completed', actor='webhook')
        assert ok is True
        assert db.session.get(ProposedAction, aid).status == 'completed'
        assert ActionEvent.query.filter_by(action_id=aid).count() == 1


def test_illegal_transition_rejected(app):
    with app.app_context():
        aid = _make('completed')
        with pytest.raises(IllegalTransition):
            transition_action(aid, from_states=('completed',),
                              to_state='executing', actor='commit-route')


def test_status_via_fields_rejected(app):
    # **fields must never smuggle a status that bypasses the legality check.
    with app.app_context():
        aid = _make('proposed')
        with pytest.raises(IllegalTransition):
            transition_action(aid, from_states=('proposed',),
                              to_state='awaiting_confirmation',
                              actor='commit-route', status='completed')
        assert db.session.get(ProposedAction, aid).status == 'proposed'
        assert ActionEvent.query.filter_by(action_id=aid).count() == 0


def test_empty_from_states_rejected(app):
    # An empty guard would skip the legality loop entirely and emit a
    # degenerate WHERE status IN () — reject it outright.
    with app.app_context():
        aid = _make('proposed')
        with pytest.raises(IllegalTransition):
            transition_action(aid, from_states=(),
                              to_state='expired', actor='reaper')


def test_new_states_present():
    assert 'awaiting_confirmation' in _TRANSITIONS['proposed']
    assert 'executing' in _TRANSITIONS['awaiting_confirmation']
    assert 'expired' in _TRANSITIONS['awaiting_confirmation']
    assert 'needs_review' in _TRANSITIONS['executing']


def test_status_column_fits_every_legal_state():
    # SQLite ignores VARCHAR length, so an under-width column passes every
    # test here and then raises StringDataRightTruncation on Postgres the
    # first time the state is written (same class of bug as
    # tests/test_ingest_resilience.py::test_resource_id_column_fits_real_ehr_ids).
    # Computed from the state map, not hardcoded, so a future state rename
    # can never silently reintroduce it.
    longest = max(len(s) for s in _all_states())
    assert ProposedAction.__table__.c.status.type.length >= longest
    assert ActionEvent.__table__.c.to_status.type.length >= longest
    assert ActionEvent.__table__.c.actor.type.length >= max(len(a) for a in _ACTORS)


def test_from_status_column_fits_worst_case_join():
    # transition_action() writes ','.join(from_states) into
    # ActionEvent.from_status. Assert the column can hold the WORST-CASE
    # join of every legal state, so a future multi-state guard or state
    # rename can never truncate on Postgres (SQLite masks it, as above).
    worst_case = len(','.join(sorted(_all_states())))
    assert ActionEvent.__table__.c.from_status.type.length >= worst_case
