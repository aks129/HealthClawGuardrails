"""The durable agent-run control plane leaves evidence, PHI-free (playbook B2).

`r6/agent_runs` was the last package in the codebase that mutated state and
audited none of it: fourteen endpoints, ten of them authenticated by a shared
secret rather than step-up, every one of them writing to the store and none of
them recording that it had. An agent could register a tool call,
execute a side effect, and persist an assistant answer with nothing in the
audit trail to say it happened.

`tests/test_ratchets.py` pinned that as the sole entry in the unaudited-mutator
set. That pin measured a *package*: one call to any audit primitive anywhere
inside `r6/agent_runs` would have emptied the set while thirteen endpoints
stayed silent. This file is the pin that measures what the ratchet's name
claims — every route in the blueprint is classified, and every route
classified as audited proves it at the wire.

The classification is the judgment call B2 exists to make, and it is
deliberately not "audit everything". `claim` and `heartbeat` are polled on a
timer by every live worker; auditing them would bury the records that matter
under queue chatter, which is the failure mode of an audit trail nobody can
read. The line drawn here: **a mutation a principal asked for is audited; a
mutation the system makes to itself on a timer is not.** The run's own
append-only `AgentRunEvent` log already holds the operational story.

The PHI half matters more than the coverage half, exactly as it did for the
command centre (`tests/test_command_center_writes_are_audited.py`, playbook
B1). `finalize` writes the assistant's answer and `tool-calls` writes the
arguments an agent chose — both are the most sensitive field in their request.
An audit trail that helpfully recorded either would copy PHI into the one
store whose whole contract is that it holds none.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from models import db
from r6.agent_runs.models import AgentRun, utcnow
from r6.models import AuditEventRecord

TENANT = "test-tenant"

#: A distinctive string that must never reach the audit trail.
_SECRET_TEXT = "Quintavious Zzyzxbarton says his A1c is 8.1 and he is scared"
_SECRET_TOKENS = ("Quintavious", "Zzyzxbarton", "A1c", "8.1", "scared")


# ---------------------------------------------------------------------------
# The classification — every route in the blueprint, and why
# ---------------------------------------------------------------------------

#: endpoint -> (event_type, resource_type) that the mutation must record.
_AUDITED = {
    "agent_runs.create_agent_run": ("create", "AgentRun"),
    "agent_runs.cancel_agent_run": ("update", "AgentRun"),
    "agent_runs.resume_agent_run": ("update", "AgentRun"),
    "agent_runs.transition_agent_run": ("update", "AgentRun"),
    "agent_runs.create_agent_tool_call": ("create", "AgentToolCall"),
    "agent_runs.transition_agent_tool_call": ("update", "AgentToolCall"),
    "agent_runs.finalize_agent_run": ("create", "ConversationMessage"),
    "agent_runs.reconcile_agent_tool_call": ("update", "AgentToolCall"),
}

#: endpoint -> why a state change here is NOT evidence worth keeping. Each of
#: these does write to the store; the claim is about what an audit reader
#: needs, not about whether a row moved.
_UNAUDITED = {
    "agent_runs.get_agent_run":
        "read; the only write is the shared deadline sweep, which no "
        "principal requested",
    "agent_runs.get_agent_run_events":
        "read; same deadline sweep",
    "agent_runs.get_agent_worker_health":
        "readiness poll; sweeps stranded runs on a timer",
    "agent_runs.claim_agent_run":
        "every live worker polls this continuously — the definition of "
        "queue chatter",
    "agent_runs.heartbeat_agent_run":
        "lease renewal, several times per run per minute",
    "agent_runs.append_agent_run_event":
        "the run's own append-only log; auditing it would copy one log into "
        "another once per streamed chunk",
}


def test_every_agent_run_route_is_classified(app):
    """A new endpoint cannot land without a decision about its trail.

    The ratchet this file replaces went green for the whole package the
    moment one audit call existed anywhere in it. This is the property that
    pin was named for: the classification is exhaustive, so endpoint fifteen
    is red until somebody says which half it belongs to.

    MUTATION: add a route to agent_runs_blueprint -> red.
    """
    routes = {
        rule.endpoint for rule in app.url_map.iter_rules()
        if rule.endpoint.startswith("agent_runs.")
    }
    assert routes, "the agent-runs blueprint is not registered"
    classified = set(_AUDITED) | set(_UNAUDITED)
    assert routes == classified, (
        "unclassified: " + ", ".join(sorted(routes - classified))
        + " | stale: " + ", ".join(sorted(classified - routes)))


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def internal_headers(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "run-worker-secret")
    return {"X-Internal-Secret": "run-worker-secret"}


@pytest.fixture
def reconcile_headers(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_RECONCILE_SECRET", "operator-secret")
    return {"X-Reconciliation-Secret": "operator-secret"}


def _events(app, resource_type=None):
    with app.app_context():
        query = AuditEventRecord.query.filter_by(tenant_id=TENANT)
        if resource_type:
            query = query.filter_by(resource_type=resource_type)
        return [
            {"action": event.event_type, "resource_type": event.resource_type,
             "resource_id": event.resource_id, "detail": event.detail}
            for event in query.all()
        ]


def _clear(app):
    with app.app_context():
        AuditEventRecord.query.delete()
        db.session.commit()


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
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()


def _run(client, auth_headers, message_id):
    return client.post(
        "/command-center/api/runs",
        headers=auth_headers,
        json={"tenant_id": TENANT, "message_id": message_id},
    )


def _claimed_run(client, auth_headers, internal_headers, *, worker="worker-1",
                 request_id="request-1"):
    """A run in `running`, held by `worker`."""
    message = _message(client, auth_headers, request_id=request_id)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    claimed = client.post(
        "/command-center/api/runs/claim",
        headers=internal_headers,
        json={"worker_id": worker, "lease_seconds": 60},
    )
    assert claimed.status_code == 200, claimed.get_data(as_text=True)
    return run_id


def _register_tool(client, internal_headers, run_id, *, worker="worker-1",
                   arguments=None, provider_call_id="provider-call-1"):
    response = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={
            "worker_id": worker,
            "provider_call_id": provider_call_id,
            "tool_name": "book_appointment",
            "arguments": arguments if arguments is not None else {"slot": "s1"},
        },
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()["id"]


# ---------------------------------------------------------------------------
# The audited mutations, one test each
# ---------------------------------------------------------------------------

def test_creating_a_run_is_audited(app, client, auth_headers):
    """MUTATION: delete the audit() in create_run -> red."""
    message = _message(client, auth_headers)
    _clear(app)

    created = _run(client, auth_headers, message["id"])
    assert created.status_code == 201, created.get_data(as_text=True)

    events = _events(app, "AgentRun")
    assert len(events) == 1, events
    assert events[0]["action"] == "create"
    assert events[0]["resource_id"] == created.get_json()["id"]


def test_an_idempotent_run_replay_is_not_audited_twice(app, client,
                                                       auth_headers):
    """A replay creates nothing, so evidence of a creation would be false.

    MUTATION: move the audit() above create_run's replay return -> red.
    """
    message = _message(client, auth_headers)
    _clear(app)

    first = _run(client, auth_headers, message["id"])
    replay = _run(client, auth_headers, message["id"])

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.get_json()["idempotent_replay"] is True
    assert len(_events(app, "AgentRun")) == 1, _events(app, "AgentRun")


def test_cancelling_a_run_is_audited(app, client, auth_headers):
    """MUTATION: delete the audit() in request_cancel -> red."""
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    _clear(app)

    cancelled = client.post(f"/command-center/api/runs/{run_id}/cancel",
                            headers=auth_headers)
    assert cancelled.status_code == 200, cancelled.get_data(as_text=True)

    events = _events(app, "AgentRun")
    assert len(events) == 1, events
    assert events[0]["action"] == "update"
    assert events[0]["resource_id"] == run_id
    assert "status=cancelled" in events[0]["detail"]


def test_cancelling_an_already_finished_run_is_not_audited(
        app, client, auth_headers, internal_headers):
    """Nothing changed, so nothing is recorded.

    MUTATION: audit unconditionally in request_cancel -> red.
    """
    run_id = _claimed_run(client, auth_headers, internal_headers)
    assert client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "failed"},
    ).status_code == 200
    _clear(app)

    cancelled = client.post(f"/command-center/api/runs/{run_id}/cancel",
                            headers=auth_headers)
    assert cancelled.status_code == 200
    assert _events(app) == []


def test_a_worker_transition_is_audited(app, client, auth_headers,
                                        internal_headers):
    """Entering the human gate is a once-per-run fact, not queue chatter.

    MUTATION: delete the audit() in transition_owned_run -> red.
    """
    run_id = _claimed_run(client, auth_headers, internal_headers)
    _clear(app)

    moved = client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "waiting_for_human"},
    )
    assert moved.status_code == 200, moved.get_data(as_text=True)

    events = _events(app, "AgentRun")
    assert len(events) == 1, events
    assert events[0]["action"] == "update"
    assert events[0]["resource_id"] == run_id
    assert "status=waiting_for_human" in events[0]["detail"]


def test_resuming_a_human_waiting_run_is_audited(app, client, auth_headers,
                                                 internal_headers):
    """The other half of the human gate: who let the agent continue.

    MUTATION: delete the audit() in resume_run -> red.
    """
    run_id = _claimed_run(client, auth_headers, internal_headers)
    assert client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "waiting_for_human"},
    ).status_code == 200
    _clear(app)

    resumed = client.post(f"/command-center/api/runs/{run_id}/resume",
                          headers=internal_headers)
    assert resumed.status_code == 200, resumed.get_data(as_text=True)

    events = _events(app, "AgentRun")
    assert len(events) == 1, events
    assert events[0]["action"] == "update"
    assert events[0]["resource_id"] == run_id
    assert "status=queued" in events[0]["detail"]


def test_registering_a_tool_call_is_audited(app, client, auth_headers,
                                            internal_headers):
    """The moment an agent commits to a real-world side effect.

    MUTATION: delete the audit() in register_tool_call -> red.
    """
    run_id = _claimed_run(client, auth_headers, internal_headers)
    _clear(app)
    call_id = _register_tool(client, internal_headers, run_id)

    events = _events(app, "AgentToolCall")
    assert len(events) == 1, events
    assert events[0]["action"] == "create"
    assert events[0]["resource_id"] == call_id
    assert "tool=book_appointment" in events[0]["detail"]


def test_an_idempotent_tool_replay_is_not_audited_twice(
        app, client, auth_headers, internal_headers):
    """MUTATION: move the audit() above register_tool_call's replay return -> red."""
    run_id = _claimed_run(client, auth_headers, internal_headers)
    _clear(app)
    _register_tool(client, internal_headers, run_id)

    replay = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls",
        headers=internal_headers,
        json={"worker_id": "worker-1", "provider_call_id": "provider-call-1",
              "tool_name": "book_appointment", "arguments": {"slot": "s1"}},
    )
    assert replay.status_code == 200
    assert replay.get_json()["idempotent_replay"] is True
    assert len(_events(app, "AgentToolCall")) == 1


