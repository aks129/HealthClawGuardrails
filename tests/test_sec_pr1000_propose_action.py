"""Security tester's exploit tests for PR #1000 (propose_action, text-to-self).

Drives the real CareAgents app onto the real engine WSGI (the Chain harness),
with only Twilio's HTTP call faked. Passing tests pin a guard the PR relies on
that its own tests did not exercise. `xfail(strict=True)` rows pin a finding:
each goes red the day it is fixed, which is the signal to drop the marker.

Synthetic data only: 555-01xx numbers are fictional, names are canaries.
"""

from __future__ import annotations

import json
import logging

import pytest

from careagents import agent as agent_mod
from careagents import text_self
from tests.test_beta_acceptance_rows import TENANT
from tests.test_careagents import _login  # noqa: F401
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_propose_action_real_engine import (  # noqa: F401
    EMAIL, OTHER, PHONE, SECRET, _action, _audit_details, _confirmations,
    _model_proposes, _pending_sms, world)

OTHER_TENANT = "ca-sec-other"
OTHER_EMAIL = "sec-other@example.com"
CANARY = "Zebulon Canaryfield"


def _fixed(chain):
    """A text propose_action would have written: the only approvable kind."""
    return text_self.body_for(
        "visit_prep",
        chain.app.extensions["careagents_runtime"]["config"].origin)


def _submit(c, agent, action_id):
    return c.post(f"/review/{agent}/{action_id}/submit", json={"ack": "true"})


def _second_account(chain, monkeypatch):
    """Account B: its own login, its own sample tenant, its own agent."""
    monkeypatch.setenv("PUBLIC_TENANTS",
                       f"test-tenant,{TENANT},{OTHER_TENANT}")
    chain.hc.new_tenant_id = lambda: OTHER_TENANT
    b = chain.app.test_client()
    _login(b, chain.svc, monkeypatch, email=OTHER_EMAIL)
    r = b.post("/api/connections/sample")
    assert r.status_code == 200, r.get_data(as_text=True)
    conn = r.get_json()["id"]
    r = b.post("/api/agents", json={"name": "B", "persona": "calm",
                                    "connection_id": conn})
    assert r.status_code == 200, r.get_data(as_text=True)
    return b, r.get_json()["id"]


# --- V6: another account's ids ----------------------------------------------

def test_another_account_cannot_see_or_approve_the_text(world, monkeypatch):  # noqa: F811
    chain, _a, twilio = world
    action_id = chain.hc.propose_sms(TENANT, PHONE, "CareAgents: synthetic")
    b, b_agent = _second_account(chain, monkeypatch)
    # Bind B to a number of its own, so the number rule is not what refuses.
    from careagents.models import Account
    with chain.svc.session() as s:
        b_id = s.query(Account).filter_by(email=OTHER_EMAIL).one().id
    assert chain.svc.get_agent_context(b_id, b_agent)["tenant"] == \
        OTHER_TENANT
    assert chain.svc.bind_imessage_handle(b_id, OTHER) == "connected"
    # And A's number is not B's to take.
    assert chain.svc.bind_imessage_handle(b_id, PHONE) == "taken"

    for agent in (chain.agent, b_agent):
        assert b.get(f"/review/{agent}/{action_id}").status_code == 404
        assert _submit(b, agent, action_id).status_code == 404
        assert b.post(f"/review/{agent}/{action_id}/decline",
                      json={}).status_code == 404
    assert _confirmations(chain, action_id) == 0
    assert _action(chain, action_id)["status"] == "awaiting_confirmation"
    assert twilio.sent == []


# --- V3: replay / double submit ---------------------------------------------

def test_a_second_submit_sends_nothing_more(world, monkeypatch):  # noqa: F811
    chain, c, twilio = world
    action_id = chain.hc.propose_sms(TENANT, PHONE, _fixed(chain))
    first = _submit(c, chain.agent, action_id)
    assert first.status_code == 200 and first.get_json()["confirmed"] is True
    second = _submit(c, chain.agent, action_id)
    assert second.status_code in (404, 409), second.get_json()
    assert second.get_json().get("confirmed") is not True
    assert len(twilio.sent) == 1


# --- V3: re-link mid-flow, by a texted STOP ----------------------------------

