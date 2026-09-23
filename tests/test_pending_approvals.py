"""A signed-in person can find what is waiting for their answer (#215, #413 P3).

Ownership is resolved on the server: account → agent → connection → tenant.
A request proposed over MCP has no CareAgents agent id; it belongs to whoever
owns the connection to that tenant, and appears on that person's list. The
list, the review page, its submit and decline, and the status view all apply
one rule (`_live_agent_context`): another account, another tenant, a revoked
connection, an expired or declined request — none of them reach a page.
CareAgents stores nothing about any of this; the list is the engine's answer.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests as _requests

from careagents.healthclaw import HealthClawError
from tests.test_careagents import FakeClient, _login, _make_direct_conn
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

#: The CareAgents fixtures, re-exported under their own names; the engine's
#: `app`/`client` fixtures come from conftest and are used only in the
#: engine-list tests below.
cfg = _cfg_fixture
svc = _svc_fixture

CANARY = "CANARY-DISPLAY-5150"
#: A test-only shared value for the approval-token mint; not a credential.
_MINT = "test-mint-" + "value"


# --- the engine list --------------------------------------------------------

def _propose(client, tenant_headers, kind="sms", to="Dr. Smith"):
    r = client.post("/r6/actions/propose", json={
        "kind": kind, "payload": {"to": to, "phone": "617-555-0100",
                                  "body": "Reminder."}}, headers=tenant_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()["id"]


def _commit(client, auth_headers, action_id):
    assert client.post("/r6/actions/%s/commit" % action_id,
                       headers=auth_headers).status_code == 202


def test_the_engine_lists_only_live_awaiting_requests_for_the_tenant(
        client, app, tenant_headers, auth_headers, other_tenant_headers):
    from models import db
    from r6.actions.models import ProposedAction
    waiting = _propose(client, tenant_headers, to="Waiting")
    _commit(client, auth_headers, waiting)
    only_proposed = _propose(client, tenant_headers, to="Proposed")   # noqa: F841
    declined = _propose(client, tenant_headers, to="Declined")
    _commit(client, auth_headers, declined)
    from tests.approval_helpers import approval_headers
    assert client.post("/r6/actions/%s/decline" % declined,
                       headers=approval_headers(app, auth_headers, declined)
                       ).status_code == 200
    lapsed = _propose(client, tenant_headers, to="Lapsed")
    _commit(client, auth_headers, lapsed)
    with app.app_context():
        ProposedAction.query.filter_by(id=lapsed).update(
            {"expires_at": datetime.now(timezone.utc).replace(tzinfo=None)
             - timedelta(minutes=1)}, synchronize_session=False)
        db.session.commit()

    r = client.get("/r6/actions", headers=tenant_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert [a["to"] for a in body["actions"]] == ["Waiting"]
    assert body["count"] == 1
    assert set(body["actions"][0]) == {"id", "kind", "to", "status", "expires_at"}
    # Another tenant sees none of it.
    assert client.get("/r6/actions", headers=other_tenant_headers).get_json()["count"] == 0
    # Only the one status is listable; nothing else is a pending inbox.
    assert client.get("/r6/actions?status=completed", headers=tenant_headers).status_code == 400
    assert client.get("/r6/actions").status_code == 400
    # The lapsed request is not shown and its page is gone too.
    assert client.get("/r6/actions/%s/review" % lapsed, headers=auth_headers).status_code == 404


def test_the_list_persists_nothing(client, app, tenant_headers, auth_headers):
    from models import db
    from r6.models import AuditEventRecord
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    with app.app_context():
        before = AuditEventRecord.query.count()
    client.get("/r6/actions", headers=tenant_headers)
    with app.app_context():
        db.session.rollback()
        assert AuditEventRecord.query.count() == before


# --- the CareAgents surfaces, faked engine ----------------------------------

def _ca_app(cfg, svc, client=None):
    from careagents.app import create_app
    a = create_app(config=cfg, client=client or FakeClient(), accounts=svc)
    a.config["TESTING"] = True
    return a


def _agent(app, svc, monkeypatch, email="user@example.com"):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    conn = c.post("/api/connections/sample").get_json()["id"]
    agent = c.post("/api/agents", json={"name": "A", "persona": "calm",
                                        "connection_id": conn}).get_json()["id"]
    return c, agent, conn


def test_the_approvals_page_lists_the_engines_answer_with_review_links(
        cfg, svc, monkeypatch):
    c, agent, _ = _agent(_ca_app(cfg, svc), svc, monkeypatch)
    r = c.get(f"/agents/{agent}/approvals")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Intake form" in html
    assert f"/review/{agent}/act-1" in html


def test_an_outage_is_not_an_empty_inbox(cfg, svc, monkeypatch):
    class Down(FakeClient):
        def pending_actions(self, tenant):
            raise HealthClawError("pending actions failed", 0)
    c, agent, _ = _agent(_ca_app(cfg, svc, Down()), svc, monkeypatch)
    r = c.get(f"/agents/{agent}/approvals")
    assert r.status_code == 503
    html = r.get_data(as_text=True)
    assert "check for requests right now" in html
    assert "Nothing is waiting" not in html


@pytest.mark.parametrize("surface", [
    "GET /agents/{agent}/approvals",
    "GET /review/{agent}/act-1",
    "POST /review/{agent}/act-1/submit",
    "POST /review/{agent}/act-1/decline",
    "GET /api/form/act-1?agent={agent}",
])
def test_every_surface_applies_the_same_ownership_rule(cfg, svc, monkeypatch,
                                                       surface):
    app = _ca_app(cfg, svc)
    c, agent, conn = _agent(app, svc, monkeypatch)
    method, path = surface.split(" ", 1)
    path = path.format(agent=agent)

    def hit(client):
        return client.open(path, method=method, json={} if method == "POST" else None)

    # The owner reaches the surface (a 422 on submit is the fake form's
    # validation answering, which is past ownership).
    assert hit(c).status_code != 404, surface
    other = app.test_client()
    _login(other, svc, monkeypatch, email="mallory@example.com")
    assert hit(other).status_code == 404, surface          # another account
    assert c.post(f"/api/connections/{conn}/disconnect").status_code == 200
    assert hit(c).status_code == 404, surface              # revoked connection


def test_a_request_the_engine_does_not_know_is_not_the_agents(cfg, svc, monkeypatch):
    c, agent, _ = _agent(_ca_app(cfg, svc), svc, monkeypatch)
    assert c.get(f"/review/{agent}/act-elsewhere").status_code == 404
    assert c.get(f"/api/form/act-elsewhere?agent={agent}").status_code == 404


def test_the_chat_header_links_to_the_list(cfg, svc, monkeypatch):
    c, agent, _ = _agent(_ca_app(cfg, svc), svc, monkeypatch)
    html = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert f"/agents/{agent}/approvals" in html


# --- the real chain: engine WSGI <- CareAgents relay <- signed-in browser --

def test_a_correction_proposed_the_way_the_mcp_tool_sends_it_is_found_reviewed_and_carried_out(
        cfg, svc, monkeypatch):
    """No fakes on either side. The engine is the real Flask app on its own
    DB; CareAgents talks to it through a real HealthClawClient whose session
    is relayed onto the engine's WSGI. The proposal is posted with the bytes
    services/agent-orchestrator/src/tools.test.ts pins for curatr_apply_fix
    — the Node process itself is not spawned here.

    Journey: propose (as the MCP tool does) → commit → the person finds it
    on their list → opens the review page through the relay → approves →
    the outcome is truthful → the record changed once → the list is empty
    → reopening the request answers 404. The record's content (a display
    canary) reaches the person only on the relayed review page and lands in
    no CareAgents table.
    """
    from careagents.app import create_app
    from careagents.healthclaw import HealthClawClient
    from main import create_app as engine_create_app
    from models import db
    from r6.models import R6Resource
    from r6.stepup import generate_step_up_token

    tenant = "ca-approvals"
    monkeypatch.setenv("PUBLIC_TENANTS", f"test-tenant,{tenant}")
    monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    monkeypatch.setenv("CURATR_FIX_RAIL_ENABLED", "1")
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", _MINT)
    engine_app = engine_create_app({
        "TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "LEGACY_BOOT_ON_CREATE": False})
    with engine_app.app_context():
        db.create_all()
        from r6.actions.registry import _clear
        from r6.actions.rails import register_all
        _clear()
        register_all()
        db.session.add(R6Resource("Condition", json.dumps({
            "resourceType": "Condition", "id": "c-chain",
            "subject": {"reference": "Patient/alice"},
            "code": {"coding": [{"system": "http://snomed.info/sct",
                                 "code": "44054006", "display": CANARY}]},
            "clinicalStatus": {"coding": [{"code": "active"}]}}),
            resource_id="c-chain", tenant_id=tenant))
        db.session.commit()
    engine = engine_app.test_client()

    class _RelaySession:
        def post(self, url, json=None, headers=None, timeout=None, data=None):
            return _to_requests(engine.post(url.replace("http://engine", ""),
                                            json=json, data=data,
                                            headers=headers or {}))

        def get(self, url, params=None, headers=None, timeout=None):
            return _to_requests(engine.get(url.replace("http://engine", ""),
                                           query_string=params or {},
                                           headers=headers or {}))

    def _to_requests(resp):
        r = _requests.Response()
        r.status_code = resp.status_code
        r._content = resp.get_data() or b""
        r.headers.update(resp.headers.to_wsgi_list())
        return r

    real = HealthClawClient(base="http://engine", mint_secret=_MINT)
    real.http = _RelaySession()
    real.new_tenant_id = lambda: tenant
    app = create_app(config=cfg, client=real, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email="owner@example.com")
    conn = _make_direct_conn(c)
    agent = c.post("/api/agents", json={"name": "A", "persona": "calm",
                                        "connection_id": conn}).get_json()["id"]

    # 1. Proposed the way the MCP tool sends it (pinned in tools.test.ts).
    tenant_headers = {"X-Tenant-Id": tenant}
    r = engine.post("/r6/actions/propose", json={
        "kind": "curatr-fix",
        "payload": {"to": "Condition/c-chain",
                    "body": "Mark this condition as resolved.",
                    "curatr_fix": {"resource_type": "Condition",
                                   "resource_id": "c-chain",
                                   "record_version": 1,
                                   "fixes": [{"field_path": "Condition.clinicalStatus.coding[0].code",
                                              "new_value": "resolved"}],
                                   "patient_intent": "it cleared up"}}},
        headers=tenant_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    action_id = r.get_json()["id"]
    step_up = {**tenant_headers, "X-Step-Up-Token": generate_step_up_token(tenant)}
    assert engine.post(f"/r6/actions/{action_id}/commit", headers=step_up).status_code == 202

    # 2. The person finds it — the list is the engine's answer, via the relay.
    page = c.get(f"/agents/{agent}/approvals")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert f"/review/{agent}/{action_id}" in html
    assert "Correction to your health record" in html
    assert CANARY not in html

    # 3. Opens the review page through the relay: the change list, not the record.
    review = c.get(f"/review/{agent}/{action_id}")
    assert review.status_code == 200, review.get_data(as_text=True)
    rhtml = review.get_data(as_text=True)
    assert "Condition.clinicalStatus.coding[0].code" in rhtml
    assert f"/review/{agent}/{action_id}/submit" in rhtml
    assert CANARY not in rhtml

    # 4. Approves. The relay records the review, mints the action-bound
    #    credential server-side and confirms; the executor runs once.
    approve = c.post(f"/review/{agent}/{action_id}/submit", json={"ack": "true"})
    assert approve.status_code == 200, approve.get_data(as_text=True)
    assert approve.get_json().get("confirmed") is True

    # 5. Truthful outcome, record changed exactly once, list empty, reopen 404.
    status = c.get(f"/api/form/{action_id}?agent={agent}").get_json()
    assert status["status"] == "completed"
    got = engine.get("/r6/fhir/Condition/c-chain", headers=tenant_headers).get_json()
    assert got["clinicalStatus"]["coding"][0]["code"] == "resolved"
    assert got["meta"]["versionId"] == "2"
    assert "Nothing is waiting" in c.get(f"/agents/{agent}/approvals").get_data(as_text=True)
    assert c.get(f"/review/{agent}/{action_id}").status_code == 404

    # 6. CareAgents stored none of it.
    # Dialect-neutral table list: svc follows CARE_TEST_DATABASE_URL, so on
    # the Postgres lane there is no sqlite_master to read (#232).
    from sqlalchemy import inspect, text
    with svc.engine.connect() as cx:
        for table in inspect(svc.engine).get_table_names():
            for row in cx.execute(text(f'select * from "{table}"')).fetchall():
                blob = json.dumps([str(v) for v in row])
                assert CANARY not in blob, table
                assert action_id not in blob, table
