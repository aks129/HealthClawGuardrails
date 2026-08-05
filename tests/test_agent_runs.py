"""Durable AgentRun queue, replay, state, lease, and tool idempotency."""

from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from models import db
from r6.agent_runs.models import (
    AgentRun,
    AgentRunEvent,
    AgentToolCall,
    AgentWorkerPresence,
    utcnow,
)
from r6.agent_runs.state import RUN_STATES
from r6.command_center.models import ConversationMessage


TENANT = "test-tenant"


@pytest.fixture
def internal_headers(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "run-worker-secret")
    return {"X-Internal-Secret": "run-worker-secret"}


@pytest.fixture
def reconcile_headers(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_RECONCILE_SECRET", "operator-secret")
    return {"X-Reconciliation-Secret": "operator-secret"}


def _message(client, auth_headers, *, text="hello", request_id="request-1"):
    response = client.post(
        "/command-center/api/conversations",
        headers=auth_headers,
        json={
            "tenant_id": TENANT,
            "conversation_id": "careagents:juniper",
            "agent_id": "juniper",
            "surface": "web",
            "request_id": request_id,
            "role": "user",
            "text": text,
        },
    )
    assert response.status_code == 201
    return response.get_json()


def _run(client, auth_headers, message_id):
    return client.post(
        "/command-center/api/runs",
        headers=auth_headers,
        json={"tenant_id": TENANT, "message_id": message_id},
    )


def _claim(client, internal_headers, worker="worker-1", lease=60):
    return client.post(
        "/command-center/api/runs/claim",
        headers=internal_headers,
        json={"worker_id": worker, "lease_seconds": lease},
    )


def test_run_creation_is_idempotent_and_events_replay_from_cursor(
        client, auth_headers):
    assert RUN_STATES == {
        "queued", "running", "waiting_for_human",
        "completed", "failed", "cancelled",
    }
    message = _message(client, auth_headers)

    first = _run(client, auth_headers, message["id"])
    replay = _run(client, auth_headers, message["id"])

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.get_json()["id"] == first.get_json()["id"]
    assert replay.get_json()["idempotent_replay"] is True

    run_id = first.get_json()["id"]
    page = client.get(
        f"/command-center/api/runs/{run_id}/events",
        headers=auth_headers,
    ).get_json()
    assert [event["type"] for event in page["events"]] == ["run.queued"]
    cursor = page["next_cursor"]
    empty = client.get(
        f"/command-center/api/runs/{run_id}/events",
        headers=auth_headers,
        query_string={"after": cursor},
    ).get_json()
    assert empty == {
        "run_id": run_id,
        "status": "queued",
        "events": [],
        "next_cursor": cursor,
    }


def test_worker_claim_is_secret_gated_and_claims_once(
        client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]

    assert _claim(client, {}).status_code == 403
    claimed = _claim(client, internal_headers)
    assert claimed.status_code == 200
    body = claimed.get_json()
    assert body["id"] == run_id
    assert body["status"] == "running"
    assert body["attempt"] == 1
    assert body["message"] == {
        "id": message["id"], "role": "user", "text": "hello"}
    assert _claim(client, internal_headers, worker="worker-2").status_code == 204


def test_worker_readiness_requires_recent_successful_queue_access(
        app, client, internal_headers):
    endpoint = "/command-center/api/runs/workers/health"
    assert client.get(endpoint).status_code == 403
    unavailable = client.get(endpoint, headers=internal_headers)
    assert unavailable.status_code == 503
    assert unavailable.get_json()["active_workers"] == 0

    # A successful empty claim is still a real queue transaction and proves
    # that an idle worker can reach the control plane.
    assert _claim(client, internal_headers, worker="idle-worker").status_code == 204
    healthy = client.get(endpoint, headers=internal_headers)
    assert healthy.status_code == 200
    assert healthy.get_json()["active_workers"] == 1

    with app.app_context():
        presence = db.session.get(AgentWorkerPresence, "idle-worker")
        presence.last_seen_at = utcnow() - timedelta(seconds=31)
        db.session.commit()
    stale = client.get(endpoint, headers=internal_headers)
    assert stale.status_code == 503
    assert stale.get_json()["status"] == "unavailable"

    assert _claim(client, internal_headers, worker="idle-worker").status_code == 204
    assert client.get(endpoint, headers=internal_headers).status_code == 200


def test_owned_run_heartbeat_refreshes_presence_but_wrong_worker_cannot(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(
        client, internal_headers, worker="active-worker").status_code == 200
    with app.app_context():
        presence = db.session.get(AgentWorkerPresence, "active-worker")
        presence.last_seen_at = utcnow() - timedelta(seconds=31)
        db.session.commit()

    wrong = client.post(
        f"/command-center/api/runs/{run_id}/heartbeat",
        headers=internal_headers,
        json={"worker_id": "wrong-worker", "lease_seconds": 60},
    )
    assert wrong.status_code == 409
    with app.app_context():
        assert db.session.get(AgentWorkerPresence, "wrong-worker") is None
    assert client.get(
        "/command-center/api/runs/workers/health",
        headers=internal_headers).status_code == 503

    valid = client.post(
        f"/command-center/api/runs/{run_id}/heartbeat",
        headers=internal_headers,
        json={"worker_id": "active-worker", "lease_seconds": 60},
    )
    assert valid.status_code == 200
    health = client.get(
        "/command-center/api/runs/workers/health", headers=internal_headers)
    assert health.status_code == 200
    assert health.get_json()["active_workers"] == 1


def test_owned_heartbeat_terminalizes_run_at_hard_deadline_once(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(
        client, internal_headers, worker="deadline-worker").status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    endpoint = f"/command-center/api/runs/{run_id}/heartbeat"
    payload = {"worker_id": "deadline-worker", "lease_seconds": 60}
    assert client.post(
        endpoint, headers=internal_headers, json=payload).status_code == 409
    assert client.post(
        endpoint, headers=internal_headers, json=payload).status_code == 409

    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        events = AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.deadline_exceeded").all()
        assert run.status == "failed"
        assert run.error_class == "RunDeadlineExceeded"
        assert run.worker_id is None and run.lease_expires_at is None
        assert len(events) == 1


def test_worker_readiness_sweeps_expired_runs_without_reading_them(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    # Exercise only independent readiness; neither a worker nor a run-specific
    # GET is involved in the deadline transition.
    first = client.get(
        "/command-center/api/runs/workers/health", headers=internal_headers)
    second = client.get(
        "/command-center/api/runs/workers/health", headers=internal_headers)

    assert first.status_code == second.status_code == 503
    assert first.get_json()["expired_runs"] == 1
    assert second.get_json()["expired_runs"] == 0
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        events = AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.deadline_exceeded").all()
        assert run.status == "failed"
        assert run.error_class == "RunDeadlineExceeded"
        assert len(events) == 1


def test_worker_readiness_sweeps_running_run_after_worker_crash(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(
        client, internal_headers, worker="crashed-worker").status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    # No worker heartbeat, claim, or run-specific read participates. The
    # independent readiness sweep owns hard-deadline enforcement here.
    first = client.get(
        "/command-center/api/runs/workers/health", headers=internal_headers)
    second = client.get(
        "/command-center/api/runs/workers/health", headers=internal_headers)

    assert first.get_json()["expired_runs"] == 1
    assert second.get_json()["expired_runs"] == 0
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        events = AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.deadline_exceeded").all()
        assert run.status == "failed"
        assert run.error_class == "RunDeadlineExceeded"
        assert run.worker_id is None and run.lease_expires_at is None
        assert len(events) == 1


def test_event_replay_expires_running_run_after_worker_crash(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(
        client, internal_headers, worker="crashed-worker").status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    endpoint = f"/command-center/api/runs/{run_id}/events"
    replay = client.get(endpoint, headers=auth_headers)
    repeated = client.get(endpoint, headers=auth_headers)

    assert replay.status_code == repeated.status_code == 200
    assert replay.get_json()["status"] == "failed"
    assert repeated.get_json()["status"] == "failed"
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        events = AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.deadline_exceeded").all()
        assert run.status == "failed"
        assert run.error_class == "RunDeadlineExceeded"
        assert run.worker_id is None and run.lease_expires_at is None
        assert len(events) == 1


def test_deadline_sweep_preserves_running_cancellation_request(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(
        client, internal_headers, worker="crashed-worker").status_code == 200
    assert client.post(
        f"/command-center/api/runs/{run_id}/cancel",
        headers=auth_headers,
    ).status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    client.get(
        "/command-center/api/runs/workers/health", headers=internal_headers)

    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        cancelled = AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.cancelled_at_deadline").all()
        deadline_failures = AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.deadline_exceeded").all()
        assert run.status == "cancelled"
        assert run.error_class is None
        assert run.worker_id is None and run.lease_expires_at is None
        assert len(cancelled) == 1
        assert deadline_failures == []


@pytest.mark.parametrize("trigger", ["worker-health", "event-replay"])
def test_deadline_preserves_ambiguous_running_tool_for_reconciliation(
        trigger, app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(
        client, internal_headers, worker="lost-worker").status_code == 200
    created = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "lost-worker",
            "provider_call_id": "possibly-completed-side-effect",
            "tool_name": "book_appointment",
            "arguments": {"slot": "slot-1"},
        },
    ).get_json()
    call_id = created["id"]
    assert client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={"worker_id": "lost-worker", "status": "running"},
    ).status_code == 200
    if trigger == "worker-health":
        # Cancellation cannot assert that an already-started side effect did
        # not happen; ambiguity must take precedence at the deadline.
        assert client.post(
            f"/command-center/api/runs/{run_id}/cancel",
            headers=auth_headers,
        ).status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    if trigger == "worker-health":
        endpoint = "/command-center/api/runs/workers/health"
        first = client.get(endpoint, headers=internal_headers)
        second = client.get(endpoint, headers=internal_headers)
        assert first.get_json()["expired_runs"] == 1
        assert second.get_json()["expired_runs"] == 0
    else:
        endpoint = f"/command-center/api/runs/{run_id}/events"
        first = client.get(endpoint, headers=auth_headers)
        second = client.get(endpoint, headers=auth_headers)
        assert first.get_json()["status"] == "waiting_for_human"
        assert second.get_json()["status"] == "waiting_for_human"
        cancellation = client.post(
            f"/command-center/api/runs/{run_id}/cancel",
            headers=auth_headers,
        ).get_json()
        assert cancellation["status"] == "waiting_for_human"
        assert cancellation["cancel_requested"] is True

    assert client.post(
        f"/command-center/api/runs/{run_id}/resume",
        headers=internal_headers,
    ).status_code == 409

    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        call = db.session.get(AgentToolCall, call_id)
        reconciliation = AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.needs_reconciliation").all()
        deadline_failures = AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.deadline_exceeded").all()
        assert run.status == "waiting_for_human"
        assert run.error_class == "AmbiguousToolOutcome"
        assert run.worker_id is None and run.lease_expires_at is None
        assert call.status == "needs_reconciliation"
        assert call.error_class == "AmbiguousToolOutcome"
        assert call.attempt == 1
        assert len(reconciliation) == 1
        assert deadline_failures == []

    # Recovery claims cannot rerun a call whose outcome is ambiguous.
    assert _claim(
        client, internal_headers, worker="replacement-worker").status_code == 204


@pytest.mark.parametrize("race", ["cancel-requested", "deadline-passed"])
def test_claim_recovery_preserves_ambiguous_tool_before_other_transitions(
        race, app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(
        client, internal_headers, worker="lost-worker").status_code == 200
    created = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "lost-worker",
            "provider_call_id": f"lease-race-{race}",
            "tool_name": "book_appointment",
            "arguments": {"slot": "slot-1"},
        },
    ).get_json()
    call_id = created["id"]
    assert client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={"worker_id": "lost-worker", "status": "running"},
    ).status_code == 200
    if race == "cancel-requested":
        assert client.post(
            f"/command-center/api/runs/{run_id}/cancel",
            headers=auth_headers,
        ).status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.lease_expires_at = utcnow() - timedelta(seconds=1)
        if race == "deadline-passed":
            run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    assert _claim(
        client, internal_headers, worker="replacement-worker").status_code == 204

    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        call = db.session.get(AgentToolCall, call_id)
        reconciliation = AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.needs_reconciliation").all()
        assert run.status == "waiting_for_human"
        assert run.error_class == "AmbiguousToolOutcome"
        assert run.worker_id is None and run.lease_expires_at is None
        assert call.status == "needs_reconciliation"
        assert call.attempt == 1
        assert len(reconciliation) == 1
        assert not AgentRunEvent.query.filter(
            AgentRunEvent.run_id == run_id,
            AgentRunEvent.event_type.in_((
                "run.failed", "run.deadline_exceeded", "run.cancelled",
                "run.cancelled_after_lease",
            )),
        ).all()


@pytest.mark.parametrize("truth", ["completed", "failed"])
def test_authorized_reconciler_resolves_ambiguous_tool_exactly_once(
        truth, app, client, auth_headers, internal_headers,
        reconcile_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(
        client, internal_headers, worker="lost-worker").status_code == 200
    created = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "lost-worker",
            "provider_call_id": f"reconcile-{truth}",
            "tool_name": "book_appointment",
            "arguments": {"slot": "slot-1"},
        },
    ).get_json()
    call_id = created["id"]
    transition_endpoint = (
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition")
    assert client.post(
        transition_endpoint,
        headers=internal_headers,
        json={"worker_id": "lost-worker", "status": "running"},
    ).status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert client.get(
        f"/command-center/api/runs/{run_id}/events",
        headers=auth_headers,
    ).get_json()["status"] == "waiting_for_human"

    # The former worker cannot supply a late result after losing its lease.
    assert client.post(
        transition_endpoint,
        headers=internal_headers,
        json={"worker_id": "lost-worker", "status": truth},
    ).status_code == 409

    reconcile_endpoint = (
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/reconcile")
    payload = {
        "status": truth,
        "evidence_ref": f"provider:truth:{truth}",
    }
    if truth == "failed":
        payload["error_class"] = "ProviderRejected"
    # Neither an unauthenticated caller nor an authenticated tenant can assert
    # provider truth. Reconciliation uses a separate operator credential.
    assert client.post(reconcile_endpoint, json=payload).status_code == 403
    assert client.post(
        reconcile_endpoint,
        headers=auth_headers,
        json=payload,
    ).status_code == 403
    assert client.post(
        reconcile_endpoint,
        headers=internal_headers,
        json=payload,
    ).status_code == 403
    assert client.post(
        reconcile_endpoint,
        headers=reconcile_headers,
        json={**payload, "status": "running"},
    ).status_code == 400
    assert client.post(
        reconcile_endpoint,
        headers=reconcile_headers,
        json={**payload, "result": {"patient_name": "must-not-enter"}},
    ).status_code == 400

    resolved = client.post(
        reconcile_endpoint, headers=reconcile_headers, json=payload)
    replay = client.post(
        reconcile_endpoint, headers=reconcile_headers, json=payload)
    conflict = client.post(
        reconcile_endpoint,
        headers=reconcile_headers,
        json={**payload, "evidence_ref": "provider:different:truth"},
    )

    assert resolved.status_code == replay.status_code == 200
    assert resolved.get_json()["idempotent_replay"] is False
    assert replay.get_json()["idempotent_replay"] is True
    assert resolved.get_json()["run_status"] == "failed"
    assert "input" not in resolved.get_json()
    assert "result" not in resolved.get_json()
    assert conflict.status_code == 409
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        call = db.session.get(AgentToolCall, call_id)
        events = AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="tool.reconciled").all()
        assert run.status == "failed"
        assert run.error_class == (
            "ReconciledToolFailure" if truth == "failed"
            else "ReconciledOutcomeNoAssistant")
        assert call.status == truth
        assert call.outcome_ref == f"provider:truth:{truth}"
        assert call.result_json is None
        assert len(events) == 1


def test_worker_readiness_fails_closed_on_queue_database_error(
        client, internal_headers, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    def fail_sweep(*, limit):
        raise SQLAlchemyError("queue unavailable")

    monkeypatch.setattr(
        "r6.agent_runs.routes.expire_overdue_runs", fail_sweep)
    response = client.get(
        "/command-center/api/runs/workers/health", headers=internal_headers)

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "unavailable",
        "available": False,
        "active_workers": 0,
        "queue": "error",
    }


def test_run_transition_and_cancel_state_machine(
        client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    _claim(client, internal_headers)

    invalid = client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "queued"},
    )
    assert invalid.status_code == 200  # running -> queued is an explicit retry
    impossible = client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "completed"},
    )
    assert impossible.status_code == 409  # worker released its lease

    _claim(client, internal_headers)
    cancel = client.post(
        f"/command-center/api/runs/{run_id}/cancel", headers=auth_headers)
    assert cancel.status_code == 200
    assert cancel.get_json()["cancel_requested"] is True
    heartbeat = client.post(
        f"/command-center/api/runs/{run_id}/heartbeat",
        headers=internal_headers,
        json={"worker_id": "worker-1", "lease_seconds": 60},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.get_json()["cancel_requested"] is True
    assert client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "completed"},
    ).status_code == 409
    cancelled = client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "cancelled"},
    )
    assert cancelled.status_code == 200
    assert cancelled.get_json()["status"] == "cancelled"
    assert client.post(
        f"/command-center/api/runs/{run_id}/cancel",
        headers=auth_headers,
    ).get_json()["status"] == "cancelled"


