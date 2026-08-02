"""Authenticated durable AgentRun queue-control API."""

from __future__ import annotations

import hmac
import os
import re

from flask import Blueprint, jsonify, request, session
from sqlalchemy.exc import SQLAlchemyError

from models import db
from r6.agent_runs.models import AgentRun, AgentRunEvent, AgentToolCall
from r6.agent_runs.service import (
    append_event,
    claim_next,
    create_run,
    expire_overdue_runs,
    expire_overdue_run,
    finalize_run,
    heartbeat,
    lock_owned_run,
    reconcile_tool_call,
    register_tool_call,
    request_cancel,
    resume_run,
    transition_owned_run,
    transition_tool_call,
    worker_availability,
)
from r6.agent_runs.state import InvalidTransition
from r6.command_center.models import ConversationMessage
from r6.read_auth import TENANT_SESSION_KEY
from r6.stepup import validate_step_up_token


agent_runs_blueprint = Blueprint(
    "agent_runs",
    __name__,
    url_prefix="/command-center/api/runs",
)

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ERROR_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_MAX_EVENT_PAYLOAD_BYTES = 256 * 1024


def _tenant_authorized(tenant_id: str) -> bool:
    if session.get(TENANT_SESSION_KEY) == tenant_id:
        return True
    token = request.headers.get("X-Step-Up-Token", "")
    return bool(token and validate_step_up_token(token, tenant_id)[0])


def _internal_authorized() -> bool:
    expected = os.environ.get("INTERNAL_TOKEN_MINT_SECRET", "")
    provided = request.headers.get("X-Internal-Secret", "")
    return bool(expected and hmac.compare_digest(provided, expected))


def _reconciler_authorized() -> bool:
    expected = os.environ.get("AGENT_RUN_RECONCILE_SECRET", "")
    provided = request.headers.get("X-Reconciliation-Secret", "")
    return bool(expected and hmac.compare_digest(provided, expected))


def _run_or_404(run_id: str) -> AgentRun | None:
    return db.session.get(AgentRun, run_id)


def _valid_payload_size(value) -> bool:
    import json
    try:
        return len(json.dumps(value, separators=(",", ":")).encode()) <= (
            _MAX_EVENT_PAYLOAD_BYTES)
    except (TypeError, ValueError):
        return False


@agent_runs_blueprint.post("")
def create_agent_run():
    body = request.get_json(silent=True) or {}
    tenant_id = body.get("tenant_id") or request.headers.get("X-Tenant-Id")
    message_id = body.get("message_id")
    if not isinstance(tenant_id, str) or not _ID.fullmatch(tenant_id):
        return jsonify({"error": "invalid tenant_id"}), 400
    if not isinstance(message_id, str) or not _ID.fullmatch(message_id):
        return jsonify({"error": "invalid message_id"}), 400
    if not _tenant_authorized(tenant_id):
        return jsonify({"error": "authentication required"}), 401
    try:
        deadline_seconds = int(body.get("deadline_seconds", 120))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid deadline_seconds"}), 400
    if not 5 <= deadline_seconds <= 3600:
        return jsonify({"error": "deadline_seconds must be 5-3600"}), 400
    try:
        run, created = create_run(
            tenant_id, message_id, deadline_seconds=deadline_seconds)
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    result = run.to_dict()
    result["idempotent_replay"] = not created
    return jsonify(result), 201 if created else 200


@agent_runs_blueprint.get("/<run_id>")
def get_agent_run(run_id: str):
    run = _run_or_404(run_id)
    if run is None:
        return jsonify({"error": "unknown run"}), 404
    if not _tenant_authorized(run.tenant_id):
        return jsonify({"error": "authentication required"}), 401
    run = expire_overdue_run(run)
    result = run.to_dict()
    result["tool_calls"] = [
        call.to_dict()
        for call in AgentToolCall.query.filter_by(run_id=run.id)
        .order_by(AgentToolCall.created_at.asc()).all()
    ]
    return jsonify(result)


