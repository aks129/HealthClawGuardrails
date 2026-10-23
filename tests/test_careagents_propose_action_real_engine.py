"""propose_action on the real engine: turn, pending, approve, text, callback.

tests/test_careagents_propose_action.py fakes the HealthClaw client, so it
proves what CareAgents sends, not what the engine accepts. These drive the
real HealthClawClient onto the real engine WSGI (the Chain harness of
tests/test_careagents_failed_turn_real_engine.py): the engine's propose
validation, the commit grant, the approval-token mint and its single-use
nonce, SmsExecutor.execute and the Twilio status callback. Only Twilio's
HTTP call is faked.

Synthetic data only: 555-01xx numbers are fictional.
"""

from __future__ import annotations

import json

import pytest

from careagents import text_self
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, _turn, cfg, svc)

PHONE = "+15555550123"
OTHER = "+15555550199"
EMAIL = "acceptance@example.com"
SECRET = "synthetic-webhook-secret"


class _Twilio:
    """Stands in for api.twilio.com: records each request, answers 201."""

    def __init__(self):
        self.sent = []

    def __call__(self, method, url, **kwargs):
        self.sent.append((method, url, kwargs.get("data") or {}))

        class _Resp:
            status_code = 201

            @staticmethod
            def json():
                return {"sid": "SM-synthetic-1", "status": "queued"}
        return _Resp()


@pytest.fixture
def world(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.models import Account
    chain = Chain(cfg, svc, monkeypatch)
    cfg.actions_sms = "on"
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACsynthetic")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "synthetic-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15555550100")
    monkeypatch.setenv("ACTIONS_WEBHOOK_SECRET", SECRET)
    twilio = _Twilio()
    monkeypatch.setattr("r6.actions.rails.sms._safe_request", twilio)
    with svc.session() as s:
        account_id = s.query(Account).filter_by(email=EMAIL).one().id
    assert svc.bind_imessage_handle(account_id, PHONE) == "connected"
    c = chain.app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    # A worker that has polled once, so the chat admits turns.
    from careagents.worker import RunWorker
    RunWorker(cfg, chain.hc, svc, "pa-worker").run_once()
    return chain, c, twilio


def _model_proposes(monkeypatch, purpose="visit_prep"):
    """One round that calls propose_action, then a plain answer."""
    class _Call:
        id, name, arguments = "call-1", "propose_action", {"purpose": purpose}

    class _Ask:
        text, tool_calls, raw_tool_calls = "", [_Call()], []

    class _Done:
        text, tool_calls, raw_tool_calls = "I've set that up.", [], []
    turns = iter([_Ask(), _Done()])
    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: next(turns))


def _model_says(monkeypatch, text):
    class _Done:
        tool_calls, raw_tool_calls = [], []
    _Done.text = text
    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: _Done())


def _action(chain, action_id):
    from r6.actions.models import ProposedAction
    with chain.engine_app.app_context():
        from models import db
        row = db.session.get(ProposedAction, action_id)
        return {"status": row.status, "payload": row.payload,
                "kind": row.kind}


def _confirmations(chain, action_id):
    from r6.actions.confirmations import ActionConfirmation
    with chain.engine_app.app_context():
        return ActionConfirmation.query.filter_by(action_id=action_id).count()


def _audit_details(chain):
    from r6.models import AuditEventRecord
    with chain.engine_app.app_context():
        return [r.detail or "" for r in AuditEventRecord.query.all()]


def _pending_sms(chain):
    return [a for a in chain.hc.pending_actions(TENANT)
            if a.get("kind") == "sms"]