def test_waiting_run_requeues_without_replaying_completed_tool(
        client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    _claim(client, internal_headers)
    call = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "provider_call_id": "tool-call-1",
            "tool_name": "start_intake_form",
            "arguments": {},
        },
    )
    assert call.status_code == 201
    call_id = call.get_json()["id"]
    assert client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "running"},
    ).status_code == 200
    assert client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "status": "completed",
            "result": {"status": "awaiting_confirmation"},
            "outcome_ref": "action-1",
        },
    ).status_code == 200

    waiting = client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "waiting_for_human"},
    )
    assert waiting.status_code == 200
    resumed = client.post(
        f"/command-center/api/runs/{run_id}/resume",
        headers=internal_headers,
    )
    assert resumed.status_code == 200
    assert resumed.get_json()["status"] == "queued"
    claimed = _claim(client, internal_headers, worker="worker-2")
    assert claimed.status_code == 200

    replay = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "worker-2",
            "provider_call_id": "tool-call-1",
            "tool_name": "start_intake_form",
            "arguments": {},
        },
    )
    assert replay.status_code == 200
    assert replay.get_json()["id"] == call_id
    assert replay.get_json()["status"] == "completed"
    assert replay.get_json()["idempotent_replay"] is True

    conflict = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "worker-2",
            "provider_call_id": "tool-call-1",
            "tool_name": "start_intake_form",
            "arguments": {"changed": True},
        },
    )
    assert conflict.status_code == 409


