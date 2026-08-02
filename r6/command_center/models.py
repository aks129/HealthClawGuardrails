"""
Command center DB models.

Conversation — durable thread identity shared across messaging surfaces.

ConversationMessage — persists chat turns from the Telegram bot (and any
future channels) so the dashboard can show recent activity by agent.

AgentTask — a lightweight task tracker for pending work surfaced by agents
(e.g., "approve curatr fix", "confirm vaccine due", "review lab result").
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from models import db


def default_conversation_id(tenant_id: str, agent_id: str | None = None) -> str:
    """Stable compatibility thread for callers that do not supply an ID."""
    return "careagents:%s" % agent_id if agent_id else "legacy:%s" % tenant_id


class Conversation(db.Model):
    """A durable, tenant-owned thread that can span web and messaging surfaces."""

    __tablename__ = "cc_conversations"
    __table_args__ = (
        db.PrimaryKeyConstraint(
            "tenant_id", "id", name="pk_cc_conversations"),
    )

    id = db.Column(db.String(128), nullable=False)
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    # Opaque identity in the calling system's namespace (for example a
    # CareAgents agent UUID). It is tenant-scoped, not a command-center FK.
    agent_id = db.Column(db.String(64), nullable=True, index=True)
    created_by_surface = db.Column(db.String(32), nullable=False,
                                   default="unknown")
    status = db.Column(db.String(24), nullable=False, default="active",
                       index=True)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "created_by_surface": self.created_by_surface,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ConversationMessage(db.Model):
    """
    One turn in an agent conversation. A "turn" is a single user message or
    assistant response; multi-turn exchanges produce one row per turn.
    """

    __tablename__ = "cc_conversation_messages"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["cc_conversations.tenant_id", "cc_conversations.id"],
            name="fk_cc_message_conversation",
            ondelete="CASCADE",
        ),
        db.UniqueConstraint(
            "tenant_id", "conversation_id", "request_id",
            name="uq_cc_message_request",
        ),
    )

    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    conversation_id = db.Column(db.String(128), nullable=False, index=True)
    agent_id = db.Column(db.String(64), nullable=True, index=True)
    channel = db.Column(db.String(32), nullable=False, default="unknown")  # telegram, mcp, api, web
    session_id = db.Column(db.String(128), nullable=True, index=True)  # telegram chat_id, mcp session, etc.
    user_id = db.Column(db.String(128), nullable=True)
    role = db.Column(db.String(16), nullable=False)  # user | assistant | system
    text = db.Column(db.Text, nullable=False)
    request_id = db.Column(db.String(128), nullable=True, index=True)
    reply_to = db.Column(db.String(64), nullable=True, index=True)
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
            "conversation_id": self.conversation_id,
            "agent_id": self.agent_id,
            "channel": self.channel,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "role": self.role,
            "text": self.text[:500] if self.text else "",
            "request_id": self.request_id,
            "reply_to": self.reply_to,
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
