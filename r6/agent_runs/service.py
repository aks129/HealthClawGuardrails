"""Transactional AgentRun queue and event operations."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from models import db
from r6.agent_runs.models import (
    AgentRun,
    AgentRunEvent,
    AgentToolCall,
    utcnow,
)
from r6.agent_runs.state import (
    TERMINAL_RUN_STATES,
    InvalidTransition,
    require_run_transition,
    require_tool_transition,
)
from r6.command_center.models import Conversation, ConversationMessage


def create_run(
    tenant_id: str,
    message_id: str,
    *,
    deadline_seconds: int = 120,
) -> tuple[AgentRun, bool]:
    """Create one queued run for an inbound message, idempotently."""
    existing = AgentRun.query.filter_by(
        tenant_id=tenant_id, message_id=message_id).first()
    if existing is not None:
        return existing, False

    message = ConversationMessage.query.filter_by(
        tenant_id=tenant_id, id=message_id).first()
    if message is None or message.role != "user":
        raise LookupError("unknown inbound user message")

    now = utcnow()
    run = AgentRun(
        tenant_id=tenant_id,
        conversation_id=message.conversation_id,
        message_id=message.id,
        agent_id=message.agent_id,
        surface=message.channel,
        status="queued",
        available_at=now,
        deadline_at=now + timedelta(seconds=deadline_seconds),
    )
    db.session.add(run)
    try:
        db.session.flush()
        append_event(run, "run.queued", {"status": "queued"})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        winner = AgentRun.query.filter_by(
            tenant_id=tenant_id, message_id=message_id).first()
        if winner is not None:
            return winner, False
        raise
    return run, True


def append_event(
    run: AgentRun,
    event_type: str,
    payload: dict | list | None = None,
) -> AgentRunEvent:
    event = AgentRunEvent(
        tenant_id=run.tenant_id,
        run_id=run.id,
        event_type=event_type,
        payload_json=_dump(payload) if payload is not None else None,
    )
    db.session.add(event)
    return event


def claim_next(worker_id: str, lease_seconds: int = 60) -> AgentRun | None:
    """Recover expired leases and atomically claim the oldest queued run."""
    now = utcnow()

    expired = (
        AgentRun.query
        .filter(AgentRun.status == "running")
        .filter(AgentRun.lease_expires_at < now)
        .with_for_update(skip_locked=True)
        .all()
    )
    for run in expired:
        if run.cancel_requested:
            transition_run(
                run, "cancelled", event_type="run.cancelled_after_lease",
                commit=False)
        else:
            run.status = "queued"
            run.worker_id = None
            run.lease_expires_at = None
            run.available_at = now
            append_event(run, "run.lease_expired", {
                "attempt": run.attempt,
                "error_class": "WorkerLeaseExpired",
            })

    overdue = (
        AgentRun.query
        .filter(AgentRun.status == "queued")
        .filter(AgentRun.deadline_at <= now)
        .with_for_update(skip_locked=True)
        .all()
    )
    for run in overdue:
        transition_run(
            run,
            "failed",
            event_type="run.deadline_exceeded",
            error_class="RunDeadlineExceeded",
            commit=False,
        )

    candidate_ids = [row[0] for row in (
        db.session.query(AgentRun.id)
        .filter(AgentRun.status == "queued")
        .filter(AgentRun.available_at <= now)
        .filter(AgentRun.deadline_at > now)
        .order_by(AgentRun.available_at.asc(), AgentRun.created_at.asc())
        .limit(50)
        .all()
    )]
    run = None
    for candidate_id in candidate_ids:
        candidate = (
            AgentRun.query
            .filter(AgentRun.id == candidate_id)
            .filter(AgentRun.status == "queued")
            .with_for_update(skip_locked=True)
            .first()
        )
        if candidate is None:
            continue
        # A conversation row is the cross-process mutex. Two workers may see
        # different queued runs in one thread, but only one can lock this row;
        # the loser leaves its run queued for the next claim cycle.
        conversation = (
            Conversation.query
            .filter_by(tenant_id=candidate.tenant_id,
                       id=candidate.conversation_id)
            .with_for_update(skip_locked=True)
            .first()
        )
        if conversation is None:
            continue
        already_running = (
            AgentRun.query
            .filter_by(tenant_id=candidate.tenant_id,
                       conversation_id=candidate.conversation_id,
                       status="running")
            .first()
        )
        if already_running is not None:
            continue
        run = candidate
        break

    if run is None:
        db.session.commit()
        return None

    # Re-check after the locks above; this is intentionally stricter than the
    # candidate scan so stale queue snapshots cannot win a claim.
    run = (
        AgentRun.query
        .filter(AgentRun.id == run.id, AgentRun.status == "queued")
        .first()
    )
    if run is None:
        db.session.commit()
        return None

    require_run_transition(run.status, "running")
    run.status = "running"
    run.worker_id = worker_id
    run.attempt += 1
    run.started_at = run.started_at or now
    run.heartbeat_at = now
    run.lease_expires_at = now + timedelta(seconds=lease_seconds)
    append_event(run, "run.started", {
        "status": "running",
        "attempt": run.attempt,
    })
    db.session.commit()
    return run


def heartbeat(run: AgentRun, worker_id: str, lease_seconds: int = 60) -> None:
    if run.status != "running" or run.worker_id != worker_id:
        raise InvalidTransition("worker does not own a running lease")
    now = utcnow()
    run.heartbeat_at = now
    run.lease_expires_at = now + timedelta(seconds=lease_seconds)
    db.session.commit()


def transition_run(
    run: AgentRun,
    target: str,
    *,
    event_type: str | None = None,
    payload: dict | list | None = None,
    error_class: str | None = None,
    available_in_seconds: int = 0,
    commit: bool = True,
) -> AgentRun:
    if run.cancel_requested and target != "cancelled":
        raise InvalidTransition("run cancellation was requested")
    require_run_transition(run.status, target)
    now = utcnow()
    run.status = target
    run.error_class = error_class
    if target == "queued":
        run.worker_id = None
        run.lease_expires_at = None
        run.available_at = now + timedelta(seconds=available_in_seconds)
    elif target in TERMINAL_RUN_STATES:
        run.finished_at = now
        run.lease_expires_at = None
        run.worker_id = None
    elif target == "waiting_for_human":
        run.lease_expires_at = None
        run.worker_id = None
    append_event(
        run,
        event_type or f"run.{target}",
        payload if payload is not None else {"status": target},
    )
    if commit:
        db.session.commit()
    return run


def request_cancel(run: AgentRun) -> AgentRun:
    if run.status in TERMINAL_RUN_STATES:
        return run
    if run.status in ("queued", "waiting_for_human"):
        return transition_run(run, "cancelled")
    run.cancel_requested = True
    append_event(run, "run.cancel_requested", {"status": run.status})
    db.session.commit()
    return run


def register_tool_call(
    run: AgentRun,
    provider_call_id: str,
    tool_name: str,
    arguments: dict,
) -> tuple[AgentToolCall, bool]:
    encoded = _dump(arguments)
    input_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    existing = AgentToolCall.query.filter_by(
        run_id=run.id, provider_call_id=provider_call_id).first()
    if existing is not None:
        if existing.tool_name != tool_name or existing.input_hash != input_hash:
            raise InvalidTransition(
                "provider_call_id was reused with different tool input")
        return existing, False
    call = AgentToolCall(
        tenant_id=run.tenant_id,
        run_id=run.id,
        provider_call_id=provider_call_id,
        tool_name=tool_name,
        input_hash=input_hash,
        input_json=encoded,
        status="pending",
    )
    db.session.add(call)
    try:
        db.session.flush()
        append_event(run, "tool.registered", {
            "tool_call_id": call.id,
            "tool_name": tool_name,
            "input_hash": input_hash,
        })
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        winner = AgentToolCall.query.filter_by(
            run_id=run.id, provider_call_id=provider_call_id).first()
        if winner is None:
            raise
        if winner.tool_name != tool_name or winner.input_hash != input_hash:
            raise InvalidTransition(
                "provider_call_id was reused with different tool input")
        return winner, False
    return call, True


def transition_tool_call(
    run: AgentRun,
    call: AgentToolCall,
    target: str,
    *,
    result: dict | list | str | None = None,
    outcome_ref: str | None = None,
    error_class: str | None = None,
) -> AgentToolCall:
    if call.run_id != run.id or call.tenant_id != run.tenant_id:
        raise LookupError("tool call does not belong to run")
    require_tool_transition(call.status, target)
    now = utcnow()
    call.status = target
    call.error_class = error_class
    call.outcome_ref = outcome_ref
    if target == "running":
        call.attempt += 1
        call.started_at = now
        call.finished_at = None
        call.result_json = None
    if target in ("completed", "failed", "needs_reconciliation"):
        call.finished_at = now
        call.result_json = _dump(result) if result is not None else None
    append_event(run, f"tool.{target}", {
        "tool_call_id": call.id,
        "tool_name": call.tool_name,
        "status": target,
        "attempt": call.attempt,
        "outcome_ref": outcome_ref,
        "error_class": error_class,
    })
    db.session.commit()
    return call


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
