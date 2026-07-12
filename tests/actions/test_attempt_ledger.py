"""Attempt-ledger columns on ProposedAction (crash-recovery bookkeeping).

attempt_id/claimed_at/provider_request_at let the ops reaper distinguish
claimed-but-never-called (safe to fail) from called-but-unresolved (needs
review) after a crash. See r6/actions/state.py for how the columns are
written during a claim.
"""
from models import db
from r6.actions.models import ProposedAction


def test_attempt_fields_default_null(app):
    with app.app_context():
        a = ProposedAction(tenant_id='t1', kind='sms', payload={'body': 'x'})
        db.session.add(a)
        db.session.commit()
        assert a.attempt_id is None
        assert a.claimed_at is None
        assert a.provider_request_at is None
