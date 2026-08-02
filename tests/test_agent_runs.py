"""Durable AgentRun queue, replay, state, lease, and tool idempotency."""

from __future__ import annotations

from datetime import timedelta

import pytest

from models import db
from r6.agent_runs.models import AgentRun, AgentRunEvent, utcnow
from r6.agent_runs.state import RUN_STATES


TENANT = "test-tenant"


@pytest.fixture
def internal_headers(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "run-worker-secret")
    return {"X-Internal-Secret": "run-worker-secret"}


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
        f"/command-center/api/runs/{winner['id']}/transition",
        headers=internal_headers,
        json={"worker_id": winner["worker_id"], "status": "completed"},
    )
    assert completed.status_code == 200
    next_run = _claim(client, internal_headers, worker="worker-c")
    assert next_run.status_code == 200
    assert next_run.get_json()["id"] == (run_ids - {winner["id"]}).pop()
