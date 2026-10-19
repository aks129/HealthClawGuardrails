"""propose_action: a fixed text to the person's own verified phone.

Design: the CTO's propose_action text-to-self note, slices 1 and 2. The
model picks a purpose and nothing else; the number is the account's own
verified iMessage number; only the review page approves, and it checks the
number again BEFORE the review is submitted, because that submit is what
writes the consent record.

These use FakeClient, so they prove what CareAgents sends and refuses, not
what the engine accepts. The engine end is
tests/test_careagents_propose_action_real_engine.py. Synthetic data only:
555-01xx numbers are fictional.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from careagents import text_self
from careagents.agent import (PROPOSE_ACTION_TOOL, TOOLS, _execute_tool,
                              tools_for_turn)
from careagents.config import Config, ConfigError
from careagents.models import Account, Surface
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, cfg, svc)

PHONE = "+15555550123"
OTHER = "+15555550199"
EMAIL = "gene@example.com"


def _account_id(svc, email=EMAIL):  # noqa: F811
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).one().id


def _app(cfg, svc, monkeypatch, fake=None, *, flag="on"):  # noqa: F811
    from careagents.app import create_app
    cfg.actions_sms = flag
    fake = fake or FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    conn = c.post("/api/connections/sample").get_json()
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    return c, fake, agent_id


def _bind(svc, handle=PHONE, email=EMAIL):  # noqa: F811
    assert svc.bind_imessage_handle(_account_id(svc, email), handle) in (
        "connected", "confirmed")


# --- the flag -----------------------------------------------------------------

def test_the_flag_defaults_off():
    """MUTATION: default CARE_ACTIONS_SMS to `on` -> red."""
    c = Config(env={"CARE_DATABASE_URL": "sqlite:///:memory:"})
    assert c.actions_sms == "off"
    assert c.actions_sms_open_for(EMAIL) is False


def test_the_flag_refuses_a_value_it_does_not_know():
    with pytest.raises(ConfigError):
        Config(env={"CARE_DATABASE_URL": "sqlite:///:memory:",
                    "CARE_ACTIONS_SMS": "yes"})


def test_the_allowlist_is_read_only_in_allowlist_mode():
    env = {"CARE_DATABASE_URL": "sqlite:///:memory:",
           "CARE_ACTIONS_SMS_ALLOWLIST": " Gene@Example.com ,x@example.com"}
    assert not Config(env=env).actions_sms_open_for(EMAIL)
    c = Config(env=env | {"CARE_ACTIONS_SMS": "allowlist"})
    assert c.actions_sms_open_for("GENE@example.com")
    assert not c.actions_sms_open_for("mallory@example.com")
    assert not c.actions_sms_open_for("")


# --- the number ---------------------------------------------------------------

def test_the_number_is_the_accounts_own_verified_phone(cfg, svc, monkeypatch):  # noqa: F811
    _app(cfg, svc, monkeypatch)
    aid = _account_id(svc)
    assert text_self.sms_phone_for(cfg, svc, aid, EMAIL) is None  # no surface
    _bind(svc)
    assert text_self.sms_phone_for(cfg, svc, aid, EMAIL) == PHONE


@pytest.mark.parametrize("flag,allow", [("off", ""), ("allowlist", ""),
                                        ("allowlist", "x@example.com")])
def test_no_number_while_the_flag_is_closed_for_the_account(
        cfg, svc, monkeypatch, flag, allow):  # noqa: F811
    _app(cfg, svc, monkeypatch, flag=flag)
    cfg.actions_sms_allowlist = frozenset(filter(None, [allow]))
    _bind(svc)
    assert text_self.sms_phone_for(cfg, svc, _account_id(svc), EMAIL) is None


def test_an_apple_id_email_is_not_a_number(cfg, svc, monkeypatch):  # noqa: F811
    _app(cfg, svc, monkeypatch)
    _bind(svc, handle="gene.icloud@example.com")
    assert text_self.sms_phone_for(cfg, svc, _account_id(svc), EMAIL) is None


def test_a_binding_due_to_reconfirm_is_not_a_number(cfg, svc, monkeypatch):  # noqa: F811
    _app(cfg, svc, monkeypatch)
    _bind(svc)
    old = time.time() - (cfg.imessage_reverify_days + 1) * 86400
    with svc.session() as s:
        s.query(Surface).filter_by(kind="imessage").update(
            {"verified_at": old, "bound_at": old, "last_inbound_at": old})
    assert text_self.sms_phone_for(cfg, svc, _account_id(svc), EMAIL) is None


def test_two_numbers_is_no_number(cfg, svc, monkeypatch):  # noqa: F811
    c, _fake, agent_id = _app(cfg, svc, monkeypatch)
    _bind(svc)
    aid = _account_id(svc)
    with svc.session() as s:
        s.add(Surface(account_id=aid, agent_id=agent_id, kind="imessage",
                      handle=OTHER, status="active", bound_at=time.time(),
                      verified_at=time.time(), last_inbound_at=time.time()))
    assert text_self.sms_phone_for(cfg, svc, aid, EMAIL) is None


def test_a_paused_account_has_no_number(cfg, svc, monkeypatch):  # noqa: F811
    _app(cfg, svc, monkeypatch)
    _bind(svc)
    svc.set_paused(EMAIL, True)
    assert text_self.sms_phone_for(cfg, svc, _account_id(svc), EMAIL) is None


# --- the tool list, per turn ----------------------------------------------------

def test_the_tool_takes_a_purpose_and_nothing_else():
    params = PROPOSE_ACTION_TOOL["parameters"]
    assert set(params["properties"]) == {"purpose"}
    assert params["additionalProperties"] is False
    assert params["properties"]["purpose"]["enum"] == ["visit_prep",
                                                       "reminder"]
    assert PROPOSE_ACTION_TOOL not in TOOLS
    assert tools_for_turn(None) == TOOLS
    assert tools_for_turn(PHONE)[-1] is PROPOSE_ACTION_TOOL


def _offered(cfg, svc, monkeypatch, c, fake, agent_id):  # noqa: F811
    """The tool names the worker handed the model on one turn."""
    from careagents import agent as agent_mod
    from careagents.worker import RunWorker
    seen = []

    class _Done:
        text, tool_calls, raw_tool_calls = "ok", [], []

    def _complete(_cfg, _system, _history, tools):
        seen.append([t["name"] for t in tools])
        return _Done()
    monkeypatch.setattr(agent_mod.llm, "complete", _complete)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": f"offer-{len(fake.events)}"},
               buffered=False)
    r.close()
    RunWorker(cfg, fake, svc, "offer-worker").run_once()
    assert seen, "the worker never asked the model"
    return seen[0]


def test_the_turn_offers_the_tool_only_with_a_number(cfg, svc, monkeypatch):  # noqa: F811
    c, fake, agent_id = _app(cfg, svc, monkeypatch)
    assert "propose_action" not in _offered(cfg, svc, monkeypatch, c, fake,
                                            agent_id)
    _bind(svc)
    assert "propose_action" in _offered(cfg, svc, monkeypatch, c, fake,
                                        agent_id)


def test_the_turn_does_not_offer_the_tool_with_the_flag_off(
        cfg, svc, monkeypatch):  # noqa: F811
    c, fake, agent_id = _app(cfg, svc, monkeypatch, flag="off")
    _bind(svc)
    assert "propose_action" not in _offered(cfg, svc, monkeypatch, c, fake,
                                            agent_id)


# --- the dispatcher -------------------------------------------------------------

def _call(fake, args, phone=PHONE, origin="https://care.example"):
    events = []
    out = json.loads(_execute_tool(
        fake, "ca-t", "propose_action", args, events, agent_id="agent_1",
        origin=origin, sms_phone=(lambda: phone)))
    return out, events


def test_a_forced_call_without_a_number_stages_nothing():
    """The model called a tool it was not offered.

    MUTATION: drop the dispatcher's number check -> red."""
    fake = FakeClient()
    out, events = _call(fake, {"purpose": "visit_prep"}, phone=None)
    assert out["error"] == "not_proposed"
    assert not getattr(fake, "proposed_sms", [])
    assert events == []
    out = json.loads(_execute_tool(fake, "ca-t", "propose_action",
                                   {"purpose": "visit_prep"}, []))
    assert out["error"] == "not_proposed"
    assert not getattr(fake, "proposed_sms", [])


