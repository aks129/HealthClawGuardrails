"""A turn the worker will refuse (paused, or terms not accepted) never reaches
a model, so it does not spend the day's allowance (#856 QA review)."""

from __future__ import annotations

from careagents.models import UsageDay
from tests.careagents_stage1_helpers import approve_terms
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, _login, cfg, svc)


def _turn(c, agent_id, request_id):
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": request_id}, buffered=False)
    status = r.status_code
    r.close()
    return status


def _used(svc):  # noqa: F811
    with svc.session() as s:
        return sum(int(u.turns or 0) for u in s.query(UsageDay).all())


def test_paused_turns_do_not_use_up_the_daily_cap(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(cfg, "chat_turns_per_day", 1)
    svc.set_paused("gene@example.com", True)
    assert _turn(c, agent_id, "p-1") == 200
    assert _turn(c, agent_id, "p-2") == 200     # not a 429
    assert _used(svc) == 0
    svc.set_paused("gene@example.com", False)
    assert _turn(c, agent_id, "p-3") == 200
    assert _used(svc) == 1
    assert _turn(c, agent_id, "p-4") == 429     # the cap still holds


def test_a_turn_waiting_on_the_terms_does_not_use_the_cap(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/direct", json={"consent": True}).get_json()
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    approve_terms(monkeypatch, "2026-10-01")
    assert _turn(c, agent_id, "t-1") == 200
    assert _used(svc) == 0