@agent_runs_blueprint.get("/<run_id>/events")
def get_agent_run_events(run_id: str):
    run = _run_or_404(run_id)
    if run is None:
        return jsonify({"error": "unknown run"}), 404
    if not _tenant_authorized(run.tenant_id):
        return jsonify({"error": "authentication required"}), 401
    run = expire_overdue_run(run)
    try:
        after = max(0, int(request.args.get("after", "0")))
        limit = min(500, max(1, int(request.args.get("limit", "100"))))
    except ValueError:
        return jsonify({"error": "invalid cursor or limit"}), 400
    events = (
        AgentRunEvent.query
        .filter_by(tenant_id=run.tenant_id, run_id=run.id)
        .filter(AgentRunEvent.id > after)
        .order_by(AgentRunEvent.id.asc())
        .limit(limit)
        .all()
    )
    return jsonify({
        "run_id": run.id,
        "status": run.status,
        "events": [event.to_dict() for event in events],
        "next_cursor": events[-1].id if events else after,
    })


@agent_runs_blueprint.get("/workers/health")
def get_agent_worker_health():
    """Internal readiness based on recent successful queue transactions."""
    if not _internal_authorized():
        return jsonify({"error": "forbidden"}), 403
    try:
        max_age_seconds = int(request.args.get("max_age_seconds", "30"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid max_age_seconds"}), 400
    if not 5 <= max_age_seconds <= 300:
        return jsonify({"error": "max_age_seconds must be 5-300"}), 400
    try:
        expired_runs = expire_overdue_runs(limit=100)
        result = worker_availability(max_age_seconds)
    except SQLAlchemyError:
        db.session.rollback()
        return jsonify({
            "status": "unavailable",
            "available": False,
            "active_workers": 0,
            "queue": "error",
        }), 503
    result["expired_runs"] = expired_runs
    result["queue"] = "ok"
    result["status"] = "healthy" if result["available"] else "unavailable"
    return jsonify(result), (200 if result["available"] else 503)


@agent_runs_blueprint.post("/<run_id>/cancel")
def cancel_agent_run(run_id: str):
    run = _run_or_404(run_id)
    if run is None:
        return jsonify({"error": "unknown run"}), 404
    if not _tenant_authorized(run.tenant_id):
        return jsonify({"error": "authentication required"}), 401
    return jsonify(request_cancel(run).to_dict())


@agent_runs_blueprint.post("/<run_id>/resume")
def resume_agent_run(run_id: str):
    """Internal approval surface resumes one human-waiting run."""
    if not _internal_authorized():
        return jsonify({"error": "forbidden"}), 403
    try:
        run = resume_run(run_id)
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except InvalidTransition as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify(run.to_dict())


@agent_runs_blueprint.post("/claim")
def claim_agent_run():
    if not _internal_authorized():
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    worker_id = body.get("worker_id")
    if not isinstance(worker_id, str) or not _ID.fullmatch(worker_id):
        return jsonify({"error": "invalid worker_id"}), 400
    try:
        lease_seconds = int(body.get("lease_seconds", 60))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid lease_seconds"}), 400
    if not 10 <= lease_seconds <= 600:
        return jsonify({"error": "lease_seconds must be 10-600"}), 400
    run = claim_next(worker_id, lease_seconds)
    if run is None:
        return "", 204
    message = ConversationMessage.query.filter_by(
        tenant_id=run.tenant_id, id=run.message_id).first()
    result = run.to_dict()
    result["message"] = {
        "id": message.id,
        "role": message.role,
        "text": message.text,
    } if message else None
    return jsonify(result)


@agent_runs_blueprint.post("/<run_id>/heartbeat")
def heartbeat_agent_run(run_id: str):
    if not _internal_authorized():
        return jsonify({"error": "forbidden"}), 403
    run = _run_or_404(run_id)
    if run is None:
        return jsonify({"error": "unknown run"}), 404
    body = request.get_json(silent=True) or {}
    worker_id = str(body.get("worker_id") or "")
    try:
        lease_seconds = int(body.get("lease_seconds", 60))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid lease_seconds"}), 400
    if not 10 <= lease_seconds <= 600:
        return jsonify({"error": "lease_seconds must be 10-600"}), 400
    try:
        heartbeat(run, worker_id, lease_seconds)
    except InvalidTransition as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({
        "ok": True,
        "cancel_requested": bool(run.cancel_requested),
        "lease_expires_at": run.to_dict()["lease_expires_at"],
    })


@agent_runs_blueprint.post("/<run_id>/transition")
def transition_agent_run(run_id: str):
    if not _internal_authorized():
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    worker_id = str(body.get("worker_id") or "")
    target = body.get("status")
    event_type = body.get("event_type") or None
    if event_type is not None and (
            not isinstance(event_type, str)
            or not _EVENT_TYPE.fullmatch(event_type)):
        return jsonify({"error": "invalid event type"}), 400
    payload = body.get("payload")
    if payload is not None and not _valid_payload_size(payload):
        return jsonify({"error": "event payload is invalid or too large"}), 413
    error_class = body.get("error_class")
    if error_class is not None and (
            not isinstance(error_class, str)
            or not _ERROR_CLASS.fullmatch(error_class)):
        return jsonify({"error": "invalid error_class"}), 400
    try:
        available_in_seconds = int(body.get("available_in_seconds", 0))
        if not 0 <= available_in_seconds <= 3600:
            return jsonify({
                "error": "available_in_seconds must be 0-3600"}), 400
        run = transition_owned_run(
            run_id,
            worker_id,
            target,
            event_type=event_type,
            payload=payload,
            error_class=error_class,
            available_in_seconds=available_in_seconds,
        )
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except (TypeError, ValueError, InvalidTransition) as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify(run.to_dict())


@agent_runs_blueprint.post("/<run_id>/events")
def append_agent_run_event(run_id: str):
    if not _internal_authorized():
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    worker_id = str(body.get("worker_id") or "")
    event_type = body.get("type")
    if not isinstance(event_type, str) or not _EVENT_TYPE.fullmatch(event_type):
        return jsonify({"error": "invalid event type"}), 400
    payload = body.get("payload")
    if payload is not None and not _valid_payload_size(payload):
        return jsonify({"error": "event payload is invalid or too large"}), 413
    try:
        run = lock_owned_run(run_id, worker_id)
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except InvalidTransition as exc:
        return jsonify({"error": str(exc)}), 409
    event = append_event(run, event_type, payload)
    db.session.commit()
    return jsonify(event.to_dict()), 201


@agent_runs_blueprint.post("/<run_id>/tool-calls")
def create_agent_tool_call(run_id: str):
    if not _internal_authorized():
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    worker_id = str(body.get("worker_id") or "")
    provider_call_id = body.get("provider_call_id")
    tool_name = body.get("tool_name")
    arguments = body.get("arguments")
    if not isinstance(provider_call_id, str) or not _ID.fullmatch(
            provider_call_id):
        return jsonify({"error": "invalid provider_call_id"}), 400
    if not isinstance(tool_name, str) or not _ID.fullmatch(tool_name):
        return jsonify({"error": "invalid tool_name"}), 400
    if not isinstance(arguments, dict):
        return jsonify({"error": "arguments must be an object"}), 400
    if not _valid_payload_size(arguments):
        return jsonify({"error": "arguments are too large"}), 413
    try:
        run = lock_owned_run(run_id, worker_id)
        if run.cancel_requested:
            raise InvalidTransition(
                "run cancellation prevents new tool registration")
        call, created = register_tool_call(
            run, provider_call_id, tool_name, arguments)
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except InvalidTransition as exc:
        return jsonify({"error": str(exc)}), 409
    result = call.to_dict(include_payload=True)
    result["idempotent_replay"] = not created
    return jsonify(result), 201 if created else 200


@agent_runs_blueprint.post("/<run_id>/tool-calls/<call_id>/transition")
def transition_agent_tool_call(run_id: str, call_id: str):
    if not _internal_authorized():
        return jsonify({"error": "forbidden"}), 403
    run = _run_or_404(run_id)
    call = db.session.get(AgentToolCall, call_id)
    if run is None or call is None or call.run_id != run_id:
        return jsonify({"error": "unknown run or tool call"}), 404
    body = request.get_json(silent=True) or {}
    worker_id = str(body.get("worker_id") or "")
    error_class = body.get("error_class")
    if error_class is not None and (
            not isinstance(error_class, str)
            or not _ERROR_CLASS.fullmatch(error_class)):
        return jsonify({"error": "invalid error_class"}), 400
    if body.get("result") is not None and not _valid_payload_size(
            body.get("result")):
        return jsonify({"error": "result is invalid or too large"}), 413
    outcome_ref = body.get("outcome_ref")
    if outcome_ref is not None and (
            not isinstance(outcome_ref, str) or len(outcome_ref) > 256):
        return jsonify({"error": "invalid outcome_ref"}), 400
    try:
        call = transition_tool_call(
            run,
            call,
            worker_id,
            body.get("status"),
            result=body.get("result"),
            outcome_ref=outcome_ref,
            error_class=error_class,
        )
    except (LookupError, InvalidTransition) as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify(call.to_dict(include_payload=True))


@agent_runs_blueprint.post("/<run_id>/finalize")
def finalize_agent_run(run_id: str):
    """Atomically persist a final assistant answer and complete its run."""
    if not _internal_authorized():
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    worker_id = str(body.get("worker_id") or "")
    text = body.get("text")
    checkpoint_id = body.get("checkpoint_id")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "nonempty text is required"}), 400
    if not isinstance(checkpoint_id, str) or not _ID.fullmatch(checkpoint_id):
        return jsonify({"error": "valid checkpoint_id is required"}), 400
    if not _valid_payload_size({"text": text, "checkpoint_id": checkpoint_id}):
        return jsonify({"error": "assistant outcome is too large"}), 413
    try:
        run, message, changed = finalize_run(
            run_id,
            worker_id,
            text=text,
            checkpoint_id=checkpoint_id,
        )
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except InvalidTransition as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({
        "run": run.to_dict(),
        "message_id": message.id,
        "idempotent_replay": not changed,
    })


