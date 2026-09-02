"""ProposedAction.payload_json is sealed once a confirmation exists (#528).

The ActionConfirmation row is the human's signature over the payload they
saw. A payload that changes after it is minted means the ledger cannot say
what was approved (human-gate spec §9 R2). The seal fires on every ORM
assignment, sees a confirmation that is still pending in the session, and
leaves the stored value untouched. transition_action()'s **fields is the
other writer — bulk UPDATE never reaches a validator — and
tests/actions/test_state_transitions.py pins that one.
"""
import json

import pytest

from models import db
from r6.actions.confirmations import consume_confirmation, issue_confirmation
from r6.actions.models import PayloadSealed, ProposedAction

ORIGINAL = {'to': 'CVS Pharmacy', 'body': 'ORIGINAL TEXT SHOWN TO HUMAN'}
SWAPPED = {'to': 'someone else', 'body': 'SWAPPED AFTER APPROVAL'}


def _make(status='awaiting_confirmation'):
    a = ProposedAction(tenant_id='t1', kind='sms', payload=dict(ORIGINAL))
    a.status = status
    db.session.add(a)
    db.session.commit()
    return a.id


def _stored_payload(aid):
    db.session.expire_all()
    return db.session.get(ProposedAction, aid).payload


def test_payload_sealed_once_a_confirmation_is_committed(app):
    with app.app_context():
        aid = _make()
        issue_confirmation(aid, approved_via='dashboard', ttl_minutes=15)
        db.session.commit()

        action = db.session.get(ProposedAction, aid)
        with pytest.raises(PayloadSealed):
            action.payload_json = json.dumps(SWAPPED)
        db.session.commit()
        assert _stored_payload(aid) == ORIGINAL


def test_payload_sealed_while_the_confirmation_is_still_pending(app):
    # The review route mints and commits in one transaction, so the seal
    # must see a confirmation that has been added but not yet flushed.
    with app.app_context():
        aid = _make()
        action = db.session.get(ProposedAction, aid)
        issue_confirmation(aid, approved_via='review-page', ttl_minutes=15)

        with pytest.raises(PayloadSealed):
            action.payload_json = json.dumps(SWAPPED)
        db.session.commit()
        assert _stored_payload(aid) == ORIGINAL


def test_consumed_confirmation_still_seals(app):
    # /confirm issues and consumes in the same instant, then executes. A
    # spent signature is still a signature: the payload stays what it was.
    with app.app_context():
        aid = _make('executing')
        issue_confirmation(aid, approved_via='telegram', ttl_minutes=15)
        db.session.flush()
        assert consume_confirmation(aid) is True
        db.session.commit()

        action = db.session.get(ProposedAction, aid)
        with pytest.raises(PayloadSealed):
            action.payload_json = json.dumps(SWAPPED)
        db.session.commit()
        assert _stored_payload(aid) == ORIGINAL


def test_payload_writable_before_any_confirmation(app):
    # The seal is keyed on the confirmation, not the status: the review
    # route annotates the payload (reviewed_qr_id) while the action awaits
    # confirmation and only then mints the signature over the result.
    with app.app_context():
        aid = _make()
        action = db.session.get(ProposedAction, aid)
        payload = action.payload
        payload['reviewed_qr_id'] = 'qr-1'
        action.payload_json = json.dumps(payload)
        db.session.commit()
        assert _stored_payload(aid)['reviewed_qr_id'] == 'qr-1'


def test_confirmation_for_another_action_does_not_seal(app):
    # A seal keyed on "any confirmation exists" would pass the tests above
    # and break every action after the first approval — the seal is scoped
    # to the action the confirmation names.
    with app.app_context():
        aid = _make()
        other = _make()
        issue_confirmation(other, approved_via='dashboard', ttl_minutes=15)
        db.session.commit()

        action = db.session.get(ProposedAction, aid)
        action.payload_json = json.dumps(SWAPPED)
        db.session.commit()
        assert _stored_payload(aid) == SWAPPED
        assert _stored_payload(other) == ORIGINAL


def test_constructing_a_new_action_is_not_sealed(app):
    # __init__ assigns payload_json before the row has an id; a confirmation
    # cannot exist for it, and the seal must not query for None.
    with app.app_context():
        a = ProposedAction(tenant_id='t1', kind='sms', payload=dict(ORIGINAL))
        db.session.add(a)
        db.session.commit()
        assert _stored_payload(a.id) == ORIGINAL