def test_ambiguous_running_tool_requires_reconciliation_before_resolution(
        client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    _claim(client, internal_headers)
    created = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "provider_call_id": "side-effect-1",
            "tool_name": "book_appointment",
            "arguments": {"slot": "slot-1"},
        },
    ).get_json()
    call_id = created["id"]
    assert client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "running"},
    ).status_code == 200

    ambiguous = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "status": "needs_reconciliation",
            "error_class": "AmbiguousToolOutcome",
        },
    )
    assert ambiguous.status_code == 200
    assert ambiguous.get_json()["status"] == "needs_reconciliation"

    # Re-execution is intentionally not a legal transition. An operator or
    # downstream reconciliation job must resolve the original side effect.
    assert client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "running"},
    ).status_code == 409
    resolved = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "status": "completed",
            "result": {"appointment_id": "appointment-1"},
            "outcome_ref": "appointment-1",
        },
    )
    assert resolved.status_code == 200
    assert resolved.get_json()["status"] == "completed"


def test_expired_worker_lease_is_recovered_for_another_worker(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    _claim(client, internal_headers)
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    recovered = _claim(client, internal_headers, worker="worker-2")

    assert recovered.status_code == 200
    assert recovered.get_json()["worker_id"] == "worker-2"
    assert recovered.get_json()["attempt"] == 2
    with app.app_context():
        kinds = [event.event_type for event in AgentRunEvent.query.filter_by(
            run_id=run_id).order_by(AgentRunEvent.id.asc()).all()]
    assert kinds == [
        "run.queued", "run.started", "run.lease_expired", "run.started"]


def test_lost_claim_response_is_redelivered_within_the_lease(
        app, client, auth_headers, internal_headers):
    """A claim that commits and never reaches its worker is handed back (#374).

    The transport failure lands *after* `claim_next` commits — a 502, a deploy
    window, a dropped connection — so the run sits `running` with a live lease
    and nobody executing it. The worker's next poll has to answer with that
    run. Answering 204 is what makes the patient's chat hang for the whole
    lease with nothing on the SSE stream, and then resume as a second attempt.

    MUTATION: delete the `_redeliver_own_claim` call in `claim_next`
    (r6/agent_runs/service.py). The second claim answers 204 again.
    """
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]

    # Nothing the worker can observe separates this from "no work available":
    # the response it would have read never arrived.
    assert _claim(client, internal_headers).status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.lease_expires_at = utcnow() + timedelta(seconds=2)
        db.session.commit()

    redelivered = _claim(client, internal_headers)

    assert redelivered.status_code == 200
    body = redelivered.get_json()
    assert body["id"] == run_id
    assert body["worker_id"] == "worker-1"
    assert body["message"] == {
        "id": message["id"], "role": "user", "text": "hello"}
    # A response nobody read is not an execution. Counting it as one produces
    # the false `attempt > 1` this defect is measured by.
    assert body["attempt"] == 1
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        lease = run.lease_expires_at
        if lease.tzinfo is None:
            lease = lease.replace(tzinfo=timezone.utc)
        # The lease is re-armed for the worker that is only now starting.
        assert lease > utcnow() + timedelta(seconds=30)
        kinds = [event.event_type for event in AgentRunEvent.query.filter_by(
            run_id=run_id).order_by(AgentRunEvent.id.asc()).all()]
    # The redelivery is named rather than silent: a recovery nobody can count
    # is the same blind spot as the hang it replaces.
    assert kinds == ["run.queued", "run.started", "run.claim_redelivered"]


