"""Out-of-band human approval record. issue_confirmation() is written by the
authenticated Telegram/dashboard approve handler; The confirm route claims FIRST
(the guarded transition is the mutex), then issues + consumes the confirmation
in the next transaction: the claim is the lock, this row is the durable consent
record of who/when/via. TTL means an approval from Tuesday can't authorize a
Thursday commit."""
import hashlib
import hmac
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
    # Keyed digest (HMAC) over the canonical form of the payload as the human
    # approved it (#559, human-gate spec 8.3); keyed so a writer with table
    # access cannot forge it alongside the payload (QA on #658). Nullable only for rows minted before the
    # column existed; the confirm route refuses to execute on such a row.
    payload_digest = db.Column(db.String(64), nullable=True)


_DIGEST_KEY_LABEL = b'healthclaw-confirmation-digest:'


def _digest_key():
    """A key the database does not hold.

    The QA pass on #658 showed why a plain hash is not enough: the digest
    lives in a row that the same writer who can swap the payload can also
    rewrite, so a forged payload plus a forged digest walked through the
    check and the audit line vouched for it. Keying the digest with the
    step-up secret means recomputing it needs more than table access. The
    secret is the one the whole human gate already stands on; without it
    nothing can be approved here either, which is the right way to fail.
    """
    from r6.stepup import _get_secret
    secret = _get_secret()
    if not secret:
        raise ValueError('STEP_UP_SECRET environment variable is required')
    return hashlib.sha256(_DIGEST_KEY_LABEL + secret.encode('utf-8')).digest()


def payload_digest(payload_json):
    """HMAC-SHA256 hex over the canonical serialisation of a payload, keyed
    by a key derived from the step-up secret (see _digest_key).

    Canonical: parsed, then dumped with sorted keys and no whitespace, so
    key order and formatting do not change what the human signed. The
    payload column is always JSON; a value that is not parses as a defect
    and raises rather than hashing bytes that mean nothing.
    """
    canonical = json.dumps(json.loads(payload_json), sort_keys=True,
                           separators=(',', ':'), ensure_ascii=False)
    return hmac.new(_digest_key(), canonical.encode('utf-8'),
                    hashlib.sha256).hexdigest()


def approval_operation(action_id, payload_json):
    """The operation an approval credential is bound to: the action AND the
    keyed digest of the payload the person was shown when it was minted
    (#659). The confirm route recomputes it from the payload about to
    execute, so a payload swapped between the push and the tap fails the
    token's own operation check before any claim, nonce or execution.
    """
    return '%s#%s' % (action_id, payload_digest(payload_json))


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
