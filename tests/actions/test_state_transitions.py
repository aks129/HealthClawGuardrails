import json
from models import db
from r6.actions.events import ActionEvent


def test_action_event_row_persists(app):
    with app.app_context():
        ev = ActionEvent(action_id='a1', from_status='proposed',
                         to_status='awaiting_confirmation', actor='commit-route',
                         detail='submitted')
        db.session.add(ev); db.session.commit()
        got = ActionEvent.query.filter_by(action_id='a1').one()
        assert got.from_status == 'proposed'
        assert got.to_status == 'awaiting_confirmation'
        assert got.actor == 'commit-route'
        assert got.created_at is not None


from r6.actions.models import ProposedAction
from r6.actions.state import transition_action, IllegalTransition
import pytest


def _make(app, status='proposed'):
    a = ProposedAction(tenant_id='t1', kind='sms', payload={'body': 'hi'})
    a.status = status
    db.session.add(a); db.session.commit()
    return a.id


def test_guarded_transition_succeeds_and_logs(app):
    with app.app_context():
        aid = _make(app, 'proposed')
        ok = transition_action(aid, from_states=('proposed',),
                               to_state='awaiting_confirmation', actor='commit-route')
        assert ok is True
        assert ProposedAction.query.get(aid).status == 'awaiting_confirmation'
        assert ActionEvent.query.filter_by(action_id=aid).count() == 1


def test_guarded_transition_noop_when_state_mismatch(app):
    with app.app_context():
        aid = _make(app, 'executing')
        ok = transition_action(aid, from_states=('proposed',),
                               to_state='awaiting_confirmation', actor='commit-route')
        assert ok is False
        assert ActionEvent.query.filter_by(action_id=aid).count() == 0


def test_illegal_transition_rejected(app):
    with app.app_context():
        aid = _make(app, 'completed')
        with pytest.raises(IllegalTransition):
            transition_action(aid, from_states=('completed',),
                              to_state='executing', actor='commit-route')


def test_new_states_present():
    from r6.actions.models import _TRANSITIONS
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
    from r6.actions.models import _TRANSITIONS
    all_states = _TRANSITIONS.keys() | set().union(*_TRANSITIONS.values())
    longest = max(len(s) for s in all_states)
    assert ProposedAction.__table__.c.status.type.length >= longest