def test_repeated_lost_responses_are_each_redelivered(
        client, auth_headers, internal_headers):
    """One deploy window loses several responses in a row, not just the first.

    MUTATION: drop the `run.claim_redelivered` exclusion from
    `_worker_never_received` (r6/agent_runs/service.py). The engine then reads
    its own marker as worker activity and the second redelivery answers 204.
    """
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200

    assert _claim(client, internal_headers).status_code == 200
    again = _claim(client, internal_headers)

    assert again.status_code == 200
    assert again.get_json()["id"] == run_id
    assert again.get_json()["attempt"] == 1


def test_a_run_with_a_side_effect_in_flight_is_never_redelivered(
        app, client, auth_headers, internal_headers):
    """Redelivery must never become a second way to run one tool.

    A registered tool call proves the worker read its claim response, so a
    lost response no longer explains the silence. That ambiguity belongs to
    lease recovery, which routes a running tool to `needs_reconciliation`
    instead of handing the side effect to anyone.

    `_worker_never_received` reads this off the event log rather than the tool
    table, so this test also asserts the coupling it depends on: a tool call
    that left no event would make the guard blind to exactly this case.

    MUTATION: drop the event clause from `_worker_never_received`. The claim
    hands back a run whose side effect may already have happened.
    """
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200
    call_id = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "provider_call_id": "possibly-completed-side-effect",
            "tool_name": "book_appointment",
            "arguments": {"slot": "slot-1"},
        },
    ).get_json()["id"]
    assert client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "running"},
    ).status_code == 200

    assert _claim(client, internal_headers).status_code == 204

    with app.app_context():
        # The coupling the guard leans on: a side effect always leaves a
        # durable trace, so reading the event log sees the tool table.
        assert AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="tool.registered").count() == 1
        run = db.session.get(AgentRun, run_id)
        run.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert _claim(
        client, internal_headers, worker="worker-2").status_code == 204
    with app.app_context():
        assert db.session.get(AgentRun, run_id).status == "waiting_for_human"
        assert db.session.get(AgentToolCall, call_id).status == (
            "needs_reconciliation")


