"""The run queue's gates and audit hold when the core serves it alone.

The core registers the run-queue blueprint itself, so a deployment with no
extension loaded serves all fourteen routes. The tests that prove those
routes' authentication and audit (tests/test_agent_runs.py,
tests/test_agent_run_writes_are_audited.py) create their user message
through the command center, carry the `extension` mark, and are deselected
by the core-only CI job. Without this file that job proves one route of
fourteen is gated.

Nothing here imports an optional module or calls an optional route: the
conversation and its message are written through r6/conversations.py, which
is core. No `extension` mark, so both CI jobs run it. Synthetic data only.
"""

from __future__ import annotations

import pytest

from models import db
from r6.agent_runs.models import AgentRun, AgentToolCall
from r6.conversations import Conversation, ConversationMessage
from r6.models import AuditEventRecord
from r6.stepup import generate_step_up_token

TENANT = "test-tenant"
OTHER = "other-tenant"
RUNS = "/command-center/api/runs"

#: Distinctive strings that must never reach the audit trail.
QUESTION = "SYNTHETIC Wilhelmina Quorthon asks about her lisinopril"
ANSWER = "SYNTHETIC Zebediah Marrowfield potassium is high"
ARGUMENT = "SYNTHETIC slot for Ottoline Feverfew"
_PHI_TOKENS = ("Wilhelmina", "Quorthon", "lisinopril", "Zebediah",
               "Marrowfield", "potassium", "Ottoline", "Feverfew",
               "SYNTHETIC")

#: Every route, by the credential it asks for. Exhaustive on purpose: a
#: fifteenth route is red here until somebody says how it is gated.
_STEP_UP = {
    "agent_runs.create_agent_run", "agent_runs.get_agent_run",
    "agent_runs.get_agent_run_events", "agent_runs.cancel_agent_run",
}
_INTERNAL_SECRET = {
    "agent_runs.get_agent_worker_health", "agent_runs.resume_agent_run",
    "agent_runs.claim_agent_run", "agent_runs.heartbeat_agent_run",
    "agent_runs.transition_agent_run", "agent_runs.append_agent_run_event",
    "agent_runs.create_agent_tool_call",
    "agent_runs.transition_agent_tool_call", "agent_runs.finalize_agent_run",
}
_RECONCILE_SECRET = {"agent_runs.reconcile_agent_tool_call"}

#: A body every handler would accept, so a refusal is the gate's and not a
#: validation error that happened to run first.
_BODY = {
    "tenant_id": TENANT, "message_id": "msg-1", "worker_id": "worker-1",
    "lease_seconds": 60, "status": "failed", "type": "model.delta",
    "provider_call_id": "provider-1", "tool_name": "book_appointment",
    "arguments": {"slot": "s1"}, "text": "answer", "checkpoint_id": "ckpt-1",
    "evidence_ref": "evidence-1",
}


def _seed_message(tenant=TENANT, message_id="msg-1"):
    conversation_id = f"careagents:juniper-{tenant}"
    if db.session.get(Conversation, (tenant, conversation_id)) is None:
        db.session.add(Conversation(
            id=conversation_id, tenant_id=tenant, agent_id="juniper",
            created_by_surface="web"))
        # Postgres checks the message's foreign key at insert.
        db.session.flush()
    db.session.add(ConversationMessage(
        id=message_id, tenant_id=tenant, conversation_id=conversation_id,
        agent_id="juniper", channel="web", role="user", text=QUESTION,
        request_id=f"request-{message_id}"))
    db.session.commit()
    return message_id


def _headers(tenant=TENANT):
    return {"X-Tenant-Id": tenant,
            "X-Step-Up-Token": generate_step_up_token(tenant)}


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "run-worker-secret")
    monkeypatch.setenv("AGENT_RUN_RECONCILE_SECRET", "operator-secret")
    return ({"X-Internal-Secret": "run-worker-secret"},
            {"X-Reconciliation-Secret": "operator-secret"})


@pytest.fixture
def queued_run(app, client, secrets):
    """One queued run for TENANT, created through the route."""
    created = client.post(RUNS, headers=_headers(), json={
        "tenant_id": TENANT, "message_id": _seed_message()})
    assert created.status_code == 201, created.get_data(as_text=True)
    return created.get_json()["id"]


def _run_queue_rules(app):
    return [rule for rule in app.url_map.iter_rules()
            if rule.endpoint.startswith("agent_runs.")]


