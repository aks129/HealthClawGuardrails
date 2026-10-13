"""One assistant, straight into chat (calm hub spec section 5 and 9)."""

from __future__ import annotations

import json

from careagents.healthclaw import HealthClawError
from careagents.models import Account, Agent
from tests.careagents_consent_helpers import consented
from tests.test_careagents import FakeClient, _login, _make_account
from tests.test_careagents import app as _app_fixture
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

#: The CareAgents fixtures, re-exported under their own names.
app = _app_fixture
cfg = _cfg_fixture
svc = _svc_fixture

INTAKE = "Fill out my intake form for a new doctor"


def _agents(svc, account_id):
    with svc.session() as s:
        return [(a.name, a.persona, a.advisor, a.connection_id)
                for a in s.query(Agent).filter_by(account_id=account_id)]


def _account_id(client):
    with client.session_transaction() as s:
        return s["account_id"]


def test_the_sample_lands_in_chat_with_the_intake_starter_first(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    body = c.post("/api/connections/sample").get_json()
    assert body["redirect"] == f"/chat?agent={body['agent_id']}"
    page = c.get(body["redirect"]).get_data(as_text=True)
    assert page.count('id="starters"') == 1
    first = page.split('class="starter">', 1)[1].split("<", 1)[0]
    assert first == INTAKE


def test_the_first_active_connection_creates_exactly_one_agent(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    c.post("/api/connections/sample")
    assert _agents(svc, _account_id(c)) == [("Juniper", "calm", None, conn)]


def test_the_sample_card_has_a_count_at_connect(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    assert svc.get_connection(_account_id(c), conn)["last_count"] == 100


def test_a_second_connection_does_not_create_another_agent(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    c.post("/api/connections/sample")
    direct = c.post("/api/connections/direct",
                    json=consented()).get_json()["id"]
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Patient", "id": "p-1"}}]}
    r = c.post(f"/api/connections/{direct}/upload", data=json.dumps(bundle),
               content_type="application/fhir+json")
    assert r.status_code == 200
    assert len(_agents(svc, _account_id(c))) == 1


def test_repeated_ingest_complete_callbacks_create_one_agent(
        app, svc, monkeypatch):
    """Poll is the ingest-complete path for Fasten and runs every 5s."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    started = c.post("/api/connections/fasten", json=consented())
    tenant = started.get_json()["connect_url"].rsplit("/connect/", 1)[1]
    for _ in range(3):
        assert c.get(f"/api/connections/{tenant}/poll").status_code == 200
    agents = _agents(svc, _account_id(c))
    assert len(agents) == 1
    assert agents[0][3] == started.get_json()["id"]


def test_an_existing_account_with_agents_gets_no_new_one(svc, monkeypatch):
    acct = _make_account(svc, monkeypatch, "legacy@example.com")
    conn = svc.add_connection(acct.id, "direct", "ca-legacy", "Uploaded",
                              status="empty")
    svc.set_connection_status("ca-legacy", "active")
    svc.create_agent(acct.id, "Ada", "calm", conn)
    svc.create_agent(acct.id, "Coach", "calm", conn)
    assert svc.activate_connection("ca-legacy") == []
    assert [a[0] for a in _agents(svc, acct.id)] == ["Ada", "Coach"]


def test_a_pending_connection_creates_no_agent_and_burns_no_stamp(
        svc, monkeypatch):
    acct = _make_account(svc, monkeypatch, "pending@example.com")
    conn = svc.add_connection(acct.id, "fasten", "ca-p", "Clinic",
                              status="pending")
    assert svc.ensure_first_agent(acct.id, conn) is None
    with svc.session() as s:
        assert s.get(Account, acct.id).first_agent_at is None
    assert len(svc.activate_connection("ca-p")) == 1


def test_the_stamp_is_what_stops_a_racing_second_create(svc, monkeypatch):
    acct = _make_account(svc, monkeypatch, "race@example.com")
    conn = svc.add_connection(acct.id, "direct", "ca-r", "Uploaded")
    with svc.session() as s:
        s.get(Account, acct.id).first_agent_at = 1.0
    assert svc.ensure_first_agent(acct.id, conn) is None
    assert _agents(svc, acct.id) == []


def test_starters_render_once_when_counts_are_unknown(cfg, svc, monkeypatch):
    from careagents.app import create_app

    class _NoCounts(FakeClient):
        def search(self, tenant, resource_type, params=None):
            raise HealthClawError("down", 503)

    a = create_app(config=cfg, client=_NoCounts(), accounts=svc)
    a.config["TESTING"] = True
    c = a.test_client()
    _login(c, svc, monkeypatch)
    body = c.post("/api/connections/sample").get_json()
    page = c.get(body["redirect"]).get_data(as_text=True)
    assert page.count('id="starters"') == 1
    assert page.count(INTAKE) == 1