def test_a_run_that_reported_progress_is_never_redelivered(
        client, auth_headers, internal_headers):
    """A durable event is the worker saying it read the claim response.

    Without this the claim loop could hand a run to a second reader while the
    first is still mid-inference — two executions of one patient turn.

    MUTATION: drop the event clause from `_worker_never_received`. The claim
    answers 200 for a run whose worker is already working on it.
    """
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200
    assert client.post(
        f"/command-center/api/runs/{run_id}/events",
        headers=internal_headers,
        json={"worker_id": "worker-1", "type": "agent.tool",
              "payload": {"name": "search_records"}},
    ).status_code == 201

    assert _claim(client, internal_headers).status_code == 204


def test_an_expired_lease_is_recovered_rather_than_redelivered(
        app, client, auth_headers, internal_headers):
    """Redelivery covers a live lease only. A dead one is still a real retry.

    MUTATION: drop the `lease_expires_at > now` clause from
    `_redeliver_own_claim`. The expired lease is then handed back as attempt 1
    and `run.lease_expired` stops being emitted — the count this defect is
    measured by would fall for the wrong reason.
    """
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    recovered = _claim(client, internal_headers)

    assert recovered.status_code == 200
    assert recovered.get_json()["attempt"] == 2
    with app.app_context():
        kinds = [event.event_type for event in AgentRunEvent.query.filter_by(
            run_id=run_id).order_by(AgentRunEvent.id.asc()).all()]
    assert kinds == [
        "run.queued", "run.started", "run.lease_expired", "run.started"]