@pytest.mark.parametrize("extra", [
    {"phone": OTHER}, {"to": OTHER}, {"body": "Jane Roe's A1c is 9.1"},
    {"number": OTHER}])
def test_an_extra_argument_refuses_the_call_and_logs_its_name_only(
        caplog, extra):
    """MUTATION: pass `phone` through from the arguments -> red."""
    fake = FakeClient()
    caplog.set_level(logging.WARNING, logger="careagents.agent")
    out, events = _call(fake, {"purpose": "reminder", **extra})
    assert out["error"] == "not_proposed"
    assert not getattr(fake, "proposed_sms", [])
    assert events == []
    [key] = extra
    assert key in caplog.text
    assert extra[key] not in caplog.text


def test_an_unknown_purpose_refuses_the_call():
    fake = FakeClient()
    out, _ = _call(fake, {"purpose": "text my doctor"})
    assert out["error"] == "not_proposed"
    out, _ = _call(fake, {})
    assert out["error"] == "not_proposed"
    out, _ = _call(fake, ["visit_prep"])
    assert out["error"] == "not_proposed"
    assert not getattr(fake, "proposed_sms", [])


def test_the_text_is_ours_and_goes_to_the_verified_number():
    """The number is the one the account verified, the words are the fixed
    template, and nothing the model wrote reaches either."""
    fake = FakeClient()
    out, events = _call(fake, {"purpose": "visit_prep"})
    [sent] = fake.proposed_sms
    assert sent["phone"] == PHONE
    assert sent["body"] == text_self.body_for("visit_prep",
                                              "https://care.example")
    assert sent["body"] == ("CareAgents: your visit prep is ready. Sign in "
                            "to read it: https://care.example/chat. Reply "
                            "STOP to opt out.")
    assert out["status"] == "awaiting_confirmation"
    assert text_self.APPROVAL_NOTE in out["note"]
    # The model reads back neither the number nor the words.
    assert PHONE not in json.dumps(out) and "visit prep" not in json.dumps(out)
    [card] = events
    assert card == {"type": "card", "kind": "review",
                    "action_id": "act-sms", "label": "Text message",
                    "review_url": "/review/agent_1/act-sms"}