@agent_runs_blueprint.post("/<run_id>/tool-calls/<call_id>/reconcile")
def reconcile_agent_tool_call(run_id: str, call_id: str):
    """Record operator-verified truth for an ambiguous side effect."""
    if not _reconciler_authorized():
        return jsonify({"error": "forbidden"}), 403
    run = _run_or_404(run_id)
    call = db.session.get(AgentToolCall, call_id)
    if run is None or call is None or call.run_id != run_id:
        return jsonify({"error": "unknown run or tool call"}), 404
    body = request.get_json(silent=True) or {}
    target = body.get("status")
    if target not in ("completed", "failed"):
        return jsonify({
            "error": "status must be completed or failed",
        }), 400
    evidence_ref = body.get("evidence_ref")
    if (not isinstance(evidence_ref, str)
            or not _ID.fullmatch(evidence_ref)):
        return jsonify({"error": "valid evidence_ref is required"}), 400
    if body.get("result") is not None:
        return jsonify({"error": "reconciliation does not accept result data"}), 400
    error_class = body.get("error_class")
    if error_class is not None and (
            not isinstance(error_class, str)
            or not _ERROR_CLASS.fullmatch(error_class)):
        return jsonify({"error": "invalid error_class"}), 400
    try:
        call, run, changed = reconcile_tool_call(
            run,
            call,
            target,
            evidence_ref=evidence_ref,
            error_class=error_class,
        )
    except (LookupError, InvalidTransition) as exc:
        return jsonify({"error": str(exc)}), 409
    response = call.to_dict()
    response["run_status"] = run.status
    response["idempotent_replay"] = not changed
    return jsonify(response)