def test_a_run_past_its_deadline_is_never_redelivered(
        app, client, auth_headers, internal_headers):
    """A claim never hands out a run whose deadline has already passed.

    MUTATION: drop the `deadline_at > now` clause from
    `_redeliver_own_claim`. The claim answers 200 with a run that can only
    fail, instead of leaving it to the deadline sweep.
    """
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    assert _claim(client, internal_headers).status_code == 204

    swept = client.get(
        "/command-center/api/runs/workers/health", headers=internal_headers)
    assert swept.get_json()["expired_runs"] == 1
    with app.app_context():
        assert db.session.get(AgentRun, run_id).status == "failed"


def test_run_detail_redacts_tool_payloads_from_operator_projection(
        client, auth_headers, internal_headers):
    message = _message(client, auth_headers, text="private health question")
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    _claim(client, internal_headers)
    created = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "provider_call_id": "private-call",
            "tool_name": "search_records",
            "arguments": {"resource_type": "Condition"},
        },
    ).get_json()

    detail = client.get(
        f"/command-center/api/runs/{run_id}", headers=auth_headers).get_json()

    assert "message" not in detail
    assert "input" not in detail["tool_calls"][0]
    assert "result" not in detail["tool_calls"][0]
    assert detail["tool_calls"][0]["input_hash"] == created["input_hash"]


def test_run_access_is_tenant_bound(client, auth_headers):
    from r6.stepup import generate_step_up_token

    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    foreign = {
        "X-Tenant-Id": "other-tenant",
        "X-Step-Up-Token": generate_step_up_token("other-tenant"),
    }

    assert client.get(
        f"/command-center/api/runs/{run_id}", headers=foreign
    ).status_code == 401
    assert client.get(
        f"/command-center/api/runs/{run_id}/events", headers=foreign
    ).status_code == 401
    assert client.post(
        f"/command-center/api/runs/{run_id}/cancel", headers=foreign
    ).status_code == 401


def test_event_and_tool_payloads_are_bounded(
        client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    _claim(client, internal_headers)
    huge = "x" * (256 * 1024 + 1)

    assert client.post(
        f"/command-center/api/runs/{run_id}/events",
        headers=internal_headers,
        json={"worker_id": "worker-1", "type": "agent.text",
              "payload": {"text": huge}},
    ).status_code == 413
    assert client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={"worker_id": "worker-1", "provider_call_id": "call-large",
              "tool_name": "search_records", "arguments": {"q": huge}},
    ).status_code == 413


def test_finalize_atomically_persists_assistant_message_and_completion(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200
    endpoint = f"/command-center/api/runs/{run_id}/finalize"
    payload = {
        "worker_id": "worker-1",
        "checkpoint_id": "round-1",
        "text": "Your appointment brief is ready.",
    }

    direct = client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "completed"},
    )
    first = client.post(endpoint, headers=internal_headers, json=payload)
    replay = client.post(endpoint, headers=internal_headers, json=payload)
    conflict = client.post(
        endpoint,
        headers=internal_headers,
        json={**payload, "text": "conflicting late answer"},
    )
    checkpoint_conflict = client.post(
        endpoint,
        headers=internal_headers,
        json={**payload, "checkpoint_id": "different-final"},
    )

    assert direct.status_code == 409
    assert first.status_code == replay.status_code == 200
    assert first.get_json()["idempotent_replay"] is False
    assert replay.get_json()["idempotent_replay"] is True
    assert conflict.status_code == 409
    assert checkpoint_conflict.status_code == 409
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        assistant = ConversationMessage.query.filter_by(
            tenant_id=TENANT,
            conversation_id=run.conversation_id,
            request_id=f"run:{run_id}:assistant",
        ).all()
        assert run.status == "completed"
        assert len(assistant) == 1
        assert assistant[0].text == payload["text"]
        assert AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="agent.text").count() == 1
        assert AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.completed").count() == 1


def test_generic_worker_failure_preserves_running_tool_ambiguity(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200
    created = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "provider_call_id": "exception-side-effect",
            "tool_name": "book_appointment",
            "arguments": {},
        },
    ).get_json()
    call_id = created["id"]
    assert client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "running"},
    ).status_code == 200

    failed = client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "status": "failed",
            "event_type": "run.failed",
            "error_class": "UnexpectedWorkerError",
        },
    )

    assert failed.status_code == 200
    assert failed.get_json()["status"] == "waiting_for_human"
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        call = db.session.get(AgentToolCall, call_id)
        assert run.status == "waiting_for_human"
        assert run.error_class == "AmbiguousToolOutcome"
        assert call.status == "needs_reconciliation"
        assert AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.failed").count() == 0
        assert AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.needs_reconciliation").count() == 1


def test_cancellation_prevents_new_tool_registration(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200
    assert client.post(
        f"/command-center/api/runs/{run_id}/cancel",
        headers=auth_headers,
    ).status_code == 200

    response = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "provider_call_id": "cancelled-new-tool",
            "tool_name": "book_appointment",
            "arguments": {},
        },
    )

    assert response.status_code == 409
    with app.app_context():
        assert AgentToolCall.query.filter_by(run_id=run_id).count() == 0


