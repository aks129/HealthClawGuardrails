"""
Command center DB models.

ConversationMessage — persists chat turns from the Telegram bot (and any
future channels) so the dashboard can show recent activity by agent.

AgentTask — a lightweight task tracker for pending work surfaced by agents
(e.g., "approve curatr fix", "confirm vaccine due", "review lab result").
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from models import db


class ConversationMessage(db.Model):
    """
    One turn in an agent conversation. A "turn" is a single user message or
    assistant response; multi-turn exchanges produce one row per turn.
    """

    __tablename__ = "cc_conversation_messages"

    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    agent_id = db.Column(db.String(64), nullable=True, index=True)
    channel = db.Column(db.String(32), nullable=False, default="unknown")  # telegram, mcp, api, web
    session_id = db.Column(db.String(128), nullable=True, index=True)  # telegram chat_id, mcp session, etc.
    user_id = db.Column(db.String(128), nullable=True)
    role = db.Column(db.String(16), nullable=False)  # user | assistant | system
    text = db.Column(db.Text, nullable=False)
    metadata_json = db.Column(db.Text, nullable=True)  # tool calls, latency, token counts
    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "channel": self.channel,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "role": self.role,
            "text": self.text[:500] if self.text else "",
            "truncated": bool(self.text) and len(self.text) > 500,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


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