def _request(client, rule, run_id, headers):
    path = rule.rule.replace("<run_id>", run_id).replace("<call_id>", "call-1")
    method = "POST" if "POST" in rule.methods else "GET"
    kwargs = {"json": _BODY} if method == "POST" else {}
    return client.open(path, method=method, headers=headers, **kwargs)


def _state(app):
    """Everything a run-queue request can change, as comparable rows."""
    with app.app_context():
        db.session.expire_all()
        return (
            sorted((r.id, r.status, r.worker_id, bool(r.cancel_requested),
                    r.attempt) for r in AgentRun.query.all()),
            AgentToolCall.query.count(),
            ConversationMessage.query.count(),
            AuditEventRecord.query.count(),
        )


def test_every_run_queue_route_is_classified_by_its_credential(app):
    """MUTATION: add a route to agent_runs_blueprint -> red."""
    endpoints = {rule.endpoint for rule in _run_queue_rules(app)}
    assert len(endpoints) == 14, sorted(endpoints)
    assert endpoints == _STEP_UP | _INTERNAL_SECRET | _RECONCILE_SECRET


@pytest.mark.parametrize("headers", [
    {},
    {"X-Tenant-Id": TENANT},
    {"X-Tenant-Id": TENANT, "X-Step-Up-Token": "AAAA.BBBB"},
    {"X-Internal-Secret": "wrong"},
    {"X-Internal-Secret": ""},
    {"X-Reconciliation-Secret": "wrong"},
], ids=["anonymous", "tenant-header-only", "forged-token",
        "wrong-internal-secret", "empty-internal-secret",
        "wrong-reconcile-secret"])
def test_no_run_queue_route_answers_without_its_credential(
        app, client, queued_run, headers):
    """Each route is asked against a run that exists, so a 404 cannot stand
    in for a refusal, and nothing may change while it refuses.

    MUTATION: delete the `_internal_authorized()` check from any internal
    route, the `_reconciler_authorized()` check from reconcile, or a
    `_tenant_authorized()` check from a tenant route -> red.
    """
    before = _state(app)
    rules = _run_queue_rules(app)
    assert rules, "the run-queue blueprint is not registered"
    for rule in rules:
        response = _request(client, rule, queued_run, headers)
        want = 401 if rule.endpoint in _STEP_UP else 403
        assert response.status_code == want, (
            rule.endpoint, response.status_code,
            response.get_data(as_text=True)[:200])
    assert _state(app) == before


def test_each_secret_opens_only_its_own_routes(app, client, queued_run,
                                               secrets):
    """The worker secret does not reconcile, the operator secret does not
    drive the queue, and a tenant's step-up token does neither.

    MUTATION: make `_reconciler_authorized` fall back to
    `_internal_authorized()` -> red.
    """
    internal, reconcile = secrets
    before = _state(app)
    for rule in _run_queue_rules(app):
        for headers, opens in ((internal, _INTERNAL_SECRET),
                               (reconcile, _RECONCILE_SECRET),
                               (_headers(), _STEP_UP)):
            if rule.endpoint in opens:
                continue
            response = _request(client, rule, queued_run, headers)
            want = 401 if rule.endpoint in _STEP_UP else 403
            assert response.status_code == want, (
                rule.endpoint, sorted(headers), response.status_code)
    assert _state(app) == before


def test_unset_secrets_fail_closed(app, client, queued_run, monkeypatch):
    """With neither secret configured no header value opens a route, the
    empty one included.

    MUTATION: drop `expected and` from `_internal_authorized` or
    `_reconciler_authorized` -> red (an empty header matches an unset
    secret).
    """
    monkeypatch.delenv("INTERNAL_TOKEN_MINT_SECRET", raising=False)
    monkeypatch.delenv("AGENT_RUN_RECONCILE_SECRET", raising=False)
    before = _state(app)
    gated = [rule for rule in _run_queue_rules(app)
             if rule.endpoint in _INTERNAL_SECRET | _RECONCILE_SECRET]
    assert len(gated) == 10
    for rule in gated:
        for value in ("", "run-worker-secret", "operator-secret"):
            response = _request(client, rule, queued_run, {
                "X-Internal-Secret": value,
                "X-Reconciliation-Secret": value})
            assert response.status_code == 403, (rule.endpoint, value)
    assert _state(app) == before


