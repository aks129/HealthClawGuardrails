"""Append-only lifecycle log for actions. Dashboards, digests, webhook-lag,
dead-letter lists, and per-tenant caps are all VIEWS over this table. Written
in the SAME transaction as every state transition (see r6/actions/state.py)."""
import uuid
from datetime import datetime, timezone
from models import db

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class ActionEvent(db.Model):
    __tablename__ = 'action_events'
    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    action_id = db.Column(db.String(64), nullable=False, index=True)
    from_status = db.Column(db.String(32), nullable=True)
    to_status = db.Column(db.String(32), nullable=False)
    actor = db.Column(db.String(32), nullable=False)  # commit-route|confirm|webhook|reaper|propose
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
