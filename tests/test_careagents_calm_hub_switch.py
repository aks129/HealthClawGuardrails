"""The hub asks once (calm hub spec section 5)."""

from __future__ import annotations

from careagents.models import Agent
from tests.test_careagents import _login
from tests.test_careagents import app as _app_fixture
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

#: The CareAgents fixtures, re-exported under their own names.
app = _app_fixture
cfg = _cfg_fixture
svc = _svc_fixture


def _setup(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    with c.session_transaction() as s:
        aid = s["account_id"]
    sample = c.post("/api/connections/sample").get_json()
    real = svc.add_connection(aid, "fasten", "ca-real", "Clinic",
                              status="active")
    return c, aid, sample["agent_id"], real


def test_switch_moves_the_agent_and_is_asked_once(app, svc, monkeypatch):
    c, aid, agent, real = _setup(app, svc, monkeypatch)
    assert svc.switch_prompted_at(aid) is None
    r = c.post("/api/hub/switch-prompt", json={
        "answer": "switch", "agent_id": agent, "connection_id": real})
    assert r.status_code == 200
    with svc.session() as s:
        assert s.get(Agent, agent).connection_id == real
    assert svc.switch_prompted_at(aid) is not None


def test_later_keeps_the_agent_and_stops_asking(app, svc, monkeypatch):
    c, aid, agent, real = _setup(app, svc, monkeypatch)
    before = svc.get_agent_context(aid, agent)["connection"]["id"]
    r = c.post("/api/hub/switch-prompt", json={"answer": "later"})
    assert r.status_code == 200
    assert svc.get_agent_context(aid, agent)["connection"]["id"] == before
    assert svc.switch_prompted_at(aid) is not None


def test_switch_uses_the_same_ownership_rule(app, svc, monkeypatch):
    c, aid, agent, _ = _setup(app, svc, monkeypatch)
    r = c.post("/api/hub/switch-prompt", json={
        "answer": "switch", "agent_id": agent, "connection_id": "conn_nope"})
    assert r.status_code == 404
    assert svc.switch_prompted_at(aid) is None


def test_an_unknown_answer_is_refused(app, svc, monkeypatch):
    c, aid, _, _ = _setup(app, svc, monkeypatch)
    assert c.post("/api/hub/switch-prompt",
                  json={"answer": "maybe"}).status_code == 400
    assert svc.switch_prompted_at(aid) is None