def test_one_waiting_text_at_a_time():
    class _Fake(FakeClient):
        def pending_actions(self, tenant):
            return [{"id": "act-1", "kind": "form-fill"},
                    {"id": "act-0", "kind": "sms", "to": "your phone"}]
    fake = _Fake()
    out, events = _call(fake, {"purpose": "reminder"})
    assert out["error"] == "not_proposed"
    assert not getattr(fake, "proposed_sms", [])


def test_a_waiting_intake_form_does_not_block_a_text():
    fake = FakeClient()      # pending_actions: one form-fill
    out, _ = _call(fake, {"purpose": "reminder"})
    assert out["action_id"] == "act-sms"


def test_the_client_sends_a_fixed_recipient_label_never_the_number():
    """The engine copies `to` into every audit detail and push.

    MUTATION: set `to` to the number -> red."""
    from careagents.healthclaw import HealthClawClient
    sent = []

    class _R:
        def __init__(self, status, body):
            self.status_code, self._body = status, body
            self.ok = status < 400
            self.headers = {"Content-Type": "application/json"}
            self.content = json.dumps(body).encode()
            self.text = json.dumps(body)

        def json(self):
            return self._body

    class _Http:
        def post(self, url, json=None, headers=None, timeout=None, data=None):
            sent.append((url, json))
            if url.endswith("/propose"):
                return _R(201, {"id": "act-9"})
            return _R(202, {"id": "act-9"})

    hc = HealthClawClient(base="http://engine", mint_secret="m")
    hc.http = _Http()
    hc.mint_token = lambda tenant: "tok"
    assert hc.propose_sms("ca-t", PHONE, "the body") == "act-9"
    (url, body), (commit_url, _) = sent
    assert url.endswith("/r6/actions/propose")
    assert commit_url.endswith("/r6/actions/act-9/commit")
    assert body == {"kind": "sms", "payload": {
        "to": "your phone", "phone": PHONE, "body": "the body"}}


