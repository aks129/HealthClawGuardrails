"""After a terms bump, a real connection's assistant asks for the current
terms until the person accepts them, on every surface (spec 6; R7, R8)."""

from __future__ import annotations

import pytest

from careagents import beta
from careagents.models import Connection
from tests.careagents_stage1_helpers import approve_terms
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, cfg, svc)


def _real_agent(cfg, svc, monkeypatch, email="gene@example.com"):  # noqa: F811
    from careagents.app import create_app
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    conn = c.post("/api/connections/fasten", json={"consent": True}).get_json()
    svc.set_connection_status(fake.tenants[-1], "active")
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    return c, fake, agent_id, conn["id"]


def _imessage_turn(fake, agent_id, request_id):
    tenant = fake.tenants[-1]          # minted by the Fasten connect
    _, mid = fake.claim_inbound_message(
        tenant, "hi", agent_id, fake.conversation_id(agent_id),
        "imessage", request_id)
    return fake.create_agent_run(tenant, mid)["id"]


def _texts(fake, run_id):
    return [e["payload"].get("text") for e in fake.events[run_id]
            if e["type"] == "agent.text"]


def test_turn_block_asks_for_terms_only_on_a_stale_real_connection():
    v = "2026-10-01"
    assert beta.turn_block({"kind": "sample", "consent_version": None},
                           False, v) is None
    assert beta.turn_block({"kind": "fasten", "consent_version": v},
                           False, v) is None
    for stale in (None, "2026-08-01"):
        assert beta.turn_block({"kind": "fasten", "consent_version": stale},
                               False, v) == beta.TERMS_TEXT
    # an unknown kind fails closed
    assert beta.turn_block({"kind": "legacy", "consent_version": None},
                           False, v) == beta.TERMS_TEXT
    # paused wins
    assert beta.turn_block({"kind": "fasten", "consent_version": None},
                           True, v) == beta.PAUSED_TEXT


def test_a_worker_turn_after_the_bump_answers_the_terms_sentence(
        cfg, svc, monkeypatch):  # noqa: F811
    """The worker path, so iMessage and Telegram get it too."""
    from careagents.worker import RunWorker
    c, fake, agent_id, conn_id = _real_agent(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    run_id = _imessage_turn(fake, agent_id, "stale-1")
    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: pytest.fail("stale consent hit a model"))
    monkeypatch.setattr(fake, "recent_messages",
                        lambda *a, **k: pytest.fail("stale consent read records"))
    RunWorker(cfg, fake, svc, "terms-worker").run_once()
    assert _texts(fake, run_id) == [beta.TERMS_TEXT]


def test_accepting_again_lets_the_next_turn_reach_the_model(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents import agent as agent_mod
    from careagents.worker import RunWorker

    class _Turn:
        text, tool_calls, raw_tool_calls = "answered", [], []
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: _Turn())
    c, fake, agent_id, conn_id = _real_agent(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    assert c.post(f"/api/connections/{conn_id}/consent",
                  json={"consent": True}).status_code == 200
    run_id = _imessage_turn(fake, agent_id, "fresh-1")
    RunWorker(cfg, fake, svc, "terms-worker").run_once()
    assert _texts(fake, run_id) == ["answered"]


def test_accepting_again_restamps_the_connection(cfg, svc, monkeypatch):  # noqa: F811
    c, fake, agent_id, conn_id = _real_agent(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    for body in ({}, {"consent": "true"}, {"consent": 1}):
        r = c.post(f"/api/connections/{conn_id}/consent", json=body)
        assert r.status_code == 428
        assert r.get_json()["consent_version"] == "2026-10-01"
    r = c.post(f"/api/connections/{conn_id}/consent", json={"consent": True})
    assert r.status_code == 200
    assert r.get_json() == {"consent_version": "2026-10-01"}
    with svc.session() as s:
        assert s.get(Connection, conn_id).consent_version == "2026-10-01"


def test_another_accounts_connection_is_a_404(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.app import create_app
    c, fake, agent_id, conn_id = _real_agent(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    other_app = create_app(config=cfg, client=fake, accounts=svc)
    other_app.config["TESTING"] = True
    other = other_app.test_client()
    _login(other, svc, monkeypatch, email="other@example.com")
    assert other.post(f"/api/connections/{conn_id}/consent",
                      json={"consent": True}).status_code == 404
    with svc.session() as s:
        assert s.get(Connection, conn_id).consent_version == "2026-08-01"
    assert svc.record_consent("acct_nobody", conn_id, "2026-10-01") is False


def test_a_sample_connection_is_not_reconsented(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    sample = c.post("/api/connections/sample").get_json()["id"]
    assert c.post(f"/api/connections/{sample}/consent",
                  json={"consent": True}).status_code == 404


def test_the_hub_lists_stale_connections_only_after_a_bump(
        cfg, svc, monkeypatch):  # noqa: F811
    c, fake, agent_id, conn_id = _real_agent(cfg, svc, monkeypatch)
    assert 'data-reconsent=' not in c.get("/home").get_data(as_text=True)
    approve_terms(monkeypatch, "2026-10-01")
    page = c.get("/home").get_data(as_text=True)
    assert f'data-reconsent="{conn_id}"' in page
    assert "Please review and accept the current terms" in page


def test_a_connection_from_before_consent_was_kept_is_asked_now(
        cfg, svc, monkeypatch):  # noqa: F811
    """R7 rollout: production holds real connections with NULL consent. They
    are asked once on deploy, before any terms change, and the sentence is
    true then too ("current terms", not "updated terms")."""
    c, fake, agent_id, conn_id = _real_agent(cfg, svc, monkeypatch)
    with svc.session() as s:
        row = s.get(Connection, conn_id)
        row.consent_version = None
        row.consented_at = None
    assert f'data-reconsent="{conn_id}"' in c.get("/home").get_data(
        as_text=True)
    assert "updated" not in beta.TERMS_TEXT
    assert "—" not in beta.TERMS_TEXT


def test_a_revoked_connection_is_not_listed(cfg, svc, monkeypatch):  # noqa: F811
    c, fake, agent_id, conn_id = _real_agent(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    assert svc.revoke_connection(_account_id(svc), conn_id) is True
    assert 'data-reconsent=' not in c.get("/home").get_data(as_text=True)


def _account_id(svc, email="gene@example.com"):  # noqa: F811
    from careagents.models import Account
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).one().id
