"""SMBP session model — app state for a 14-day home BP monitoring order.

Like ProposedAction in r6/actions, this is application state (not a FHIR
resource); the readings themselves are FHIR Observations.
"""

import uuid
from datetime import datetime, timezone

from r6.models import db


def _utcnow():
    return datetime.now(timezone.utc)


class SMBPSession(db.Model):
    __tablename__ = "smbp_sessions"

    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    patient_ref = db.Column(db.String(128), nullable=False)
    language = db.Column(db.String(8), nullable=False, default="en")
    days = db.Column(db.Integer, nullable=False, default=14)
    started = db.Column(db.DateTime, nullable=False, default=_utcnow)
    consent_captured = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "patient_ref": self.patient_ref,
            "language": self.language,
            "days": self.days,
            "started": self.started.isoformat() if self.started else None,
            "consent_captured": self.consent_captured,
        }
