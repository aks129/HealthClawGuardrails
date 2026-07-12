"""ActionConfirmation: single-use, TTL-bound, out-of-band human approval.

issue_confirmation() is written by the authenticated Telegram/dashboard
approve handler. consume_confirmation() is called INSIDE the claim
transaction (Task 10) so approval and execution are atomic — an approval
from Tuesday can't authorize a Thursday commit, and it does not commit
itself (the caller owns the transaction), so tests here commit explicitly.
"""
from datetime import datetime, timedelta, timezone

import pytest

from models import db
from r6.actions.confirmations import (
    APPROVED_VIA_VALUES,
    ActionConfirmation,
    issue_confirmation,
    consume_confirmation,
)


def _naive(dt):
    return dt.replace(tzinfo=None)


def test_issue_then_consume_once(app):
    with app.app_context():
        issue_confirmation('a1', approved_via='telegram', ttl_minutes=15)
        db.session.commit()
        assert consume_confirmation('a1') is True
        db.session.commit()
        assert consume_confirmation('a1') is False


def test_expired_confirmation_refused(app):
    with app.app_context():
        c = issue_confirmation('a2', approved_via='dashboard', ttl_minutes=15)
        c.expires_at = _naive(datetime.now(timezone.utc) - timedelta(minutes=1))
        db.session.commit()
        assert consume_confirmation('a2') is False


def test_two_confirmations_only_one_needed(app):
    # Double-tap Approve on two devices: two rows exist for the same action.
    # consume_confirmation() UPDATEs ALL matching (unconsumed, unexpired)
    # rows in a single statement, so the first call consumes both and the
    # second call finds nothing left. "One approval event authorizes at
    # most one execution" is enforced by Task 10's single-winner claim
    # transition, not by this table having exactly one open row.
    with app.app_context():
        issue_confirmation('a3', approved_via='telegram', ttl_minutes=15)
        issue_confirmation('a3', approved_via='dashboard', ttl_minutes=15)
        db.session.commit()
        assert consume_confirmation('a3') is True
        db.session.commit()
        assert consume_confirmation('a3') is False


def test_unknown_approval_channel_rejected(app):
    # Mirrors VALID_KINDS gating ProposedAction.__init__: approved_via is
    # audit vocabulary, so an unrecognized channel is a programming error,
    # not a row to store.
    with app.app_context():
        with pytest.raises(ValueError):
            issue_confirmation('a9', approved_via='carrier-pigeon',
                               ttl_minutes=15)


def test_approved_via_column_fits_documented_vocabulary(app):
    # Computed from the enforced vocabulary constant, not hardcoded, per
    # house rule: a future channel name can't silently truncate on Postgres
    # (SQLite masks varchar overflow — see the analogous status-width
    # tests in tests/actions/test_state_transitions.py).
    with app.app_context():
        longest = max(len(v) for v in APPROVED_VIA_VALUES)
        assert ActionConfirmation.__table__.c.approved_via.type.length >= longest