def test_a_tool_call_outcome_is_audited(app, client, auth_headers,
                                        internal_headers):
    """MUTATION: delete the audit() in transition_tool_call -> red."""
    run_id = _claimed_run(client, auth_headers, internal_headers)
    call_id = _register_tool(client, internal_headers, run_id)
    _clear(app)

    endpoint = (
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition")
    assert client.post(endpoint, headers=internal_headers,
                       json={"worker_id": "worker-1",
                             "status": "running"}).status_code == 200
    assert client.post(endpoint, headers=internal_headers,
                       json={"worker_id": "worker-1",
                             "status": "completed",
                             "result": {"confirmation": "ok"}}
                       ).status_code == 200

    events = _events(app, "AgentToolCall")
    assert [event["action"] for event in events] == ["update", "update"]
    assert {event["resource_id"] for event in events} == {call_id}
    details = " ".join(event["detail"] for event in events)
    assert "status=running" in details and "status=completed" in details


def test_finalizing_a_run_audits_the_assistant_message_and_the_completion(
        app, client, auth_headers, internal_headers):
    """The assistant turn is a write of the same shape the command centre
    audits for the user turn. Auditing one and not the other would leave the
    trail able to show a question and never its answer.

    MUTATION: delete either audit() in finalize_run -> red.
    """
    run_id = _claimed_run(client, auth_headers, internal_headers)
    _clear(app)

    finalized = client.post(
        f"/command-center/api/runs/{run_id}/finalize",
        headers=internal_headers,
        json={"worker_id": "worker-1", "checkpoint_id": "round-1",
              "text": "Your appointment brief is ready."},
    )
    assert finalized.status_code == 200, finalized.get_data(as_text=True)
    message_id = finalized.get_json()["message_id"]

    messages = _events(app, "ConversationMessage")
    assert len(messages) == 1, messages
    assert messages[0]["action"] == "create"
    assert messages[0]["resource_id"] == message_id
    assert "role=assistant" in messages[0]["detail"]

    runs = _events(app, "AgentRun")
    assert len(runs) == 1, runs
    assert runs[0]["action"] == "update"
    assert "status=completed" in runs[0]["detail"]


def test_a_finalize_replay_is_not_audited_twice(app, client, auth_headers,
                                                internal_headers):
    """MUTATION: audit before finalize_run's completed-replay return -> red."""
    run_id = _claimed_run(client, auth_headers, internal_headers)
    payload = {"worker_id": "worker-1", "checkpoint_id": "round-1",
               "text": "Your appointment brief is ready."}
    endpoint = f"/command-center/api/runs/{run_id}/finalize"
    _clear(app)

    assert client.post(endpoint, headers=internal_headers,
                       json=payload).status_code == 200
    replay = client.post(endpoint, headers=internal_headers, json=payload)

    assert replay.status_code == 200
    assert replay.get_json()["idempotent_replay"] is True
    assert len(_events(app, "ConversationMessage")) == 1
    assert len(_events(app, "AgentRun")) == 1


def test_reconciling_an_ambiguous_side_effect_is_audited(
        app, client, auth_headers, internal_headers, reconcile_headers):
    """Operator-attested provider truth is the single most evidence-worthy
    mutation in this package: a human asserting what happened in the world.

    MUTATION: delete the audit() in reconcile_tool_call -> red.
    """
    run_id = _claimed_run(client, auth_headers, internal_headers,
                          worker="lost-worker")
    call_id = _register_tool(client, internal_headers, run_id,
                             worker="lost-worker")
    assert client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={"worker_id": "lost-worker", "status": "running"},
    ).status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert client.get(f"/command-center/api/runs/{run_id}/events",
                      headers=auth_headers).get_json()[
                          "status"] == "waiting_for_human"
    _clear(app)

    reconciled = client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/reconcile",
        headers=reconcile_headers,
        json={"status": "completed", "evidence_ref": "provider:truth:1"},
    )
    assert reconciled.status_code == 200, reconciled.get_data(as_text=True)

    events = _events(app, "AgentToolCall")
    assert len(events) == 1, events
    assert events[0]["action"] == "update"
    assert events[0]["resource_id"] == call_id
    assert "evidence=provider:truth:1" in events[0]["detail"]


