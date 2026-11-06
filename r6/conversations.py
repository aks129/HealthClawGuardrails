"""
Conversation threads and their messages: the engine's record of a chat.

Conversation — durable thread identity shared across messaging surfaces.

ConversationMessage — one persisted turn, from any channel.

A chat transcript is PHI-adjacent, so these are the tables the engine
writes transcripts to, per tenant. Optional modules such as the command
center read and write them; they do not own them.

The tables keep their original ``cc_`` names on purpose: renaming them
would need a migration, and the prefix is history, not ownership.
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
    # CareAgents agent UUID). It is tenant-scoped, not a foreign key.
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


#: The surfaces a conversation turn may arrive on (#884 security note).
#: The channel was checked for length only, and it is copied into AuditEvent
#: detail, so a caller could write any 32 characters into the audit trail.
#: "unknown" is the route's own default for a write that names none.
#: "voice" is a CareAgents web turn that started as speech.
KNOWN_CHANNELS = frozenset({
    "web", "imessage", "telegram", "sms", "voice", "mcp", "api", "worker",
    "unknown"})


def audit_channel(channel) -> str:
    """The channel as audit detail may carry it: a known one, else "other".
    A row stored before the allowlist keeps its value; its audit does not."""
    return (channel if isinstance(channel, str) and channel in KNOWN_CHANNELS
            else "other")


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