def test_a_text_runs_from_the_turn_to_the_callback(world, monkeypatch):
    chain, c, twilio = world
    _model_proposes(monkeypatch)
    _turn(c, chain.agent, "text me when my visit prep is ready")

    [pending] = _pending_sms(chain)
    action_id = pending["id"]
    row = _action(chain, action_id)
    assert row["status"] == "awaiting_confirmation"
    assert row["payload"] == {
        "to": "your phone", "phone": PHONE,
        "body": text_self.body_for(
            "visit_prep",
            chain.app.extensions["careagents_runtime"]["config"].origin)}

    # The page offers Approve: this is the account's own number.
    page = c.get(f"/review/{chain.agent}/{action_id}").get_data(as_text=True)
    assert "data-not-your-number" not in page
    assert f"/review/{chain.agent}/{action_id}/submit" in page

    # Capture the single-use approval token the relay spends.
    real_send = chain.hc._send
    tokens = []

    def _send(method, url, **kw):
        if url.endswith(f"/{action_id}/confirm"):
            tokens.append((kw.get("headers") or {}).get("X-Step-Up-Token"))
        return real_send(method, url, **kw)
    monkeypatch.setattr(chain.hc, "_send", _send)

    r = c.post(f"/review/{chain.agent}/{action_id}/submit",
               json={"ack": "true"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["confirmed"] is True
    # The review's consent record, and the confirm's own at the claim.
    assert _confirmations(chain, action_id) >= 1
    [(method, url, data)] = twilio.sent
    assert method == "POST" and "/Messages.json" in url
    assert data["To"] == PHONE
    assert data["Body"] == row["payload"]["body"]
    assert f"action_id={action_id}" in data["StatusCallback"]
    assert _action(chain, action_id)["status"] == "executing"

    # The spent token does not approve again: nothing is texted twice.
    [token] = tokens
    again = chain.engine.post(
        f"/r6/actions/{action_id}/confirm",
        headers={"X-Tenant-Id": TENANT, "X-Step-Up-Token": token},
        json={"approved_via": "review-page"})
    assert again.status_code == 401
    assert "replay" in again.get_data(as_text=True)
    assert len(twilio.sent) == 1

    # Twilio's status callback, on the secret the executor registered.
    forged = chain.engine.post(
        f"/r6/actions/callback/twilio?action_id={action_id}&secret=wrong",
        data={"MessageSid": "SM-synthetic-1", "MessageStatus": "delivered"})
    assert forged.status_code == 403
    cb = chain.engine.post(
        f"/r6/actions/callback/twilio?action_id={action_id}&secret={SECRET}",
        data={"MessageSid": "SM-synthetic-1", "MessageStatus": "delivered"})
    assert cb.status_code == 200, cb.get_data(as_text=True)
    assert _action(chain, action_id)["status"] == "completed"

    # No audit row carries the number or the words (#: summary() copies
    # `to`, which is why `to` is a fixed label).
    details = "\n".join(_audit_details(chain))
    assert "5555550123" not in details
    assert "visit prep" not in details
    assert "your phone" in details   # the label is what the audit names


def test_a_second_text_is_not_staged_while_one_waits(world, monkeypatch):
    chain, c, _twilio = world
    _model_proposes(monkeypatch)
    _turn(c, chain.agent, "text me", request_id="first")
    _model_proposes(monkeypatch, purpose="reminder")
    _turn(c, chain.agent, "text me again", request_id="second")
    assert len(_pending_sms(chain)) == 1


def test_a_texted_yes_approves_nothing(world, monkeypatch):
    chain, c, twilio = world
    _model_proposes(monkeypatch)
    _turn(c, chain.agent, "text me", request_id="propose")
    [pending] = _pending_sms(chain)
    _model_says(monkeypatch, "Approve it on the review page.")
    _turn(c, chain.agent, "yes", request_id="yes")
    _turn(c, chain.agent, "YES approve", request_id="yes-2")
    # And as an actual text: the iMessage inbound from the bound number.
    from tests.test_careagents import _enqueue_and_run_imessage
    runtime_cfg = chain.app.extensions["careagents_runtime"]["config"]
    texted = _enqueue_and_run_imessage(
        chain.app, chain.app.test_client(),
        headers={"X-Internal-Secret": runtime_cfg.mint_secret},
        json={"handle": PHONE, "text": "YES", "request_id": "sms-yes"})
    assert texted.status_code == 200, texted.get_data(as_text=True)
    assert _action(chain, pending["id"])["status"] == "awaiting_confirmation"
    assert _confirmations(chain, pending["id"]) == 0
    assert twilio.sent == []


def test_a_text_staged_to_another_number_is_refused_at_approval(
        world, monkeypatch):
    """A raw propose (the MCP server's path, or any tenant-header POST)
    reaches the same inbox. CareAgents refuses it BEFORE the review is
    submitted, so the engine records no consent and texts no one."""
    chain, c, twilio = world
    action_id = chain.hc.propose_sms(TENANT, OTHER, "CareAgents: synthetic")
    assert _action(chain, action_id)["status"] == "awaiting_confirmation"

    page = c.get(f"/review/{chain.agent}/{action_id}").get_data(as_text=True)
    assert "data-not-your-number" in page
    assert 'id="approve-btn" class="btn btn-primary btn-lg" disabled hidden>' \
        in page

    r = c.post(f"/review/{chain.agent}/{action_id}/submit",
               json={"ack": "true"})
    assert r.status_code == 409
    assert r.get_json()["error"] == "not_your_number"
    assert _confirmations(chain, action_id) == 0
    assert _action(chain, action_id)["status"] == "awaiting_confirmation"
    assert twilio.sent == []

    # Decline still works on it.
    d = c.post(f"/review/{chain.agent}/{action_id}/decline", json={})
    assert d.status_code == 200, d.get_json()
    assert _action(chain, action_id)["status"] == "declined"
    assert twilio.sent == []


def test_a_rebind_after_propose_is_refused_at_approval(world, monkeypatch):
    chain, c, twilio = world
    from careagents.models import Account
    _model_proposes(monkeypatch)
    _turn(c, chain.agent, "text me")
    [pending] = _pending_sms(chain)
    with chain.svc.session() as s:
        account_id = s.query(Account).filter_by(email=EMAIL).one().id
    chain.svc.disconnect_imessage(account_id)
    assert chain.svc.bind_imessage_handle(account_id, OTHER) == "connected"
    r = c.post(f"/review/{chain.agent}/{pending['id']}/submit",
               json={"ack": "true"})
    assert r.status_code == 409
    assert _confirmations(chain, pending["id"]) == 0
    assert twilio.sent == []


def test_with_the_flag_off_a_forced_call_stages_nothing(world, monkeypatch):
    chain, c, _twilio = world
    chain.app.extensions["careagents_runtime"]["config"].actions_sms = "off"
    _model_proposes(monkeypatch)
    _turn(c, chain.agent, "text me")
    assert _pending_sms(chain) == []
    # The model read back a refusal, not an id.
    from r6.agent_runs.models import AgentToolCall
    with chain.engine_app.app_context():
        [call] = AgentToolCall.query.filter_by(tool_name="propose_action").all()
        result = json.loads(call.result_json or "{}")
    content = json.loads(result.get("content") or "{}")
    assert content.get("error") == "not_proposed"
