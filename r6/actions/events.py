"""Append-only lifecycle log for actions. Dashboards, digests, webhook-lag,
dead-letter lists, and per-tenant caps are all VIEWS over this table. Written
in the SAME transaction as every state transition (see r6/actions/state.py)."""
import uuid

from models import db
from r6.actions.models import _utcnow

class ActionEvent(db.Model):
    __tablename__ = 'action_events'
    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    action_id = db.Column(db.String(64), nullable=False, index=True)
    # 128, not 32: transition_action() writes ','.join(from_states) here, and
    # the full-join worst case is already ~70 chars. SQLite masks varchar
    # overflow; Postgres truncation-errors. Width asserted against the state
    # map in tests/actions/test_state_transitions.py.
    from_status = db.Column(db.String(128), nullable=True)
    to_status = db.Column(db.String(32), nullable=False)
    actor = db.Column(db.String(32), nullable=False)  # commit-route|confirm|webhook|reaper|propose
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
