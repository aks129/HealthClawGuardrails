"""
ProposedAction — lifecycle record for real-world actions (calls, SMS).

Mirrors the FHIR propose -> commit write pattern: an action is proposed
(draft shown to the patient), submitted for approval (commit ->
awaiting_confirmation), claimed and executed by the human's out-of-band
approval (awaiting_confirmation -> executing, Bland.ai / Twilio), and
resolved (completed/failed/needs_review/unknown) by executor result or
webhook callback.

PHI note: payload_json holds the verbatim script (needed to execute) and
is tenant-scoped like R6Resource. summary() is the ONLY representation
allowed in audit detail and Telegram notifications.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from models import db

PROPOSAL_TTL_MINUTES = 30

VALID_KINDS = ('phone-call', 'sms', 'form-fill', 'insurance-call')

# Legal status transitions. awaiting_confirmation is the out-of-band gate:
# commit submits (proposed->awaiting_confirmation), the human's approval claims
# (awaiting_confirmation->executing). expiry from awaiting_confirmation is the
# COMMON path (proposals linger hours awaiting a human). needs_review = executed
# but outcome unconfirmable (carries evidence). unknown = post-possible-send.
_TRANSITIONS = {
    'proposed': {'awaiting_confirmation', 'expired'},
    'awaiting_confirmation': {'executing', 'expired'},
    'executing': {'completed', 'failed', 'needs_review', 'unknown'},
    'completed': set(),
    'failed': set(),
    'needs_review': set(),
    'expired': set(),
    'unknown': {'completed', 'failed', 'needs_review'},
}


def _utcnow():
    # Stored naive-UTC; columns aren't timezone-aware, so this matches what other models' aware defaults become after DB round-trip.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProposedAction(db.Model):
    __tablename__ = 'proposed_actions'

    id = db.Column(db.String(64), primary_key=True,
                   default=lambda: str(uuid.uuid4()))
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    kind = db.Column(db.String(32), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    # 32, not 16: 'awaiting_confirmation' is 21 chars. SQLite ignores varchar
    # width so tests can't catch truncation at write time — Postgres raises
    # StringDataRightTruncation. Width is asserted against the state map in
    # tests/actions/test_state_transitions.py; schema_sync auto-widens the
    # live column at boot when the model length exceeds it.
    status = db.Column(db.String(32), nullable=False, default='proposed')
    external_ref = db.Column(db.String(128), nullable=True)
    outcome_summary = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)

    # Attempt ledger (crash-recovery; see r6/actions/state.py + the ops reaper).
    # attempt_id = idempotency key set at claim; provider_request_at is stamped
    # immediately before the provider POST so a crash is distinguishable:
    # claimed-but-never-called (safe to fail) vs called-but-unresolved (review).
    attempt_id = db.Column(db.String(64), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    provider_request_at = db.Column(db.DateTime, nullable=True)

    def __init__(self, tenant_id, kind, payload, **kwargs):
        if kind not in VALID_KINDS:
            raise ValueError('Unsupported action kind: %s' % kind)
        super().__init__(
            tenant_id=tenant_id,
            kind=kind,
            payload_json=json.dumps(payload),
            expires_at=_utcnow() + timedelta(minutes=PROPOSAL_TTL_MINUTES),
            **kwargs,
        )

    def is_expired(self):
        return _utcnow() >= self.expires_at

    @property
    def payload(self):
        return json.loads(self.payload_json)

    def summary(self):
        """PHI-safe representation — the only shape allowed in audit/notify."""
        p = self.payload
        return {
            'id': self.id,
            'kind': self.kind,
            'to': p.get('to'),          # recipient label, e.g. "CVS Pharmacy"
            'status': self.status,
            'expires_at': self.expires_at.replace(tzinfo=None).isoformat() + 'Z',
        }

    def to_dict(self):
        """Full representation for the owning tenant (includes draft)."""
        d = self.summary()
        d['payload'] = self.payload
        d['external_ref'] = self.external_ref
        d['outcome_summary'] = self.outcome_summary
        return d
