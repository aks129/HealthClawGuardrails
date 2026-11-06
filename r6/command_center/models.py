"""
Command center DB models.

Conversation and ConversationMessage live in r6/conversations.py, which is
core; they are re-exported here for existing callers.

AgentTask — a lightweight task tracker for pending work surfaced by agents
(e.g., "approve curatr fix", "confirm vaccine due", "review lab result").
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from models import db
from r6.conversations import (  # noqa: F401  (re-exported)
    KNOWN_CHANNELS,
    Conversation,
    ConversationMessage,
    audit_channel,
    default_conversation_id,
)


class AgentTask(db.Model):
    """
    A unit of pending work attributed to an agent. Surfaced in the command
    center's "Pending Tasks" panel.
    """

    __tablename__ = "cc_agent_tasks"

    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    agent_id = db.Column(db.String(64), nullable=False, index=True)
    title = db.Column(db.String(256), nullable=False)
    description = db.Column(db.Text, nullable=True)
    # pending | in_progress | completed | dismissed
    status = db.Column(db.String(24), nullable=False, default="pending", index=True)
    # low | medium | high | critical
    priority = db.Column(db.String(16), nullable=False, default="medium")
    resource_ref = db.Column(db.String(256), nullable=True)  # FHIR reference e.g., "Condition/abc"
    source = db.Column(db.String(64), nullable=True)  # what generated this — curatr, care-gap, telegram, etc.
    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    due_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "resource_ref": self.resource_ref,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "due_at": self.due_at.isoformat() if self.due_at else None,
        }