# ---------------------------------------------------------------------------
# The half that would be a leak rather than a gap
# ---------------------------------------------------------------------------

def test_the_finalize_audit_never_carries_the_assistant_answer(
        app, client, auth_headers, internal_headers):
    """`text` is the agent's answer about a patient. CLAUDE.md: audit detail
    stays PHI-free, and chat transcripts are PHI-adjacent.

    MUTATION: put `text` (or any slice of it) into either detail -> red.
    """
    run_id = _claimed_run(client, auth_headers, internal_headers)
    _clear(app)

    client.post(
        f"/command-center/api/runs/{run_id}/finalize",
        headers=internal_headers,
        json={"worker_id": "worker-1", "checkpoint_id": "round-1",
              "text": _SECRET_TEXT},
    )

    blob = json.dumps(_events(app))
    for token in _SECRET_TOKENS:
        assert token not in blob, (
            f"the audit trail carries {token!r} out of the assistant answer")


def test_the_tool_call_audit_never_carries_the_arguments(
        app, client, auth_headers, internal_headers):
    """Tool arguments are model-authored and routinely carry the clinical
    reason for the action.

    MUTATION: put `arguments` into the register detail -> red.
    """
    run_id = _claimed_run(client, auth_headers, internal_headers)
    _clear(app)
    call_id = _register_tool(
        client, internal_headers, run_id,
        arguments={"slot": "s1", "reason": _SECRET_TEXT})

    client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "running"},
    )
    client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "failed",
              "result": {"note": _SECRET_TEXT}},
    )

    blob = json.dumps(_events(app))
    for token in _SECRET_TOKENS:
        assert token not in blob, (
            f"the audit trail carries {token!r} out of a tool call")


