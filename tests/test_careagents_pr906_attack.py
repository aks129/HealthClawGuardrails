"""Security attack pins for #906: real records stay off the Mac relay and
Telegram.

The PR's own tests make an agent "real" by rewriting Connection.kind in
place, which keeps the tenant. A person changes records through
POST /api/agents/<id>/connection, which moves the agent to another
connection and therefore another tenant. These tests drive that route,
swap run ids across agents and accounts, and try the keywords and the
kind edge cases.

Synthetic data only: 555 numbers, example.com accounts, canary strings.
"""

from __future__ import annotations

import pytest

from careagents import sendblue_surface, tester_terms
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _login, cfg, svc)
from tests.test_careagents_imessage import (
    HDRS, OTHER_EMAIL, PHONE, _acct_id, _inbound, _pair, _run)

CANARY = "Zzyzx Canarywood"            # a fixture name; never real
OTHER_PHONE = "+15550100999"


def _poll(c, run_id, handle=PHONE):
    return c.get(f"/api/surfaces/imessage/runs/{run_id}", headers=HDRS,
                 query_string={"handle": handle})


def _real_connection(svc, hc, email, kind="fasten"):  # noqa: F811
    """A consented non-sample connection on its own tenant, as a finished
    Fasten connect leaves it."""
    tenant = hc.new_tenant_id()
    cid = svc.add_connection(_acct_id(svc, email), kind, tenant, "Real",
                             status="active", provider="your doctor",
                             consent_version=tester_terms.CONSENT_VERSION)
    return cid, tenant


def _finished_run(hc, tenant, agent_id, text, surface="web"):
    """A finished run on `tenant` for `agent_id` whose answer is `text`."""
    conv = hc.conversation_id(agent_id)
    _, msg = hc.claim_inbound_message(tenant, "q", agent_id, conv, surface,
                                      f"req-{len(hc.runs)}")
    run = hc.create_agent_run(tenant, msg)
    hc.runs[run["id"]]["status"] = "completed"
    hc._append_run_event(run["id"], "agent.text", {"text": text})
    return run["id"]


# --- the realistic switch: move the agent, which changes the tenant -------

