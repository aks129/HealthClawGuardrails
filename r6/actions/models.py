"""
ProposedAction — lifecycle record for real-world actions (calls, SMS).

Mirrors the FHIR propose -> commit write pattern: an action is proposed
(draft shown to the patient), confirmed (step-up + human confirmation),
executed (Bland.ai / Twilio), and resolved by webhook callback.

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

# Legal status transitions
_TRANSITIONS = {
    'proposed': {'confirmed', 'expired'},
    'confirmed': {'executing', 'failed'},
    'executing': {'completed', 'failed', 'unknown'},
    'completed': set(),
    'failed': set(),
    'expired': set(),
    'unknown': {'completed', 'failed'},  # late webhook may still resolve it
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
    status = db.Column(db.String(16), nullable=False, default='proposed')
    external_ref = db.Column(db.String(128), nullable=True)
    outcome_summary = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)

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

    def transition(self, new_status):
        allowed = _TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                'Illegal transition %s -> %s' % (self.status, new_status))
        self.status = new_status

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