# ---------------------------------------------------------------------------
# The negative half — a classification that only ever says "yes" proves nothing
# ---------------------------------------------------------------------------

def test_a_refused_mutation_leaves_no_audit_event(app, client, auth_headers,
                                                  internal_headers):
    """Evidence of a write that was refused is worse than none.

    Also the fail-closed check for the flush-only audit primitive: a handler
    that audits and then raises must leave the row rolled back, not pending.

    Both refusals here are raised BEFORE the audit could be reached — 401 at
    the route, and the wrong-worker 409 inside `lock_owned_run`, which has no
    `run` to audit yet. So neither one exercises the stated mutation; moving
    `_audit_run_change` above the fence is caught by the double-count in
    test_a_worker_transition_is_audited, not here (verified 2026-09-04).
    The third case below is the one that does: it passes the fence with the
    right worker and is then refused, so an audit placed on that path WOULD
    run. It also exercises the fail-closed half — a flushed row behind a 409
    trips the kernel's install_audit_assertions at teardown.

    MUTATION: audit before the `completed` raise in transition_owned_run -> red.
    """
    message = _message(client, auth_headers)
    run_id = _claimed_run(client, auth_headers, internal_headers,
                          request_id="request-2")
    _clear(app)

    unauthenticated = _run(client, {"X-Tenant-Id": TENANT}, message["id"])
    wrong_worker = client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "not-the-owner", "status": "failed"},
    )
    # Right worker, past the fence, refused on the next line: `completed` is
    # reachable only through finalize.
    refused_past_the_fence = client.post(
        f"/command-center/api/runs/{run_id}/transition",
        headers=internal_headers,
        json={"worker_id": "worker-1", "status": "completed"},
    )

    assert unauthenticated.status_code == 401
    assert wrong_worker.status_code == 409
    assert refused_past_the_fence.status_code == 409
    assert _events(app) == []


