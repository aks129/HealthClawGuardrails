"""Durable agent run, tool-call, and append-only event records."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from models import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentRun(db.Model):
    __tablename__ = "agent_runs"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["cc_conversations.tenant_id", "cc_conversations.id"],
            name="fk_agent_run_conversation",
            ondelete="CASCADE",
        ),
        db.UniqueConstraint(
            "tenant_id", "message_id", name="uq_agent_run_message"),
        db.CheckConstraint(
            "status IN ('queued','running','waiting_for_human',"
            "'completed','failed','cancelled')",
            name="ck_agent_run_status",
        ),
    )

    id = db.Column(
        db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    conversation_id = db.Column(db.String(128), nullable=False, index=True)
    message_id = db.Column(
        db.String(64), db.ForeignKey(
            "cc_conversation_messages.id", ondelete="CASCADE"),
        nullable=False, index=True)
    agent_id = db.Column(db.String(64), nullable=True, index=True)
    surface = db.Column(db.String(32), nullable=False, default="unknown")
    status = db.Column(db.String(24), nullable=False, default="queued", index=True)
    attempt = db.Column(db.Integer, nullable=False, default=0)
    worker_id = db.Column(db.String(128), nullable=True, index=True)
    available_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    deadline_at = db.Column(db.DateTime, nullable=False, index=True)
    lease_expires_at = db.Column(db.DateTime, nullable=True, index=True)
    heartbeat_at = db.Column(db.DateTime, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    error_class = db.Column(db.String(128), nullable=True, index=True)
    cancel_requested = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "agent_id": self.agent_id,
            "surface": self.surface,
            "status": self.status,
            "attempt": self.attempt,
            "worker_id": self.worker_id,
            "available_at": _iso(self.available_at),
            "deadline_at": _iso(self.deadline_at),
            "lease_expires_at": _iso(self.lease_expires_at),
            "heartbeat_at": _iso(self.heartbeat_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "error_class": self.error_class,
            "cancel_requested": bool(self.cancel_requested),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class AgentToolCall(db.Model):
    __tablename__ = "agent_tool_calls"
    __table_args__ = (
        db.UniqueConstraint(
            "run_id", "provider_call_id", name="uq_agent_tool_call_provider"),
        db.CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="ck_agent_tool_call_status",
        ),
    )

    id = db.Column(
        db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    run_id = db.Column(
        db.String(64), db.ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False, index=True)
    provider_call_id = db.Column(db.String(128), nullable=False)
    tool_name = db.Column(db.String(128), nullable=False, index=True)
    input_hash = db.Column(db.String(64), nullable=False)
    input_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(24), nullable=False, default="pending", index=True)
    attempt = db.Column(db.Integer, nullable=False, default=0)
    result_json = db.Column(db.Text, nullable=True)
    outcome_ref = db.Column(db.String(256), nullable=True, index=True)
    error_class = db.Column(db.String(128), nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def to_dict(self, include_payload: bool = False) -> dict:
        result = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "provider_call_id": self.provider_call_id,
            "tool_name": self.tool_name,
            "input_hash": self.input_hash,
            "status": self.status,
            "attempt": self.attempt,
            "outcome_ref": self.outcome_ref,
            "error_class": self.error_class,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_payload:
            result["input"] = _json(self.input_json)
            result["result"] = _json(self.result_json)
        return result


class AgentRunEvent(db.Model):
    __tablename__ = "agent_run_events"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    run_id = db.Column(
        db.String(64), db.ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False, index=True)
    event_type = db.Column(db.String(64), nullable=False, index=True)
    payload_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    def to_dict(self, include_payload: bool = True) -> dict:
        result = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "type": self.event_type,
            "created_at": _iso(self.created_at),
        }
        if include_payload:
            result["payload"] = _json(self.payload_json) or {}
        return result


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _json(value: str | None):
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