def test_cancellation_prevents_pending_tool_from_starting(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200
    created = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "provider_call_id": "cancelled-pending-tool",
            "tool_name": "book_appointment",
            "arguments": {},
        },
    ).get_json()
    assert client.post(
        f"/command-center/api/runs/{run_id}/cancel",
        headers=auth_headers,
    ).status_code == 200

    start = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{created['id']}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "running"},
    )

    assert start.status_code == 409
    cancelled = client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "status": "failed",
            "error_class": "ToolStartRejected",
        },
    )
    assert cancelled.status_code == 200
    assert cancelled.get_json()["status"] == "cancelled"
    with app.app_context():
        assert db.session.get(AgentToolCall, created["id"]).status == "pending"


def test_cancellation_allows_started_tool_outcome_but_no_next_tool(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200
    created = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "provider_call_id": "already-started-tool",
            "tool_name": "book_appointment",
            "arguments": {},
        },
    ).get_json()
    transition_endpoint = (
        f"/command-center/api/runs/{run_id}/tool-calls/{created['id']}/transition")
    assert client.post(
        transition_endpoint,
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "running"},
    ).status_code == 200
    assert client.post(
        f"/command-center/api/runs/{run_id}/cancel",
        headers=auth_headers,
    ).status_code == 200

    outcome = client.post(
        transition_endpoint,
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "status": "completed",
            "outcome_ref": "provider:appointment:confirmed",
            "result": {"content": "{}", "ui_events": []},
        },
    )
    next_tool = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": "worker-1",
            "provider_call_id": "must-not-start",
            "tool_name": "send_message",
            "arguments": {},
        },
    )
    cancelled = client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "cancelled"},
    )

    assert outcome.status_code == 200
    assert outcome.get_json()["status"] == "completed"
    assert next_tool.status_code == 409
    assert cancelled.status_code == 200
    assert cancelled.get_json()["status"] == "cancelled"


@pytest.mark.parametrize("mutation", [
    "completion", "event", "tool-registration", "finalization",
])
def test_worker_mutations_fail_after_authoritative_deadline(
        mutation, app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    if mutation == "completion":
        response = client.post(
            f"/command-center/api/runs/{run_id}/transition",
            headers=internal_headers,
            json={"worker_id": "worker-1", "status": "completed"},
        )
    elif mutation == "event":
        response = client.post(
            f"/command-center/api/runs/{run_id}/events",
            headers=internal_headers,
            json={"worker_id": "worker-1", "type": "agent.late"},
        )
    elif mutation == "tool-registration":
        response = client.post(
            f"/command-center/api/runs/{run_id}/tool-calls",
            headers=internal_headers,
            json={
                "worker_id": "worker-1",
                "provider_call_id": "late-tool",
                "tool_name": "book_appointment",
                "arguments": {},
            },
        )
    else:
        response = client.post(
            f"/command-center/api/runs/{run_id}/finalize",
            headers=internal_headers,
            json={
                "worker_id": "worker-1",
                "checkpoint_id": "late-final",
                "text": "must not persist",
            },
        )

    assert response.status_code == 409
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        assert run.status == "failed"
        assert run.error_class == "RunDeadlineExceeded"
        assert AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.completed").count() == 0
        assert AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="agent.late").count() == 0
        assert AgentToolCall.query.filter_by(
            run_id=run_id, provider_call_id="late-tool").count() == 0
        assert ConversationMessage.query.filter_by(
            tenant_id=TENANT,
            conversation_id=run.conversation_id,
            request_id=f"run:{run_id}:assistant",
        ).count() == 0


def test_overdue_queued_run_fails_instead_of_starting(
        app, client, auth_headers, internal_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    assert _claim(client, internal_headers).status_code == 204
    detail = client.get(
        f"/command-center/api/runs/{run_id}", headers=auth_headers).get_json()
    assert detail["status"] == "failed"
    assert detail["error_class"] == "RunDeadlineExceeded"


def test_event_replay_expires_queued_run_without_any_worker_claim(
        app, client, auth_headers):
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    replay = client.get(
        f"/command-center/api/runs/{run_id}/events", headers=auth_headers)

    assert replay.status_code == 200
    body = replay.get_json()
    assert body["status"] == "failed"
    assert [event["type"] for event in body["events"]] == [
        "run.queued", "run.deadline_exceeded"]
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        assert run.error_class == "RunDeadlineExceeded"


def test_postgres_workers_cannot_claim_one_run_twice(
        app, client, auth_headers, internal_headers):
    if db.engine.dialect.name != "postgresql":
        pytest.skip("row-lock concurrency contract requires PostgreSQL")
    from concurrent.futures import ThreadPoolExecutor

    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]

    def claim(worker):
        with app.test_client() as contender:
            response = _claim(contender, internal_headers, worker=worker)
            return response.status_code, response.get_json(silent=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))

    assert sorted(status for status, _body in results) == [200, 204]
    winner = next(body for status, body in results if status == 200)
    assert winner["id"] == run_id