def test_another_tenants_token_cannot_reach_a_run(app, client, queued_run):
    """A valid token for a different tenant, with either tenant header.

    MUTATION: authorize against the X-Tenant-Id header instead of the run's
    own tenant in get/events/cancel -> red.
    """
    other = _headers(OTHER)
    crossed = {"X-Tenant-Id": TENANT,
               "X-Step-Up-Token": other["X-Step-Up-Token"]}
    for headers in (other, crossed):
        assert client.get(f"{RUNS}/{queued_run}",
                          headers=headers).status_code == 401
        assert client.get(f"{RUNS}/{queued_run}/events",
                          headers=headers).status_code == 401
        assert client.post(f"{RUNS}/{queued_run}/cancel", headers=headers,
                           json={}).status_code == 401
    with app.app_context():
        run = db.session.get(AgentRun, queued_run)
        assert run.status == "queued" and not run.cancel_requested


def test_a_token_cannot_create_a_run_for_another_tenant(app, client):
    """The body names the tenant; the token must be bound to that tenant,
    and a tenant cannot queue a run on another tenant's message.

    MUTATION: authorize `request.headers["X-Tenant-Id"]` instead of the
    resolved `tenant_id` in create_agent_run -> red.
    """
    _seed_message(TENANT, "msg-1")
    _seed_message(OTHER, "msg-other")
    # OTHER's token and header, TENANT named in the body.
    assert client.post(RUNS, headers=_headers(OTHER), json={
        "tenant_id": TENANT, "message_id": "msg-1"}).status_code == 401
    # TENANT's token and header, OTHER named in the body.
    assert client.post(RUNS, headers=_headers(), json={
        "tenant_id": OTHER, "message_id": "msg-other"}).status_code == 401
    # OTHER, properly authenticated, asking for TENANT's message.
    assert client.post(RUNS, headers=_headers(OTHER), json={
        "tenant_id": OTHER, "message_id": "msg-1"}).status_code == 404
    with app.app_context():
        assert AgentRun.query.count() == 0


def test_a_run_driven_through_the_core_is_audited_without_phi(
        app, client, queued_run, secrets):
    """Create, claim, tool call and its result, finalize: the
    principal-requested changes leave rows for the run's tenant, and none
    carries the question, the tool argument, the tool result or the answer.

    MUTATION: delete the audit() in create_run -> red; put the answer text
    into finalize_run's _detail() -> red.
    """
    internal, _ = secrets
    claimed = client.post(f"{RUNS}/claim", headers=internal, json={
        "worker_id": "worker-1", "lease_seconds": 60})
    assert claimed.status_code == 200, claimed.get_data(as_text=True)
    assert claimed.get_json()["id"] == queued_run
    tool = client.post(f"{RUNS}/{queued_run}/tool-calls", headers=internal,
                       json={"worker_id": "worker-1",
                             "provider_call_id": "provider-1",
                             "tool_name": "book_appointment",
                             "arguments": {"slot": ARGUMENT}})
    assert tool.status_code == 201, tool.get_data(as_text=True)
    call = f"{RUNS}/{queued_run}/tool-calls/{tool.get_json()['id']}/transition"
    for status, extra in (("running", {}),
                          ("completed", {"result": {"note": ANSWER}})):
        moved = client.post(call, headers=internal, json={
            "worker_id": "worker-1", "status": status, **extra})
        assert moved.status_code == 200, moved.get_data(as_text=True)
    done = client.post(f"{RUNS}/{queued_run}/finalize", headers=internal,
                       json={"worker_id": "worker-1", "text": ANSWER,
                             "checkpoint_id": "ckpt-1"})
    assert done.status_code == 200, done.get_data(as_text=True)

    with app.app_context():
        rows = AuditEventRecord.query.filter_by(tenant_id=TENANT).all()
        recorded = sorted((row.event_type, row.resource_type) for row in rows)
        everything = " ".join(
            " ".join(str(getattr(row, column.name)) for column in
                     AuditEventRecord.__table__.columns)
            for row in AuditEventRecord.query.all())
    assert recorded == [
        ("create", "AgentRun"), ("create", "AgentToolCall"),
        ("create", "ConversationMessage"), ("update", "AgentRun"),
        ("update", "AgentToolCall"), ("update", "AgentToolCall"),
    ], recorded
    assert not [token for token in _PHI_TOKENS if token in everything]