def test_move_to_real_then_relay_inbound_queues_nothing(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    cid, _ = _real_connection(svc, hc, "gene@example.com")
    assert c.post(f"/api/agents/{agent_id}/connection",
                  json={"connection_id": cid}).status_code == 200
    before = len(hc.runs)
    r = _inbound(c, PHONE, "what are my meds?")
    assert r.get_json() == {
        "reply": sendblue_surface.real_records_text(cfg.origin)}
    assert len(hc.runs) == before


def test_a_run_queued_on_sample_is_not_answered_after_a_move_to_real(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                         reply="sample words")
    _pair(c, agent_id)
    run_id = _inbound(c, PHONE, "hello").get_json()["run_id"]
    cid, _ = _real_connection(svc, hc, "gene@example.com")
    c.post(f"/api/agents/{agent_id}/connection", json={"connection_id": cid})
    # The worker refuses a run whose tenant is no longer the agent's.
    _run(app)
    r = _poll(c, run_id)
    assert r.status_code == 404
    assert "reply" not in r.get_json()


def test_a_real_tenant_run_is_not_texted_after_a_move_back_to_sample(
        cfg, svc, monkeypatch):  # noqa: F811
    """The direction the PR's _is_real cannot see: the agent is on the
    sample now, the run id names an answer drawn from real records."""
    app, c, hc, agent_id, sample_tenant, sample_conn = _chat_app(
        cfg, svc, monkeypatch)
    _pair(c, agent_id)
    cid, real_tenant = _real_connection(svc, hc, "gene@example.com")
    c.post(f"/api/agents/{agent_id}/connection", json={"connection_id": cid})
    real_run = _finished_run(hc, real_tenant, agent_id, f"Dr. {CANARY}")
    c.post(f"/api/agents/{agent_id}/connection",
           json={"connection_id": sample_conn})
    r = _poll(c, real_run)
    assert r.status_code == 404
    assert CANARY not in r.get_data(as_text=True)


# --- run ids from another agent or account (V6) ----------------------------

def test_a_run_of_another_agent_on_the_same_sample_tenant_is_refused(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, tenant, conn_id = _chat_app(cfg, svc, monkeypatch)
    other = c.post("/api/agents", json={
        "name": "Other", "persona": "calm",
        "connection_id": conn_id}).get_json()["id"]
    _pair(c, agent_id)
    run_id = _finished_run(hc, tenant, other, CANARY)
    r = _poll(c, run_id)
    assert r.status_code == 404
    assert CANARY not in r.get_data(as_text=True)


def test_a_run_of_another_account_is_refused(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    other = app.test_client()
    _login(other, svc, monkeypatch, email=OTHER_EMAIL)
    conn = other.post("/api/connections/sample").get_json()
    other_agent = other.post("/api/agents", json={
        "name": "B", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    _pair(other, other_agent, handle=OTHER_PHONE)
    b_run = _finished_run(hc, hc.tenants[-1], other_agent, CANARY,
                          surface="imessage")
    # A's phone asks for B's run; B's phone asks for nothing it lacks.
    r = _poll(c, b_run, handle=PHONE)
    assert r.status_code == 404
    assert CANARY not in r.get_data(as_text=True)
    assert _poll(c, b_run, handle=OTHER_PHONE).get_json()["reply"] == CANARY


def test_telegram_code_for_another_accounts_agent_is_refused(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    other = app.test_client()
    _login(other, svc, monkeypatch, email=OTHER_EMAIL)
    r = other.post("/api/surfaces/telegram", json={"agent_id": agent_id})
    assert r.status_code == 404
    assert "code" not in r.get_json()


# --- kinds the PR did not list --------------------------------------------

@pytest.mark.parametrize("kind", ["", "SAMPLE", "sample ", "direct"])
def test_any_kind_but_exact_sample_is_held_back_on_relay_and_telegram(
        cfg, svc, monkeypatch, kind):  # noqa: F811
    from careagents.models import Agent, Connection
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    with svc.session() as s:
        s.get(Connection, s.get(Agent, agent_id).connection_id).kind = kind
    before = len(hc.runs)
    assert _inbound(c, PHONE, "hi").get_json() == {
        "reply": sendblue_surface.real_records_text(cfg.origin)}
    assert len(hc.runs) == before
    assert c.post("/api/surfaces/telegram",
                  json={"agent_id": agent_id}).status_code == 409


# --- APPROVALS and CONNECT on a real agent ---------------------------------

def test_approvals_on_a_real_agent_says_a_count_and_link_only(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    cid, _ = _real_connection(svc, hc, "gene@example.com")
    c.post(f"/api/agents/{agent_id}/connection", json={"connection_id": cid})
    hc.pending_actions = lambda tenant: [
        {"id": "act-1", "kind": "sms", "to": CANARY,
         "status": "awaiting_confirmation", "payload": {"body": CANARY}}]
    before = len(hc.runs)
    reply = _inbound(c, PHONE, "APPROVALS").get_json()["reply"]
    assert CANARY not in reply
    assert reply.startswith("You have 1 request waiting for your OK: ")
    assert reply.endswith(f"/agents/{agent_id}/approvals")
    assert len(hc.runs) == before
    connect = _inbound(c, PHONE, "CONNECT").get_json()["reply"]
    assert CANARY not in connect and len(hc.runs) == before


# --- Telegram after a move ---------------------------------------------------

def test_a_telegram_chat_bound_on_sample_stays_on_the_sample_tenant(
        cfg, svc, monkeypatch):  # noqa: F811
    """The engine holds chat -> tenant from bind time; moving the agent
    rebinds nothing. The CareAgents surface row is left active, though,
    on an agent that now reads real records."""
    from careagents.models import Surface
    app, c, hc, agent_id, sample_tenant, _ = _chat_app(cfg, svc, monkeypatch)
    code = c.post("/api/surfaces/telegram",
                  json={"agent_id": agent_id}).get_json()["code"]
    assert app.test_client().post(
        "/api/surfaces/telegram/bind",
        json={"code": f"care_{code}", "chat_id": 4242},
        headers={"X-Internal-Secret": cfg.mint_secret}).status_code == 200
    cid, real_tenant = _real_connection(svc, hc, "gene@example.com")
    c.post(f"/api/agents/{agent_id}/connection", json={"connection_id": cid})
    assert hc.bound == [(sample_tenant, 4242)]
    assert all(t != real_tenant for t, _ in hc.bound)
    # A second mint after the move is refused.
    assert c.post("/api/surfaces/telegram",
                  json={"agent_id": agent_id}).status_code == 409
    with svc.session() as s:
        row = s.query(Surface).filter_by(agent_id=agent_id,
                                         kind="telegram").one()
        assert row.status == "active"       # finding: not swept (see report)


def test_telegram_bind_rejects_a_bad_secret_and_replay(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    code = c.post("/api/surfaces/telegram",
                  json={"agent_id": agent_id}).get_json()["code"]
    raw = app.test_client()
    assert raw.post("/api/surfaces/telegram/bind",
                    json={"code": code, "chat_id": 1},
                    headers={"X-Internal-Secret": "nope"}).status_code == 403
    ok = raw.post("/api/surfaces/telegram/bind",
                  json={"code": code, "chat_id": 1},
                  headers={"X-Internal-Secret": cfg.mint_secret})
    assert ok.status_code == 200
    again = raw.post("/api/surfaces/telegram/bind",
                     json={"code": code, "chat_id": 2},
                     headers={"X-Internal-Secret": cfg.mint_secret})
    assert again.status_code == 404
    assert [chat for _, chat in hc.bound] == [1]
