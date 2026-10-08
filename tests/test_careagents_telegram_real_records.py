"""Telegram is never bound to an agent on real records.

The OpenClaw gateway answers Telegram itself, outside the run worker, so
the turn check there does not hold it back (careagents/beta.py). The rule
is applied where CareAgents still has a say: minting a binding code and
binding a chat. Both read `sendblue_surface.real_records_blocked`, the
check the Sendblue line and the Mac relay use.

Once bound, the engine holds chat -> tenant for the connection the agent
had at bind time. Every connection gets its own tenant, so moving the
agent to real records later leaves the chat on the sample tenant.

Synthetic data only.
"""

from __future__ import annotations

import pytest

from careagents import sendblue_surface
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, app, cfg, svc)
from tests.test_careagents_sendblue import _make_real


def _agent(c):
    conn = c.post("/api/connections/sample").get_json()["id"]
    return c.post("/api/agents", json={"name": "A", "persona": "calm",
                                       "connection_id": conn}
                  ).get_json()["id"]


def _bind(app, cfg, code, chat_id=4242):  # noqa: F811
    return app.test_client().post(
        "/api/surfaces/telegram/bind",
        json={"code": f"care_{code}", "chat_id": chat_id},
        headers={"X-Internal-Secret": cfg.mint_secret})


def _engine(app):  # noqa: F811
    return app.extensions["careagents_runtime"]["client"]


@pytest.mark.parametrize("kind", ["fasten", "wearables", "somethingnew"])
def test_a_real_records_agent_gets_no_telegram_code(
        app, svc, monkeypatch, cfg, kind):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    agent_id = _agent(c)
    _make_real(svc, agent_id, kind)
    r = c.post("/api/surfaces/telegram", json={"agent_id": agent_id})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "real_records"
    assert body["message"] == sendblue_surface.real_records_text(cfg.origin)
    assert "code" not in body and "deep_link" not in body
    from careagents.models import Surface
    with svc.session() as s:
        assert s.query(Surface).filter_by(agent_id=agent_id,
                                          kind="telegram").count() == 0


def test_a_sample_agent_still_binds_to_telegram(
        app, svc, monkeypatch, cfg):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    agent_id = _agent(c)
    r = c.post("/api/surfaces/telegram", json={"agent_id": agent_id})
    assert r.status_code == 200
    assert _bind(app, cfg, r.get_json()["code"]).status_code == 200
    assert [chat for _, chat in _engine(app).bound] == [4242]


def test_a_code_minted_while_sample_binds_nothing_after_a_switch(
        app, svc, monkeypatch, cfg):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    agent_id = _agent(c)
    code = c.post("/api/surfaces/telegram",
                  json={"agent_id": agent_id}).get_json()["code"]
    _make_real(svc, agent_id)                   # real records, before /start
    r = _bind(app, cfg, code)
    assert r.status_code == 409
    assert r.get_json()["error"] == "real_records"
    assert _engine(app).bound == []
    # Not marked bound on our side either.
    assert svc.find_surface_by_code(code)["status"] == "pending"