def test_postgres_workers_serialize_distinct_runs_in_one_conversation(
        app, client, auth_headers, internal_headers):
    if db.engine.dialect.name != "postgresql":
        pytest.skip("conversation row-lock contract requires PostgreSQL")
    from concurrent.futures import ThreadPoolExecutor

    first_message = _message(
        client, auth_headers, text="first", request_id="request-first")
    second_message = _message(
        client, auth_headers, text="second", request_id="request-second")
    run_ids = {
        _run(client, auth_headers, first_message["id"]).get_json()["id"],
        _run(client, auth_headers, second_message["id"]).get_json()["id"],
    }

    def claim(worker):
        with app.test_client() as contender:
            response = _claim(contender, internal_headers, worker=worker)
            return response.status_code, response.get_json(silent=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))

    assert sorted(status for status, _body in results) == [200, 204]
    winner = next(body for status, body in results if status == 200)
    assert winner["id"] in run_ids

    completed = client.post(
        f"/command-center/api/runs/{winner['id']}/finalize",
        headers=internal_headers,
        json={
            "worker_id": winner["worker_id"],
            "checkpoint_id": "concurrency-final",
            "text": "first run complete",
        },
    )
    assert completed.status_code == 200
    next_run = _claim(client, internal_headers, worker="worker-c")
    assert next_run.status_code == 200
    assert next_run.get_json()["id"] == (run_ids - {winner["id"]}).pop()


@pytest.mark.parametrize("mutation", [
    "finalization", "event", "tool-registration",
])
def test_postgres_deadline_fences_concurrent_stale_worker_mutation(
        mutation, app, client, auth_headers, internal_headers, monkeypatch):
    if db.engine.dialect.name != "postgresql":
        pytest.skip("row-lock fencing contract requires PostgreSQL")
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import r6.agent_runs.routes as run_routes
    from r6.agent_runs.service import _terminalize_at_deadline

    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    assert _claim(client, internal_headers).status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    winner_locked = threading.Event()
    release_winner = threading.Event()
    mutation_started = threading.Event()
    original_lock = run_routes.lock_owned_run
    original_finalize = run_routes.finalize_run

    def observed_lock(*args, **kwargs):
        mutation_started.set()
        return original_lock(*args, **kwargs)

    def observed_finalize(*args, **kwargs):
        mutation_started.set()
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(run_routes, "lock_owned_run", observed_lock)
    monkeypatch.setattr(run_routes, "finalize_run", observed_finalize)

    def deadline_winner():
        with app.app_context():
            run = (
                AgentRun.query.filter_by(id=run_id)
                .with_for_update().one()
            )
            _terminalize_at_deadline(run, commit=False)
            winner_locked.set()
            assert release_winner.wait(5)
            db.session.commit()

    def stale_mutation():
        with app.test_client() as contender:
            if mutation == "finalization":
                return contender.post(
                    f"/command-center/api/runs/{run_id}/finalize",
                    headers=internal_headers,
                    json={
                        "worker_id": "worker-1",
                        "checkpoint_id": "late-final",
                        "text": "must not persist",
                    },
                ).status_code
            if mutation == "event":
                return contender.post(
                    f"/command-center/api/runs/{run_id}/events",
                    headers=internal_headers,
                    json={"worker_id": "worker-1", "type": "agent.late"},
                ).status_code
            return contender.post(
                f"/command-center/api/runs/{run_id}/tool-calls",
                headers=internal_headers,
                json={
                    "worker_id": "worker-1",
                    "provider_call_id": "late-tool",
                    "tool_name": "book_appointment",
                    "arguments": {},
                },
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(deadline_winner)
        assert winner_locked.wait(5)
        stale = pool.submit(stale_mutation)
        assert mutation_started.wait(5)
        release_winner.set()
        winner.result(timeout=5)
        assert stale.result(timeout=5) == 409

    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        assert run.status == "failed"
        assert AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="run.completed").count() == 0
        assert AgentRunEvent.query.filter_by(
            run_id=run_id, event_type="agent.late").count() == 0
        assert AgentToolCall.query.filter_by(
            run_id=run_id, provider_call_id="late-tool").count() == 0
        assert ConversationMessage.query.filter_by(
            tenant_id=TENANT,
            conversation_id=run.conversation_id,
            request_id=f"run:{run_id}:assistant",
        ).count() == 0


def test_a_refused_step_up_names_its_reason(client, caplog):
    """#307's second half. Destructuring `valid, _` satisfies the letter of
    the rule and still throws away the only thing that distinguishes a
    misconfigured secret from an expired token from a token minted for
    someone else's tenant — all three used to produce the same silent 401.

    MUTATION: change the call site back to `valid, _ = ...` and drop the log
    line. This goes red; the source-level `[0]` guard in
    tests/test_write_guard_matrix.py cannot see that regression.
    """
    import logging

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/command-center/api/runs",
            headers={"X-Tenant-Id": TENANT,
                     "X-Step-Up-Token": "not-even-a-token"},
            json={"tenant_id": TENANT, "message_id": "m-refused-1"},
        )

    assert response.status_code == 401
    blob = " ".join(record.getMessage() for record in caplog.records)
    assert "step-up refused" in blob, "the refusal was not recorded at all"
    assert "Malformed step-up token" in blob, (
        "the refusal was recorded without the reason — which is the half of "
        "#307 that destructuring alone does not fix")
    assert "not-even-a-token" not in blob, (
        "the rejected token itself was logged")