def test_queue_chatter_is_deliberately_not_audited(app, client, auth_headers,
                                                   internal_headers):
    """The classification has to be false somewhere or it is not a decision.

    These three fire on a timer for every live worker. If auditing them ever
    becomes right, this test is the place that argues about it — not a silent
    drift in either direction.
    """
    message = _message(client, auth_headers)
    run_id = _run(client, auth_headers, message["id"]).get_json()["id"]
    _clear(app)

    assert client.post("/command-center/api/runs/claim",
                       headers=internal_headers,
                       json={"worker_id": "worker-1"}).status_code == 200
    assert client.post(f"/command-center/api/runs/{run_id}/heartbeat",
                       headers=internal_headers,
                       json={"worker_id": "worker-1"}).status_code == 200
    assert client.post(f"/command-center/api/runs/{run_id}/events",
                       headers=internal_headers,
                       json={"worker_id": "worker-1", "type": "agent.thought"}
                       ).status_code == 201

    assert _events(app) == []


def test_the_deadline_sweep_writes_the_human_gate_and_audits_nothing(
        app, client, auth_headers, internal_headers):
    """The other three exemptions, and the one asymmetry they cost.

    `test_queue_chatter_is_deliberately_not_audited` covers claim, heartbeat
    and the event log — three of the six exempt endpoints. The other three are
    the GETs, and calling them "reads" undersells what they do: the shared
    deadline sweep runs inside them, and on a run holding a RUNNING tool call
    it commits the run into `waiting_for_human` and the tool call into
    `needs_reconciliation`. That is entry into the human gate, and it is the
    same state `POST /transition` reaches — where it IS audited.

    So the classification is per-code-path, not per-route: the gate is audited
    when a worker declares it and silent when the deadline reaches it. The
    consequence is that a `reconcile` audit — the package's most
    evidence-worthy row — can stand in the trail with no record of how its
    call became ambiguous. The same sweep also runs inside
    `_enforce_worker_fence`, so `/transition`, `/heartbeat`, `/tool-calls`,
    `/finalize` and `POST /events` each have a path that commits this and then
    answers 409.

    This test does not argue that is wrong — `AgentRunEvent` holds the story
    and a timer is not a principal. It pins it, so a change of mind is an edit
    here rather than drift, and so the three GET exemptions are exercised at
    the wire like the other three.
    """
    run_id = _claimed_run(client, auth_headers, internal_headers,
                          worker="lost-worker")
    call_id = _register_tool(client, internal_headers, run_id,
                             worker="lost-worker")
    assert client.post(
        f"/command-center/api/runs/{run_id}/tool-calls/{call_id}/transition",
        headers=internal_headers,
        json={"worker_id": "lost-worker", "status": "running"},
    ).status_code == 200
    with app.app_context():
        run = db.session.get(AgentRun, run_id)
        run.deadline_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    _clear(app)

    # GET #1: the read that terminalizes.
    detail = client.get(f"/command-center/api/runs/{run_id}",
                        headers=auth_headers)
    assert detail.status_code == 200, detail.get_data(as_text=True)
    body = detail.get_json()
    # The write really happened, so "nothing was audited" is a decision about
    # evidence and not an observation that nothing moved.
    assert body["status"] == "waiting_for_human", body
    assert [call["status"] for call in body["tool_calls"]] == [
        "needs_reconciliation"], body

    # GET #2 and #3: the event replay and the readiness poll, both of which
    # run the same sweep.
    assert client.get(f"/command-center/api/runs/{run_id}/events",
                      headers=auth_headers).status_code == 200
    assert client.get(
        "/command-center/api/runs/workers/health",
        headers=internal_headers).status_code in (200, 503)

    assert _events(app) == []
