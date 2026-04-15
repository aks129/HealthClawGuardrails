"""
WearableConnection — maps a HealthClaw tenant to an Open Wearables user.

Open Wearables holds the OAuth tokens; we only store the pairing plus
last-sync status. Each tenant can have multiple connections, one per
provider (garmin, oura, polar, etc.).
"""

from datetime import datetime, timezone
from models import db


SUPPORTED_PROVIDERS = (
    'garmin', 'oura', 'polar', 'suunto',
    'whoop', 'fitbit', 'strava', 'ultrahuman',
)


class WearableConnection(db.Model):
    __tablename__ = 'wearable_connections'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    provider = db.Column(db.String(32), nullable=False)
    ow_user_id = db.Column(db.String(128), nullable=False)
    patient_ref = db.Column(db.String(128))
    connected_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_sync_at = db.Column(db.DateTime)
    last_sync_status = db.Column(db.String(32), default='never')
    last_sync_detail = db.Column(db.Text)
    observation_count = db.Column(db.Integer, default=0)

    __table_args__ = (
        db.UniqueConstraint(
            'tenant_id', 'provider', 'ow_user_id',
            name='uq_wearable_tenant_provider_user',
        ),
    )

    def to_dict(self):
        return {
            'provider': self.provider,
            'ow_user_id': self.ow_user_id,
            'patient_ref': self.patient_ref,
            'connected_at': (
                self.connected_at.isoformat() if self.connected_at else None
            ),
            'last_sync_at': (
                self.last_sync_at.isoformat() if self.last_sync_at else None
            ),
            'last_sync_status': self.last_sync_status or 'never',
            'last_sync_detail': self.last_sync_detail,
            'observation_count': self.observation_count or 0,
        }