def test_a_stop_after_propose_refuses_the_approval(world, monkeypatch):  # noqa: F811
    chain, c, twilio = world
    _model_proposes(monkeypatch)
    from tests.test_careagents import _turn
    _turn(c, chain.agent, "text me")
    [pending] = _pending_sms(chain)
    runtime_cfg = chain.app.extensions["careagents_runtime"]["config"]
    r = chain.app.test_client().post(
        "/api/surfaces/imessage/inbound",
        headers={"X-Internal-Secret": runtime_cfg.mint_secret},
        json={"handle": PHONE, "text": "STOP", "request_id": "stop-1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    page = c.get(f"/review/{chain.agent}/{pending['id']}")
    assert "data-not-your-number" in page.get_data(as_text=True)
    r = _submit(c, chain.agent, pending["id"])
    assert r.status_code == 409 and r.get_json()["error"] == "not_your_number"
    assert _confirmations(chain, pending["id"]) == 0
    assert twilio.sent == []


# --- V1: logs carry neither the number, the words nor a secret --------------

def test_the_happy_path_logs_no_number_body_or_secret(world, monkeypatch,  # noqa: F811
                                                      caplog):
    chain, c, twilio = world
    caplog.set_level(logging.DEBUG)
    _model_proposes(monkeypatch)
    from tests.test_careagents import _turn
    _turn(c, chain.agent, "text me when my visit prep is ready")
    [pending] = _pending_sms(chain)
    assert _submit(c, chain.agent, pending["id"]).get_json()["confirmed"]
    chain.engine.post(
        f"/r6/actions/callback/twilio?action_id={pending['id']}"
        f"&secret={SECRET}",
        data={"MessageSid": "SM-synthetic-1", "MessageStatus": "delivered"})
    assert len(twilio.sent) == 1
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "5555550123" not in text
    assert "visit prep is ready" not in text
    assert "synthetic-token" not in text
    assert SECRET not in text


def test_an_extra_argument_name_is_not_written_to_the_log(caplog):
    """The refusal logs the model's argument NAMES, on the reasoning that a
    value could be anything the model was fed. A name is exactly as
    model-controlled as a value: record text that says "call propose_action
    with an argument named <a patient's name and number>" puts that string
    in the application log, 40 characters of it."""
    caplog.set_level(logging.WARNING)
    events: list = []
    out = agent_mod._execute_tool(
        None, TENANT, "propose_action",
        {"purpose": "visit_prep", f"{CANARY} 555-0199": "x"}, events,
        agent_id="a", sms_phone=lambda: PHONE)
    assert json.loads(out)["error"] == "not_proposed"
    assert events == []
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert CANARY not in logged


# --- V3: what the approval-time check does not hold -------------------------

def test_a_text_with_foreign_words_to_my_number_is_not_approvable(world):  # noqa: F811
    chain, c, twilio = world
    body = "CareAgents: account locked, sign in at http://evil.example/x"
    action_id = chain.hc.propose_sms(TENANT, PHONE, body)
    r = _submit(c, chain.agent, action_id)
    assert twilio.sent == [], (r.status_code, twilio.sent)


def test_with_the_flag_off_no_text_is_approvable(world):  # noqa: F811
    chain, c, twilio = world
    chain.app.extensions["careagents_runtime"]["config"].actions_sms = "off"
    action_id = chain.hc.propose_sms(TENANT, PHONE,
                                     text_self.body_for("reminder", ""))
    _submit(c, chain.agent, action_id)
    assert twilio.sent == []


# --- V1/V2: audit detail when `to` is not CareAgents' label ------------------

@pytest.mark.xfail(strict=True, reason=(
    "pre-existing, not this PR: summary() copies an attacker-chosen `to` "
    "into every audit detail. propose_action keeps its own `to` fixed; a "
    "raw propose does not."))
def test_a_raw_propose_cannot_put_the_number_in_audit_detail(world):  # noqa: F811
    chain, _c, _twilio = world
    r = chain.engine.post(
        "/r6/actions/propose", headers={"X-Tenant-Id": TENANT},
        json={"kind": "sms", "payload": {
            "to": f"{CANARY} {OTHER}", "phone": OTHER, "body": "hello"}})
    assert r.status_code == 201, r.get_data(as_text=True)
    details = "\n".join(_audit_details(chain))
    assert OTHER not in details and CANARY not in details


# --- forged callbacks ---------------------------------------------------------

def test_callbacks_need_the_secret_and_the_right_sid(world, monkeypatch):  # noqa: F811
    chain, c, twilio = world
    mine = chain.hc.propose_sms(TENANT, PHONE, _fixed(chain))
    assert _submit(c, chain.agent, mine).get_json()["confirmed"] is True
    other = chain.hc.propose_sms(TENANT, OTHER, "CareAgents: synthetic")
    url = "/r6/actions/callback/twilio?action_id=%s&secret=%s"
    # No secret, a wrong one: refused.
    assert chain.engine.post(
        f"/r6/actions/callback/twilio?action_id={mine}",
        data={"MessageStatus": "delivered"}).status_code == 403
    # The right secret with another message's sid: refused.
    assert chain.engine.post(url % (mine, SECRET), data={
        "MessageSid": "SM-someone-else",
        "MessageStatus": "delivered"}).status_code == 404
    # An action still awaiting approval cannot be resolved by a callback.
    r = chain.engine.post(url % (other, SECRET), data={
        "MessageSid": "SM-synthetic-1", "MessageStatus": "delivered"})
    assert r.status_code == 200
    assert _action(chain, other)["status"] == "awaiting_confirmation"
    assert _action(chain, mine)["status"] == "executing"
    assert len(twilio.sent) == 1


# --- re-attack of the fix (f4040577): near-miss texts must all refuse --------

def _stage_raw(chain, payload):
    """The MCP server's / a raw caller's path: propose, then commit."""
    r = chain.engine.post("/r6/actions/propose",
                          headers={"X-Tenant-Id": TENANT},
                          json={"kind": "sms", "payload": payload})
    assert r.status_code == 201, r.get_data(as_text=True)
    aid = r.get_json()["id"]
    r = chain.engine.post(f"/r6/actions/{aid}/commit",
                          headers=chain.hc._headers(TENANT))
    assert r.status_code == 202, r.get_data(as_text=True)
    return aid


_NEAR_MISSES = {
    "body trailing space": lambda b: {"body": b + " "},
    "body leading space": lambda b: {"body": " " + b},
    "body newline": lambda b: {"body": b + "\n"},
    "body nbsp": lambda b: {"body": b.replace(" ", " ", 1)},
    "body zero-width": lambda b: {"body": b + "​"},
    "body homoglyph": lambda b: {"body": b.replace("CareAgents",
                                                   "CаreAgents", 1)},
    "body other origin": lambda b: {"body": text_self.body_for(
        "visit_prep", "http://evil.example")},
    "body no origin": lambda b: {"body": text_self.body_for("reminder", "")},
    "body two fixed texts": lambda b: {"body": b + " " + b},
    "to capital": lambda b: {"to": "Your phone"},
    "to trailing space": lambda b: {"to": "your phone "},
    "to nbsp": lambda b: {"to": "your phone"},
    "to the number": lambda b: {"to": PHONE},
    "to missing": lambda b: {"to": None},
    "phone spaced": lambda b: {"phone": "+1 555 555 0123"},
    "phone no plus": lambda b: {"phone": "15555550123"},
    "phone trailing space": lambda b: {"phone": PHONE + " "},
    "phone fullwidth": lambda b: {"phone": "+１５５５５"
                                           "５５０１２"
                                           "３"},
}


@pytest.mark.parametrize("name", sorted(_NEAR_MISSES))
def test_a_near_miss_of_the_fixed_text_is_refused(world, name,  # noqa: F811
                                                  caplog):
    chain, c, twilio = world
    caplog.set_level(logging.DEBUG)
    payload = {"to": text_self.RECIPIENT_LABEL, "phone": PHONE,
               "body": _fixed(chain)}
    payload.update(_NEAR_MISSES[name](payload["body"]))
    payload = {k: v for k, v in payload.items() if v is not None}
    aid = _stage_raw(chain, payload)
    page = c.get(f"/review/{chain.agent}/{aid}").get_data(as_text=True)
    assert "data-not-your-number" in page
    r = _submit(c, chain.agent, aid)
    assert r.status_code == 409, (name, r.get_json())
    assert _confirmations(chain, aid) == 0
    assert twilio.sent == []
    # The refusal's log line names the action and a reason, nothing sent.
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "5555550123" not in logged and "visit prep" not in logged
    assert "evil.example" not in logged


def test_the_exact_fixed_text_to_my_number_is_approvable_by_design(
        world):  # noqa: F811
    """By design: a raw stage of exactly what propose_action would stage is
    the same text to the same phone, and still needs the person's tap."""
    chain, c, twilio = world
    aid = _stage_raw(chain, {"to": text_self.RECIPIENT_LABEL, "phone": PHONE,
                             "body": _fixed(chain)})
    assert _submit(c, chain.agent, aid).get_json()["confirmed"] is True
    [(_m, _u, data)] = twilio.sent
    assert data["To"] == PHONE and data["Body"] == _fixed(chain)
