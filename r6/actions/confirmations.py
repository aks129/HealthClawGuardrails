"""Out-of-band human approval record. issue_confirmation() is written by the
authenticated Telegram/dashboard approve handler; consume_confirmation() is
called INSIDE the claim transaction so approval and execution are atomic and
single-use (an approval from Tuesday can't authorize a Thursday commit)."""
import uuid
from datetime import timedelta

from models import db
from r6.actions.models import _utcnow

APPROVED_VIA_VALUES = ('telegram', 'dashboard')


class ActionConfirmation(db.Model):
    __tablename__ = 'action_confirmations'
    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    action_id = db.Column(db.String(64), nullable=False, index=True)
    approved_via = db.Column(db.String(32), nullable=False)  # telegram | dashboard
    approved_at = db.Column(db.DateTime, default=_utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)


def issue_confirmation(action_id, approved_via, ttl_minutes):
    c = ActionConfirmation(action_id=action_id, approved_via=approved_via,
                           expires_at=_utcnow() + timedelta(minutes=ttl_minutes))
    db.session.add(c)
    return c


def consume_confirmation(action_id):
    """Atomically claim every unconsumed, unexpired confirmation for
    action_id. Returns True iff at least one row was consumed by THIS call
    (guarded UPDATE). Does NOT commit — the caller owns the transaction so
    consumption lands atomically with the action claim (see the confirm
    route, Task 10).

    All open confirmations for the action are consumed together — one
    approval event authorizes at most one execution. A second Approve tap
    from another device finds nothing left to consume; the actual
    single-winner guarantee comes from Task 10's claim transition, not
    from this table having exactly one open row.
    """
    now = _utcnow()
    consumed = ActionConfirmation.query.filter(
        ActionConfirmation.action_id == action_id,
        ActionConfirmation.consumed_at.is_(None),
        ActionConfirmation.expires_at > now,
    ).update({'consumed_at': now}, synchronize_session=False)
    return bool(consumed)
