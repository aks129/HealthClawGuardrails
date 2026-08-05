"""Transactional AgentRun queue and event operations."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta, timezone

from sqlalchemy.exc import IntegrityError

from models import db
from r6.agent_runs.models import (
    AgentRun,
    AgentRunEvent,
    AgentToolCall,
    AgentWorkerPresence,
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


def _preserve_ambiguous_tools(
    run: AgentRun,
    *,
    reason: str,
    commit: bool = True,
) -> bool:
    running_calls = (
        AgentToolCall.query
        .filter_by(run_id=run.id, tenant_id=run.tenant_id, status="running")
        .with_for_update()
        .all()
    )
    if not running_calls:
        return False

    # The external side effect may have completed even though its worker
    # disappeared before persisting the outcome. Ambiguity wins over timeout,
    # cancellation, and lease retry: never imply the action did not happen and
    # never make it eligible for automatic execution.
    now = utcnow()
    tool_call_ids = []
    for call in running_calls:
        require_tool_transition(call.status, "needs_reconciliation")
        call.status = "needs_reconciliation"
        call.error_class = "AmbiguousToolOutcome"
        call.finished_at = now
        tool_call_ids.append(call.id)
    require_run_transition(run.status, "waiting_for_human")
    run.status = "waiting_for_human"
    run.error_class = "AmbiguousToolOutcome"
    run.worker_id = None
    run.lease_expires_at = None
    append_event(run, "run.needs_reconciliation", {
        "status": "waiting_for_human",
        "reason": reason,
        "tool_call_ids": tool_call_ids,
        "cancel_requested": bool(run.cancel_requested),
    })
    if commit:
        db.session.commit()
    return True


def _terminalize_at_deadline(
    run: AgentRun,
    *,
    commit: bool = True,
) -> AgentRun:
    if _preserve_ambiguous_tools(
            run, reason="deadline_with_running_tool", commit=commit):
        return run
    if run.cancel_requested:
        return transition_run(
            run,
            "cancelled",
            event_type="run.cancelled_at_deadline",
            commit=commit,
        )
    return transition_run(
        run,
        "failed",
        event_type="run.deadline_exceeded",
        error_class="RunDeadlineExceeded",
        commit=commit,
    )


def _recover_expired_lease(
    run: AgentRun,
    now,
    *,
    commit: bool = True,
) -> AgentRun:
    if _preserve_ambiguous_tools(
            run, reason="lease_expired_with_running_tool", commit=commit):
        return run
    if run.cancel_requested:
        return transition_run(
            run,
            "cancelled",
            event_type="run.cancelled_after_lease",
            commit=commit,
        )
    require_run_transition(run.status, "queued")
    run.status = "queued"
    run.worker_id = None
    run.lease_expires_at = None
    run.available_at = now
    append_event(run, "run.lease_expired", {
        "attempt": run.attempt,
        "error_class": "WorkerLeaseExpired",
    })
    if commit:
        db.session.commit()
    return run


def _enforce_worker_fence(
    run: AgentRun,
    worker_id: str,
    *,
    now=None,
) -> AgentRun:
    """Validate ownership under lock and enforce deadline/lease fencing."""
    if run.status != "running" or run.worker_id != worker_id:
        raise InvalidTransition("worker does not own a running lease")
    now = now or utcnow()
    if _as_utc(run.deadline_at) <= now:
        _terminalize_at_deadline(run)
        raise InvalidTransition("run deadline exceeded")
    if (run.lease_expires_at is None
            or _as_utc(run.lease_expires_at) <= now):
        _recover_expired_lease(run, now)
        raise InvalidTransition("worker lease expired")
    return run


def lock_owned_run(run_id: str, worker_id: str) -> AgentRun:
    """Lock and recheck the authoritative worker mutation fence."""
    run = (
        AgentRun.query
        .filter(AgentRun.id == run_id)
        .with_for_update()
        .first()
    )
    if run is None:
        raise LookupError("unknown run")
    return _enforce_worker_fence(run, worker_id)


def expire_overdue_run(run: AgentRun) -> AgentRun:
    """Fail one overdue queued or running run without a worker claim.

    Operator reads and SSE replay both call this path. The row lock makes it
    race safely with claims and heartbeats: exactly one side can terminalize
    the run at its hard deadline.
    """
    now = utcnow()
    if run.status not in ("queued", "running"):
        return run
    locked = (
        AgentRun.query
        .filter(AgentRun.id == run.id)
        .filter(AgentRun.status.in_(("queued", "running")))
        .filter(AgentRun.deadline_at <= now)
        .with_for_update()
        .first()
    )
    if locked is None:
        db.session.expire(run)
        return db.session.get(AgentRun, run.id) or run
    return _terminalize_at_deadline(locked)


def expire_overdue_runs(limit: int = 100) -> int:
    """Bounded control-plane sweep for runs stranded without a worker.

    Readiness polls invoke this independently of clients and workers. Locks and
    the nonterminal-state filter make repeated or concurrent sweeps idempotent.
    """
    now = utcnow()
    candidate_ids = [row[0] for row in (
        db.session.query(AgentRun.id)
        .filter(AgentRun.status.in_(("queued", "running")))
        .filter(AgentRun.deadline_at <= now)
        .order_by(AgentRun.deadline_at.asc())
        .limit(limit)
        .all()
    )]
    expired = 0
    for run_id in candidate_ids:
        run = (
            AgentRun.query
            .filter(AgentRun.id == run_id)
            .filter(AgentRun.status.in_(("queued", "running")))
            .filter(AgentRun.deadline_at <= now)
            .with_for_update(skip_locked=True)
            .first()
        )
        if run is None:
            continue
        _terminalize_at_deadline(run, commit=False)
        expired += 1
    db.session.commit()
    return expired


def worker_availability(max_age_seconds: int = 30) -> dict:
    """Summarize workers that recently completed a queue transaction."""
    now = utcnow()
    cutoff = now - timedelta(seconds=max_age_seconds)
    active = AgentWorkerPresence.query.filter(
        AgentWorkerPresence.last_seen_at >= cutoff).count()
    latest = AgentWorkerPresence.query.order_by(
        AgentWorkerPresence.last_seen_at.desc()).first()
    return {
        "available": active > 0,
        "active_workers": active,
        "max_age_seconds": max_age_seconds,
        "latest_seen_at": (
            latest.last_seen_at.isoformat() if latest is not None else None),
    }


def _record_worker_presence(worker_id: str, now) -> None:
    presence = db.session.get(AgentWorkerPresence, worker_id)
    if presence is None:
        presence = AgentWorkerPresence(
            worker_id=worker_id, first_seen_at=now, last_seen_at=now)
        db.session.add(presence)
    else:
        presence.last_seen_at = now


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _worker_never_received(run: AgentRun) -> bool:
    """True when the queue holds no evidence the worker read its claim.

    Every write a worker makes to a run appends a durable event in the same
    transaction: `register_tool_call` commits `tool.registered` alongside the
    call row, tool transitions append `tool.<status>`, progress arrives as
    `agent.*`. So "nothing since `run.started`" is the whole question, and it
    covers a side effect in flight — the case that must never be handed to a
    second reader. A run that went quiet after doing something is ambiguous,
    and ambiguity belongs to lease recovery, which routes a running tool to
    `needs_reconciliation` (`_preserve_ambiguous_tools`). Only a run that did
    nothing at all is explained by a response its worker never received.

    Tool calls are therefore not queried separately: a check that cannot fail
    while `register_tool_call` holds is decoration, and the test for a tool in
    flight asserts that coupling rather than trusting it.

    This engine's own redelivery marker is not worker activity, so a second
    lost response is recoverable too.
    """
    started = (
        AgentRunEvent.query
        .filter_by(tenant_id=run.tenant_id, run_id=run.id,
                   event_type="run.started")
        .order_by(AgentRunEvent.id.desc())
        .first()
    )
    if started is None:
        return False
    reported = (
        AgentRunEvent.query
        .filter_by(tenant_id=run.tenant_id, run_id=run.id)
        .filter(AgentRunEvent.id > started.id)
        .filter(AgentRunEvent.event_type != "run.claim_redelivered")
        .first()
    )
    return reported is None


def _redeliver_own_claim(
    worker_id: str,
    now,
    lease_seconds: int,
) -> AgentRun | None:
    """Hand a live claim back to the worker that never received its response.

    A 502 after `claim_next` commits leaves the run `running` with `worker_id`
    set and nobody executing it (#374). The worker cannot tell that from "no
    work available", so it polls again — and until this path existed the run
    stayed stranded for the whole lease, which is a patient watching an empty
    chat stream for up to 60 seconds before the turn silently restarts as a
    second attempt.

    The claiming worker is the only safe recipient: `run_worker_pool` blocks
    its slot inside `process()`, so a claim arriving under a worker id that
    already owns a running run is proof that slot is not executing it.
    """
    owned = (
        AgentRun.query
        .filter(AgentRun.status == "running")
        .filter(AgentRun.worker_id == worker_id)
        .filter(AgentRun.lease_expires_at > now)
        .filter(AgentRun.deadline_at > now)
        .order_by(AgentRun.started_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if owned is None or not _worker_never_received(owned):
        return None

    # `attempt` is not incremented: the run has not been executed once. The
    # redelivery gets its own event instead, so a lost response stays as
    # countable as the lease expiry it replaces.
    owned.heartbeat_at = now
    owned.lease_expires_at = min(
        now + timedelta(seconds=lease_seconds),
        _as_utc(owned.deadline_at),
    )
    append_event(owned, "run.claim_redelivered", {
        "status": "running",
        "attempt": owned.attempt,
    })
    return owned


def claim_next(worker_id: str, lease_seconds: int = 60) -> AgentRun | None:
    """Recover expired leases and atomically claim the oldest queued run."""
    now = utcnow()

    # This record is committed only if the queue transaction below succeeds.
    # A live process that cannot reach or transact with the queue therefore
    # cannot keep readiness green merely by existing.
    _record_worker_presence(worker_id, now)

    # Asked before the sweeps below, so the lease and deadline clauses in
    # `_redeliver_own_claim` are the boundary rather than a restatement of one
    # the recovery pass has already applied. A caller holding a stranded claim
    # has work to do; the sweeps run on every other claim and on the readiness
    # poll, which the runbook makes independent of any worker.
    redelivered = _redeliver_own_claim(worker_id, now, lease_seconds)
    if redelivered is not None:
        db.session.commit()
        return redelivered

    expired = (
        AgentRun.query
        .filter(AgentRun.status == "running")
        .filter(AgentRun.lease_expires_at < now)
        .with_for_update(skip_locked=True)
        .all()
    )
    for run in expired:
        _recover_expired_lease(run, now, commit=False)

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
    run.lease_expires_at = min(
        now + timedelta(seconds=lease_seconds),
        _as_utc(run.deadline_at),
    )
    append_event(run, "run.started", {
        "status": "running",
        "attempt": run.attempt,
    })
    db.session.commit()
    return run


def heartbeat(run: AgentRun, worker_id: str, lease_seconds: int = 60) -> None:
    locked = (
        AgentRun.query
        .filter(AgentRun.id == run.id)
        .with_for_update()
        .first()
    )
    if locked is None:
        raise InvalidTransition("worker does not own a running lease")
    now = utcnow()
    _enforce_worker_fence(locked, worker_id, now=now)
    locked.heartbeat_at = now
    locked.lease_expires_at = min(
        now + timedelta(seconds=lease_seconds),
        _as_utc(locked.deadline_at),
    )
    _record_worker_presence(worker_id, now)
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


def transition_owned_run(
    run_id: str,
    worker_id: str,
    target: str,
    *,
    event_type: str | None = None,
    payload: dict | list | None = None,
    error_class: str | None = None,
    available_in_seconds: int = 0,
) -> AgentRun:
    """Apply a generic worker transition behind the authoritative fence."""
    run = lock_owned_run(run_id, worker_id)
    if target == "completed":
        raise InvalidTransition(
            "completed is only allowed through atomic finalization")
    require_run_transition(run.status, target)
    if _preserve_ambiguous_tools(
            run, reason=f"worker_{target}_with_running_tool"):
        return run
    if run.cancel_requested and target != "cancelled":
        return transition_run(
            run, "cancelled", event_type="run.cancelled",
            payload={"status": "cancelled"})
    return transition_run(
        run,
        target,
        event_type=event_type,
        payload=payload,
        error_class=error_class,
        available_in_seconds=available_in_seconds,
    )


def request_cancel(run: AgentRun) -> AgentRun:
    locked = (
        AgentRun.query
        .filter(AgentRun.id == run.id)
        .with_for_update()
        .first()
    )
    if locked is None:
        raise LookupError("unknown run")
    run = locked
    if run.status in TERMINAL_RUN_STATES:
        return run
    if run.status == "waiting_for_human" and AgentToolCall.query.filter_by(
            run_id=run.id, status="needs_reconciliation").first() is not None:
        if not run.cancel_requested:
            run.cancel_requested = True
            append_event(run, "run.cancel_requested", {"status": run.status})
            db.session.commit()
        return run
    if run.status in ("queued", "waiting_for_human"):
        return transition_run(run, "cancelled")
    run.cancel_requested = True
    append_event(run, "run.cancel_requested", {"status": run.status})
    db.session.commit()
    return run


def resume_run(run_id: str) -> AgentRun:
    """Requeue a human-waiting run unless provider truth is unresolved."""
    run = (
        AgentRun.query
        .filter(AgentRun.id == run_id)
        .with_for_update()
        .first()
    )
    if run is None:
        raise LookupError("unknown run")
    if AgentToolCall.query.filter_by(
            run_id=run.id, status="needs_reconciliation").first() is not None:
        raise InvalidTransition("run has a tool awaiting reconciliation")
    return transition_run(
        run, "queued", event_type="run.resumed",
        payload={"status": "queued"})


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
    worker_id: str,
    target: str,
    *,
    result: dict | list | str | None = None,
    outcome_ref: str | None = None,
    error_class: str | None = None,
) -> AgentToolCall:
    # Lock in the same order as deadline enforcement so a late tool outcome
    # cannot race past lease revocation and overwrite needs_reconciliation.
    locked_run = lock_owned_run(run.id, worker_id)
    locked_call = (
        AgentToolCall.query
        .filter(AgentToolCall.id == call.id)
        .with_for_update()
        .first()
    )
    if locked_call is None:
        raise LookupError("unknown run or tool call")
    run = locked_run
    call = locked_call
    if call.run_id != run.id or call.tenant_id != run.tenant_id:
        raise LookupError("tool call does not belong to run")
    if run.cancel_requested and target == "running":
        raise InvalidTransition(
            "run cancellation prevents starting a tool call")
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


def finalize_run(
    run_id: str,
    worker_id: str,
    *,
    text: str,
    checkpoint_id: str,
) -> tuple[AgentRun, ConversationMessage, bool]:
    """Atomically persist the assistant answer, event, and run completion."""
    run = (
        AgentRun.query
        .filter(AgentRun.id == run_id)
        .with_for_update()
        .first()
    )
    if run is None:
        raise LookupError("unknown run")

    request_id = f"run:{run.id}:assistant"
    prior = ConversationMessage.query.filter_by(
        tenant_id=run.tenant_id,
        conversation_id=run.conversation_id,
        request_id=request_id,
    ).first()
    if run.status == "completed":
        prior_text_events = AgentRunEvent.query.filter_by(
            run_id=run.id, event_type="agent.text").all()
        checkpoint_matches = any(
            (_load(event.payload_json) or {}).get("checkpoint_id")
            == checkpoint_id
            for event in prior_text_events
        )
        if (prior is not None and prior.role == "assistant"
                and prior.text == text and prior.agent_id == run.agent_id
                and prior.reply_to == run.message_id and checkpoint_matches):
            return run, prior, False
        raise InvalidTransition("completed run has conflicting assistant outcome")

    _enforce_worker_fence(run, worker_id)
    unresolved = AgentToolCall.query.filter(
        AgentToolCall.run_id == run.id,
        AgentToolCall.status.in_((
            "pending", "running", "needs_reconciliation")),
    ).first()
    if unresolved is not None:
        raise InvalidTransition("run has an unresolved tool call")

    if prior is not None:
        if (prior.role != "assistant" or prior.text != text
                or prior.agent_id != run.agent_id
                or prior.reply_to != run.message_id):
            raise InvalidTransition(
                "assistant request ID conflicts with persisted outcome")
        message = prior
    else:
        conversation = (
            Conversation.query
            .filter_by(tenant_id=run.tenant_id, id=run.conversation_id)
            .with_for_update()
            .first()
        )
        if conversation is None:
            raise LookupError("run conversation is missing")
        message = ConversationMessage(
            tenant_id=run.tenant_id,
            conversation_id=run.conversation_id,
            agent_id=run.agent_id,
            channel=run.surface,
            role="assistant",
            text=text,
            request_id=request_id,
            reply_to=run.message_id,
            metadata_json=_dump({"careagents_agent_id": run.agent_id}),
        )
        db.session.add(message)
        conversation.updated_at = utcnow()

    prior_text_events = AgentRunEvent.query.filter_by(
        run_id=run.id, event_type="agent.text").all()
    already_emitted = any(
        (_load(event.payload_json) or {}).get("checkpoint_id") == checkpoint_id
        for event in prior_text_events
    )
    if not already_emitted:
        append_event(run, "agent.text", {
            "checkpoint_id": checkpoint_id,
            "text": text,
        })
    transition_run(
        run,
        "completed",
        event_type="run.completed",
        payload={"status": "completed"},
        commit=False,
    )
    db.session.commit()
    return run, message, True


def reconcile_tool_call(
    run: AgentRun,
    call: AgentToolCall,
    target: str,
    *,
    evidence_ref: str,
    error_class: str | None = None,
) -> tuple[AgentToolCall, AgentRun, bool]:
    """Record provider truth for one ambiguous side effect, idempotently.

    This is intentionally separate from worker transitions: a reconciler may
    resolve ambiguity but can never put the call back into an executable state.
    """
    if target not in ("completed", "failed"):
        raise InvalidTransition(
            "reconciliation may only complete or fail a tool call")

    locked_run = (
        AgentRun.query
        .filter(AgentRun.id == run.id)
        .with_for_update()
        .first()
    )
    locked_call = (
        AgentToolCall.query
        .filter(AgentToolCall.id == call.id)
        .with_for_update()
        .first()
    )
    if locked_run is None or locked_call is None:
        raise LookupError("unknown run or tool call")
    run = locked_run
    call = locked_call
    if call.run_id != run.id or call.tenant_id != run.tenant_id:
        raise LookupError("tool call does not belong to run")

    prior_events = AgentRunEvent.query.filter_by(
        run_id=run.id, event_type="tool.reconciled").all()
    prior = next((
        event for event in prior_events
        if (_load(event.payload_json) or {}).get("tool_call_id") == call.id
    ), None)
    if prior is not None:
        if call.status == target and call.outcome_ref == evidence_ref:
            return call, run, False
        raise InvalidTransition("tool reconciliation conflicts with prior truth")

    if run.status != "waiting_for_human":
        raise InvalidTransition("run is not awaiting reconciliation")
    require_tool_transition(call.status, target)
    now = utcnow()
    call.status = target
    call.outcome_ref = evidence_ref
    call.error_class = (
        None if target == "completed"
        else (error_class or "ReconciledToolFailure")
    )
    # Reconciliation records only opaque, server-controlled evidence. It does
    # not accept or manufacture a tool result envelope that a resumed model
    # could mistake for the original provider response.
    call.result_json = None
    call.finished_at = now

    remaining = (
        AgentToolCall.query
        .filter_by(run_id=run.id, status="needs_reconciliation")
        .filter(AgentToolCall.id != call.id)
        .count()
    )
    if remaining == 0:
        if run.cancel_requested:
            run_target = "cancelled"
            run_error = None
        else:
            any_failed = AgentToolCall.query.filter_by(
                run_id=run.id, status="failed").count() > 0
            run_target = "failed"
            run_error = (
                "ReconciledToolFailure" if any_failed
                else "ReconciledOutcomeNoAssistant"
            )
        require_run_transition(run.status, run_target)
        run.status = run_target
        run.error_class = run_error
        run.finished_at = now
        run.worker_id = None
        run.lease_expires_at = None

    append_event(run, "tool.reconciled", {
        "tool_call_id": call.id,
        "tool_name": call.tool_name,
        "status": target,
        "evidence_ref": evidence_ref,
        "error_class": call.error_class,
        "run_status": run.status,
        "run_error_class": run.error_class,
    })
    db.session.commit()
    return call, run, True


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load(value: str | None):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