def test_text_surfaces_name_the_text_and_the_page_not_a_form():
    from careagents.imessage import run_reply
    events = [{"type": "agent.card", "payload": {
        "type": "card", "kind": "review", "action_id": "act-sms",
        "label": "Text message"}}]
    reply = run_reply(events, "https://care.example", "agent_1")
    assert "form" not in reply
    assert "Only the review page approves it" in reply
    assert "https://care.example/review/agent_1/act-sms" in reply


# --- the approval check (slice 2) ------------------------------------------------

class _SmsFake(FakeClient):
    """An sms waiting for review, to `phone`. Records every review POST."""

    def __init__(self, phone):
        super().__init__()
        self.phone = phone
        self.reviews, self.confirms, self.declines = [], [], []

    def action_status(self, tenant, action_id):
        if action_id != "act-sms":
            raise __import__("careagents.healthclaw").healthclaw \
                .HealthClawError("not found", 404)
        return {"id": "act-sms", "kind": "sms", "to": "your phone",
                "status": "awaiting_confirmation",
                "payload": {"to": "your phone", "phone": self.phone,
                            "body": "CareAgents: ..."}}

    def submit_review(self, tenant, action_id, decisions):
        self.reviews.append(action_id)
        return 200, {"status": "awaiting_confirmation"}

    def confirm_action(self, tenant, action_id):
        self.confirms.append(action_id)
        return {"status": "executing"}

    def decline_action(self, tenant, action_id):
        self.declines.append(action_id)
        return 200, {"id": action_id, "status": "declined"}

    def fetch_review_page(self, tenant, action_id, voice=None):
        from flask import render_template
        from main import create_app as engine_app
        app = engine_app({"TESTING": True,
                          "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                          "LEGACY_BOOT_ON_CREATE": False})
        with app.test_request_context():
            return 200, render_template(
                "action_approve.html", tenant_id=tenant, action_id=action_id,
                kind_label="Text message", body_label="Message",
                to="your phone", phone=self.phone, body="CareAgents: ...",
                fixes=[], fix_record=None, fix_version=None)


def test_a_text_to_another_number_is_refused_before_the_review_is_submitted(
        cfg, svc, monkeypatch):  # noqa: F811
    """MUTATION: delete the approval check -> red (submit_review is called
    and the consent record would be written)."""
    fake = _SmsFake(OTHER)
    c, _fake, agent = _app(cfg, svc, monkeypatch, fake)
    _bind(svc)
    r = c.post(f"/review/{agent}/act-sms/submit", json={"ack": "true"})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "not_your_number"
    assert body["confirmed"] is False
    assert fake.reviews == [] and fake.confirms == []


def test_a_text_with_no_verified_number_on_the_account_is_refused(
        cfg, svc, monkeypatch):  # noqa: F811
    fake = _SmsFake(PHONE)
    c, _fake, agent = _app(cfg, svc, monkeypatch, fake)
    r = c.post(f"/review/{agent}/act-sms/submit", json={"ack": "true"})
    assert r.status_code == 409
    assert fake.reviews == []


def test_a_rebind_between_propose_and_approve_is_refused(
        cfg, svc, monkeypatch):  # noqa: F811
    fake = _SmsFake(PHONE)
    c, _fake, agent = _app(cfg, svc, monkeypatch, fake)
    _bind(svc)
    svc.disconnect_imessage(_account_id(svc))
    _bind(svc, handle=OTHER)
    r = c.post(f"/review/{agent}/act-sms/submit", json={"ack": "true"})
    assert r.status_code == 409
    assert fake.reviews == [] and fake.confirms == []


def test_a_text_to_the_accounts_own_number_is_approved(
        cfg, svc, monkeypatch):  # noqa: F811
    fake = _SmsFake(PHONE)
    c, _fake, agent = _app(cfg, svc, monkeypatch, fake)
    _bind(svc)
    r = c.post(f"/review/{agent}/act-sms/submit", json={"ack": "true"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["confirmed"] is True
    assert fake.reviews == ["act-sms"] and fake.confirms == ["act-sms"]


def test_a_text_to_another_number_can_still_be_declined(
        cfg, svc, monkeypatch):  # noqa: F811
    fake = _SmsFake(OTHER)
    c, _fake, agent = _app(cfg, svc, monkeypatch, fake)
    _bind(svc)
    r = c.post(f"/review/{agent}/act-sms/decline", json={})
    assert r.status_code == 200
    assert r.get_json()["declined"] is True
    assert fake.declines == ["act-sms"]


def test_the_review_page_says_not_your_number_in_place_of_approve(
        cfg, svc, monkeypatch):  # noqa: F811
    fake = _SmsFake(OTHER)
    c, _fake, agent = _app(cfg, svc, monkeypatch, fake)
    _bind(svc)
    html = c.get(f"/review/{agent}/act-sms").get_data(as_text=True)
    assert "data-not-your-number" in html
    assert "not going to the phone number linked to your account" in html
    assert 'id="approve-btn" class="btn btn-primary btn-lg" disabled hidden>' \
        in html
    assert 'bg-body-secondary rounded" hidden>' in html
    # Decline is untouched and still posts to the relay.
    assert 'id="decline-btn" class="btn btn-outline-secondary btn-lg ms-2">' \
        in html
    assert f"/review/{agent}/act-sms/submit" in html


def test_the_review_page_for_the_accounts_own_number_is_unchanged(
        cfg, svc, monkeypatch):  # noqa: F811
    fake = _SmsFake(PHONE)
    c, _fake, agent = _app(cfg, svc, monkeypatch, fake)
    _bind(svc)
    html = c.get(f"/review/{agent}/act-sms").get_data(as_text=True)
    assert "data-not-your-number" not in html
    assert 'disabled hidden>' not in html


def test_the_page_markup_the_relay_rewords_is_still_the_engines():
    """Each string the relay replaces occurs once in the engine template; a
    reworded template would otherwise leave Approve on the page."""
    from careagents.app import _NOT_YOUR_NUMBER_PAGE
    src = open("templates/action_approve.html").read()
    for old, _new in _NOT_YOUR_NUMBER_PAGE:
        assert src.count(old) == 1, old


def test_other_kinds_are_not_held_to_the_number(cfg, svc, monkeypatch):  # noqa: F811
    """The intake form (FakeClient's act-1) reviews exactly as before."""
    c, fake, agent = _app(cfg, svc, monkeypatch)
    r = c.post(f"/review/{agent}/act-1/submit", json={"nka": "true"})
    assert r.status_code == 200


@pytest.mark.parametrize("purpose", text_self.PURPOSES)
@pytest.mark.parametrize("origin", ["https://care.example", ""])
def test_every_fixed_text_passes_the_engines_red_flag_screen(purpose, origin):
    """The engine screens payload.body at propose; a template it refused
    would make the tool fail for everyone."""
    from r6.actions.safety import screen_text
    body = text_self.body_for(purpose, origin)
    assert screen_text(body) is None
    assert "STOP" in body and len(body) <= 160
