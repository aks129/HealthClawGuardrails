"""Rename, Change records and Delete (calm hub spec section 3, 5, 9)."""

from __future__ import annotations

import pytest

from careagents.accounts import AuthError
from careagents.models import Agent, Surface
from tests.test_careagents import FakeClient, _login, _make_account
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

#: The CareAgents fixtures, re-exported under their own names.
cfg = _cfg_fixture
svc = _svc_fixture


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def app(cfg, svc, fake):
    from careagents.app import create_app
    a = create_app(config=cfg, client=fake, accounts=svc)
    a.config["TESTING"] = True
    return a


def _person(app, svc, monkeypatch, email):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    with c.session_transaction() as s:
        aid = s["account_id"]
    conn = svc.add_connection(aid, "direct", f"ca-{email[:4]}", "Uploaded",
                              status="active")
    agent = svc.create_agent(aid, "Juniper", "calm", conn)
    return c, aid, conn, agent


def _connection_of(svc, agent_id):
    with svc.session() as s:
        return s.get(Agent, agent_id).connection_id


def test_change_records_moves_to_another_active_connection(
        app, svc, monkeypatch):
    c, aid, conn, agent = _person(app, svc, monkeypatch, "ann@example.com")
    other = svc.add_connection(aid, "sample", "ca-ann-s", "Sample records")
    r = c.post(f"/api/agents/{agent}/connection",
               json={"connection_id": other})
    assert r.status_code == 200
    assert _connection_of(svc, agent) == other


def test_change_records_refuses_another_accounts_connection(
        app, svc, monkeypatch):
    c, _, conn, agent = _person(app, svc, monkeypatch, "ann@example.com")
    _, _, bobs_conn, _ = _person(app, svc, monkeypatch, "bob@example.com")
    r = c.post(f"/api/agents/{agent}/connection",
               json={"connection_id": bobs_conn})
    assert r.status_code == 404
    assert _connection_of(svc, agent) == conn


def test_change_records_refuses_a_revoked_connection(app, svc, monkeypatch):
    c, aid, conn, agent = _person(app, svc, monkeypatch, "ann@example.com")
    gone = svc.add_connection(aid, "direct", "ca-gone", "Old", status="active")
    svc.revoke_connection(aid, gone)
    r = c.post(f"/api/agents/{agent}/connection",
               json={"connection_id": gone})
    assert r.status_code == 404
    assert _connection_of(svc, agent) == conn


def test_change_records_refuses_another_accounts_agent(app, svc, monkeypatch):
    c, _, conn, _ = _person(app, svc, monkeypatch, "ann@example.com")
    _, _, _, bobs_agent = _person(app, svc, monkeypatch, "bob@example.com")
    r = c.post(f"/api/agents/{bobs_agent}/connection",
               json={"connection_id": conn})
    assert r.status_code == 404


def test_move_agent_checks_ownership_in_the_service(svc, monkeypatch):
    """MUTATION M3: drop account_id from move_agent's connection lookup
    -> red."""
    ann = _make_account(svc, monkeypatch, "ann2@example.com")
    bob = _make_account(svc, monkeypatch, "bob2@example.com")
    mine = svc.add_connection(ann.id, "direct", "ca-a2", "Mine",
                              status="active")
    theirs = svc.add_connection(bob.id, "direct", "ca-b2", "Theirs",
                                status="active")
    agent = svc.create_agent(ann.id, "Juniper", "calm", mine)
    with pytest.raises(AuthError):
        svc.move_agent(ann.id, agent, theirs)
    assert _connection_of(svc, agent) == mine


def test_rename_is_owner_only_and_refuses_a_blank_name(app, svc, monkeypatch):
    c, _, _, agent = _person(app, svc, monkeypatch, "ann@example.com")
    bob, _, _, _ = _person(app, svc, monkeypatch, "bob@example.com")
    assert c.post(f"/api/agents/{agent}/rename",
                  json={"name": "  "}).status_code == 400
    assert bob.post(f"/api/agents/{agent}/rename",
                    json={"name": "Mine now"}).status_code == 404
    r = c.post(f"/api/agents/{agent}/rename", json={"name": "Ada"})
    assert r.status_code == 200 and r.get_json()["name"] == "Ada"
    with svc.session() as s:
        assert s.get(Agent, agent).name == "Ada"


def test_delete_removes_the_agent_only(app, svc, fake, monkeypatch):
    c, aid, conn, agent = _person(app, svc, monkeypatch, "ann@example.com")
    keep = svc.create_agent(aid, "Coach", "calm", conn)
    svc.add_surface(aid, agent, "imessage", "code-1")
    bob, _, _, _ = _person(app, svc, monkeypatch, "bob@example.com")
    assert bob.delete(f"/api/agents/{agent}").status_code == 404
    assert c.delete(f"/api/agents/{agent}").status_code == 200
    with svc.session() as s:
        assert s.get(Agent, agent) is None
        assert s.get(Agent, keep) is not None
        assert s.query(Surface).filter_by(agent_id=agent).count() == 0
    # The conversation stays in HealthClaw with the connection.
    assert fake.purged == []
    assert svc.get_connection(aid, conn) is not None
