"""Out-of-band human approval record. issue_confirmation() is written by the
authenticated Telegram/dashboard approve handler; The confirm route claims FIRST
(the guarded transition is the mutex), then issues + consumes the confirmation
in the next transaction: the claim is the lock, this row is the durable consent
record of who/when/via. TTL means an approval from Tuesday can't authorize a
Thursday commit."""
import hashlib
import json
import uuid
from datetime import timedelta

from models import db
from r6.actions.models import _utcnow

APPROVED_VIA_VALUES = ('telegram', 'dashboard', 'review-page')
ACTION_APPROVAL_AUDIENCE = 'action-approval'


class ActionConfirmation(db.Model):
    __tablename__ = 'action_confirmations'
    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    action_id = db.Column(db.String(64), nullable=False, index=True)
    approved_via = db.Column(db.String(32), nullable=False)  # APPROVED_VIA_VALUES
    approved_at = db.Column(db.DateTime, default=_utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)
    # sha256 over the canonical form of the payload as the human approved it
    # (#559, human-gate spec 8.3). Nullable only for rows minted before the
    # column existed; the confirm route refuses to execute on such a row.
    payload_digest = db.Column(db.String(64), nullable=True)


def payload_digest(payload_json):
    """sha256 hex over the canonical serialisation of a payload.

    Canonical: parsed, then dumped with sorted keys and no whitespace, so
    key order and formatting do not change what the human signed. The
    payload column is always JSON; a value that is not parses as a defect
    and raises rather than hashing bytes that mean nothing.
    """
    canonical = json.dumps(json.loads(payload_json), sort_keys=True,
                           separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def issue_confirmation(action_id, approved_via, ttl_minutes, *, payload_json):
    """Mint the consent record over the payload as it stands right now.

    `payload_json` is required, keyword-only: a confirmation that does not
    say what bytes were approved is the defect #559 names, so no caller
    can mint one by forgetting the argument.
    """
    if approved_via not in APPROVED_VIA_VALUES:
        raise ValueError('Unsupported approval channel: %s' % approved_via)
    c = ActionConfirmation(action_id=action_id, approved_via=approved_via,
                           expires_at=_utcnow() + timedelta(minutes=ttl_minutes),
                           payload_digest=payload_digest(payload_json))
    db.session.add(c)
    return c


def has_confirmation(action_id):
    """True if any ActionConfirmation names action_id — committed, or still
    pending in this session (the review route mints and commits in one
    transaction). Backs the payload seal on ProposedAction (#528): once a
    confirmation exists, the payload is what the human approved. Runs
    without autoflush so a validator never flushes unrelated state as a
    side effect; the pending scan covers what that flush would have found."""
    for obj in db.session.new:
        if isinstance(obj, ActionConfirmation) and obj.action_id == action_id:
            return True
    with db.session.no_autoflush:
        return ActionConfirmation.query.filter_by(
            action_id=action_id).first() is not None


def open_confirmations(action_id):
    """Every unconsumed, unexpired confirmation for action_id: the set the
    confirm route is about to consume, and so the set whose digests must
    all match the payload it is about to execute (#559)."""
    now = _utcnow()
    return ActionConfirmation.query.filter(
        ActionConfirmation.action_id == action_id,
        ActionConfirmation.consumed_at.is_(None),
        ActionConfirmation.expires_at > now,
    ).all()


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
