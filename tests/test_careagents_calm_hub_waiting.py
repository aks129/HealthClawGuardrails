"""Waiting for you (calm hub spec section 3 and 9)."""

from __future__ import annotations

import pytest

from careagents.healthclaw import HealthClawError
from tests.test_careagents import FakeClient, _login
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


def test_the_count_matches_the_approvals_page(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    r = c.get("/api/approvals/count")
    assert r.status_code == 200
    page = c.get(f"/agents/{agent}/approvals").get_data(as_text=True)
    assert r.get_json()["count"] == page.count(f'href="/review/{agent}/')
    assert r.get_json()["href"] == f"/agents/{agent}/approvals"


def test_two_agents_on_one_connection_count_its_requests_once(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    body = c.post("/api/connections/sample").get_json()
    c.post("/api/agents", json={"name": "Coach", "connection_id": body["id"]})
    assert c.get("/api/approvals/count").get_json()["count"] == 1


def test_no_assistant_is_an_honest_zero(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert c.get("/api/approvals/count").get_json() == {"count": 0}


def test_a_revoked_connection_is_not_counted(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    c.post(f"/api/connections/{conn}/disconnect")
    assert c.get("/api/approvals/count").get_json() == {"count": 0}


def test_a_failed_count_is_503_with_no_count(app, svc, fake, monkeypatch):
    """An unanswered question is not an empty inbox (#215)."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    c.post("/api/connections/sample")

    def _down(tenant):
        raise HealthClawError("down", 503)
    monkeypatch.setattr(fake, "pending_actions", _down)

    r = c.get("/api/approvals/count")
    assert r.status_code == 503
    assert "count" not in r.get_json()
