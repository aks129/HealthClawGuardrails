"""Sendblue: a hosted iMessage line in front of careagents.imessage.

The webhook is a thin adapter over handle_inbound; the run worker owns
delivery of the agent's answer. No real network: the Sendblue client is
replaced by a recorder, or its HTTP session by a scripted one.

Synthetic handles only (555 numbers).
"""

from __future__ import annotations

import logging

import pytest

from careagents import imessage, sendblue, sendblue_surface
from careagents.config import Config
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)

SECRET = "sb-test-signing-secret"
LINE = "+15550100000"           # our Sendblue number
PHONE = "+15550100123"          # the texter
HDRS = {"sb-signing-secret": SECRET}


def _env(base: Config, **extra) -> dict:
    env = {"CARE_DATABASE_URL": base.database_url,
           "CARE_RP_ID": "localhost", "CARE_ORIGIN": "http://localhost",
           "OPENAI_API_KEY": "k", "HEALTHCLAW_MINT_SECRET": "mint-secret",
           "CARE_REAL_RECORDS": "on",
           "SENDBLUE_API_KEY_ID": "key-id", "SENDBLUE_API_SECRET": "key-secret",
           "SENDBLUE_WEBHOOK_SECRET": SECRET,
           "SENDBLUE_FROM_NUMBER": LINE}
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


@pytest.fixture
def sb_cfg(cfg):  # noqa: F811
    return Config(env=_env(cfg))


@pytest.fixture
def sb_svc(sb_cfg):
    from careagents.accounts import AccountService
    service = AccountService(sb_cfg)
    yield service
    service.engine.dispose()


class FakeSendblue:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.typing: list[str] = []

    def send_message(self, number, content):
        self.sent.append((number, content))
        return sendblue.SendResult(ok=True, status=202)

    def send_typing(self, number):
        self.typing.append(number)
        return True


def _app(sb_cfg, sb_svc, monkeypatch, reply="here you go"):
    app, c, fake_hc, agent_id, *_ = _chat_app(sb_cfg, sb_svc, monkeypatch,
                                              reply=reply)
    ext = app.extensions["careagents_sendblue"]
    fake = FakeSendblue()
    ext.client = fake
    ext.spawn = lambda fn: fn()        # background work, run inline
    return app, c, fake, fake_hc, agent_id


def _hook(c, content="hi", handle="msg-1", headers=HDRS, **extra):
    body = {"content": content, "is_outbound": False, "status": "RECEIVED",
            "message_handle": handle, "from_number": PHONE,
            "to_number": LINE, "sendblue_number": LINE,
            "service": "iMessage", "media_url": "", "group_id": "",
            "date_sent": "2026-10-05T12:00:00Z"}
    body.update(extra)
    return c.post("/api/surfaces/sendblue/webhook", json=body,
                  headers=headers)


def _deliverer(app, fake, clock=None):
    rt = app.extensions["careagents_runtime"]
    return sendblue_surface.Deliverer(
        rt["config"], rt["client"], rt["accounts"], fake,
        **({"clock": clock} if clock else {}))


def _run(app):
    from careagents.worker import RunWorker
    rt = app.extensions["careagents_runtime"]
    RunWorker(rt["config"], rt["client"], rt["accounts"], "sb-worker"
              ).run_once()


def _pair(c, agent_id, handle="pair-1"):
    code = c.post("/api/surfaces/imessage",
                  json={"agent_id": agent_id}).get_json()["code"]
    return _hook(c, f"care {code}", handle=handle)


# --- config -----------------------------------------------------------------

def test_enabled_only_when_every_required_setting_is_present(cfg):  # noqa: F811
    assert Config(env=_env(cfg)).sendblue_enabled is True
    for name in ("SENDBLUE_API_KEY_ID", "SENDBLUE_API_SECRET",
                 "SENDBLUE_WEBHOOK_SECRET", "SENDBLUE_FROM_NUMBER"):
        assert Config(env=_env(cfg, **{name: ""})).sendblue_enabled is False
    assert Config(env=_env(cfg)).sendblue_api_base == "https://api.sendblue.co"
    assert Config(env=_env(
        cfg, SENDBLUE_API_BASE="https://api.sendblue.com/")
    ).sendblue_api_base == "https://api.sendblue.com"


@pytest.mark.parametrize("raw", [
    "+1 (555) 010-0100", "555-010-0100", "(555) 010 0100", "+15550100100"])
def test_the_from_number_is_normalized_to_e164(cfg, raw):  # noqa: F811
    c = Config(env=_env(cfg, SENDBLUE_FROM_NUMBER=raw))
    assert c.sendblue_enabled is True
    assert c.sendblue_from_number == "+15550100100"
    assert c.imessage_handle == "+15550100100"


@pytest.mark.parametrize("raw", [
    "12345", "not a number", "line@example.com", "+1000"])
def test_an_unusable_from_number_switches_sendblue_off(
        cfg, raw, caplog):  # noqa: F811
    caplog.set_level(logging.WARNING, logger="careagents.config")
    c = Config(env=_env(cfg, SENDBLUE_FROM_NUMBER=raw))
    assert c.sendblue_enabled is False
    assert c.imessage_handle == ""
    warnings = [r for r in caplog.records if "SENDBLUE_FROM_NUMBER" in
                r.getMessage()]
    assert len(warnings) == 1
    assert raw not in caplog.text


def test_the_text_us_handle_is_the_sendblue_line_unless_overridden(cfg):  # noqa: F811
    assert Config(env=_env(cfg)).imessage_handle == LINE
    assert Config(env=_env(cfg, CARE_IMESSAGE_HANDLE="+15550100999")
                  ).imessage_handle == "+15550100999"
    assert Config(env=_env(cfg, SENDBLUE_API_SECRET="")
                  ).imessage_handle == ""


def test_settings_shows_the_sendblue_number(sb_cfg, sb_svc, monkeypatch):
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    r = c.post("/api/surfaces/imessage", json={"agent_id": agent_id})
    assert r.get_json()["handle"] == LINE
    assert LINE in r.get_json()["instructions"]


# --- the webhook's front door -------------------------------------------------

def test_the_webhook_is_absent_when_sendblue_is_off(cfg, svc, monkeypatch):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    assert _hook(c).status_code == 404


@pytest.mark.parametrize("headers", [
    {}, {"sb-signing-secret": ""}, {"sb-signing-secret": "wrong"},
    {"sb-signing-secret": SECRET + "x"}, {"sb-signing-secret": "sécret"}])
def test_a_missing_or_wrong_secret_is_refused(
        sb_cfg, sb_svc, monkeypatch, headers):
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    r = _hook(c, headers=headers)
    assert r.status_code == 401
    assert fake.sent == []


def test_the_right_secret_is_accepted(sb_cfg, sb_svc, monkeypatch):
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    assert _hook(c).status_code == 200


@pytest.mark.parametrize("extra", [
    {"is_outbound": True},
    {"group_id": "group-1"},
    {"status": "SENT"},
    {"status": "DELIVERED"},
    {"type": "message_status"},
])
def test_what_is_not_an_inbound_one_to_one_text_is_ignored(
        sb_cfg, sb_svc, monkeypatch, extra):
    from careagents.models import ImessageLink
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    r = _hook(c, **extra)
    assert r.status_code == 200
    assert fake.sent == []
    with sb_svc.session() as s:
        assert s.query(ImessageLink).count() == 0


# --- replies ------------------------------------------------------------------

def test_an_unbound_sender_is_texted_a_sign_in_link(sb_cfg, sb_svc, monkeypatch):
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    r = _hook(c, "hi")
    assert r.status_code == 200
    assert len(fake.sent) == 1
    number, text = fake.sent[0]
    assert number == PHONE
    assert text.startswith("Hi, this is CareAgents.")
    assert f"{sb_cfg.origin}/link/" in text


def test_a_sendblue_retry_is_answered_once(sb_cfg, sb_svc, monkeypatch):
    from careagents.models import ImessageLink
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    assert _hook(c, "hi", handle="same").status_code == 200
    assert _hook(c, "hi", handle="same").status_code == 200
    assert len(fake.sent) == 1
    with sb_svc.session() as s:
        assert s.query(ImessageLink).count() == 1
    assert _hook(c, "hi", handle="other").status_code == 200
    assert len(fake.sent) == 2


def test_the_message_handle_is_not_stored_as_sent(sb_cfg, sb_svc, monkeypatch):
    from careagents.models import SendblueMessage
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    _hook(c, "hi", handle="handle-abc")
    with sb_svc.session() as s:
        row = s.query(SendblueMessage).one()
        assert "handle-abc" not in (row.key_hash, row.id)


def test_a_media_only_message_is_told_text_only(sb_cfg, sb_svc, monkeypatch):
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    r = _hook(c, "", media_url="https://example.com/x.jpg")
    assert r.status_code == 200
    assert fake.sent == [(PHONE, sendblue_surface.MEDIA_ONLY_TEXT)]
    assert sendblue_surface.MEDIA_ONLY_TEXT == "I can only read text for now."


def test_a_failure_inside_the_core_lets_sendblue_retry(
        sb_cfg, sb_svc, monkeypatch):
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(sendblue_surface.imessage, "handle_inbound", _boom)
    assert _hook(c, "hi", handle="m-9").status_code == 500
    monkeypatch.undo()
    app.extensions["careagents_sendblue"].client = fake
    assert _hook(c, "hi", handle="m-9").status_code == 200
    assert len(fake.sent) == 1


# --- the agent's answer, delivered by the worker ------------------------------

def test_a_run_is_delivered_once_when_it_finishes(sb_cfg, sb_svc, monkeypatch):
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch,
                                      reply="Your A1c is in range.")
    assert _pair(c, agent_id).status_code == 200
    assert fake.sent == [(PHONE, imessage.WELCOME_TEXT)]
    fake.sent.clear()

    assert _hook(c, "how is my a1c?", handle="q-1").status_code == 200
    assert fake.typing == [PHONE]           # the run started
    deliver = _deliverer(app, fake)
    deliver.once()
    assert fake.sent == []                  # not finished yet

    _run(app)
    deliver.once()
    assert fake.sent == [(PHONE, "Your A1c is in range.")]
    deliver.once()
    _deliverer(app, fake).once()            # a second worker process
    assert len(fake.sent) == 1


def test_two_workers_that_both_saw_a_row_send_it_once(
        sb_cfg, sb_svc, monkeypatch):
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    _hook(c, "hello", handle="q-race")
    _run(app)
    fake.sent.clear()
    [row] = sb_svc.sendblue_pending()       # both read it before either sent
    _deliverer(app, fake)._deliver(dict(row))
    _deliverer(app, fake)._deliver(dict(row))
    assert len(fake.sent) == 1


def test_a_review_card_is_delivered_as_a_link(sb_cfg, sb_svc, monkeypatch):
    # The same projection the relay's runs endpoint uses.
    events = [
        {"type": "agent.text", "payload": {"text": "Here it is."}},
        {"type": "agent.card", "payload": {
            "type": "card", "kind": "review", "action_id": "act-1",
            "provider_call_id": "p", "event_key": "p:0"}},
        {"type": "agent.card", "payload": {
            "type": "card", "kind": "pdf", "url": "https://example.com/f.pdf"}},
    ]
    reply = imessage.run_reply(events, "http://localhost", "ag_1")
    assert reply.startswith("Here it is.")
    assert "http://localhost/review/ag_1/act-1" in reply
    assert "https://example.com/f.pdf" in reply
    assert imessage.run_reply([], "http://x", "a").endswith(
        "Please try again.")


def test_a_run_that_never_finishes_gets_one_timeout_text(
        sb_cfg, sb_svc, monkeypatch):
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    fake.sent.clear()
    _hook(c, "hello", handle="q-2")
    now = [0.0]
    import time
    now[0] = time.time() + 3600
    deliver = _deliverer(app, fake, clock=lambda: now[0])
    deliver.once()
    deliver.once()
    assert len(fake.sent) == 1
    assert fake.sent[0][1].startswith("That took too long.")


def test_a_handle_that_stopped_is_not_sent_the_answer(
        sb_cfg, sb_svc, monkeypatch):
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    _hook(c, "hello", handle="q-3")
    _hook(c, "STOP", handle="q-4")
    fake.sent.clear()
    _run(app)
    _deliverer(app, fake).once()
    assert fake.sent == []


def test_the_worker_pool_starts_the_deliverer_when_enabled(
        sb_cfg, monkeypatch):
    import threading
    from careagents import worker
    started = []
    monkeypatch.setattr(sendblue_surface.Deliverer, "run",
                        lambda self, stop: started.append(stop))
    stop = threading.Event()
    stop.set()
    worker.run_worker_pool(sb_cfg, stop)
    assert started == [stop]


# --- the client: retries, limits, masking ------------------------------------

class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


class _HTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers,
                           "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _client(sb_cfg, *responses):
    http = _HTTP(*responses)
    sleeps: list[float] = []
    return sendblue.Client(sb_cfg, http=http, sleep=sleeps.append), http, sleeps


def test_send_posts_the_documented_shape(sb_cfg):
    client, http, sleeps = _client(sb_cfg, _Resp(200, {"status": "QUEUED"}))
    res = client.send_message(PHONE, "hello")
    assert res.ok and not res.retried and sleeps == []
    call = http.calls[0]
    assert call["url"] == "https://api.sendblue.co/api/send-message"
    assert call["json"] == {"number": PHONE, "from_number": LINE,
                            "content": "hello"}
    assert call["headers"]["sb-api-key-id"] == "key-id"
    assert call["headers"]["sb-api-secret-key"] == "key-secret"
    assert call["timeout"]


def test_long_content_is_cut_to_the_limit(sb_cfg):
    client, http, _ = _client(sb_cfg, _Resp(200))
    client.send_message(PHONE, "x" * 30000)
    assert len(http.calls[0]["json"]["content"]) == sendblue.MAX_CONTENT


def test_a_429_is_retried_once_after_a_pause(sb_cfg):
    client, http, sleeps = _client(sb_cfg, _Resp(429), _Resp(429))
    res = client.send_message(PHONE, "hello")
    assert not res.ok and res.status == 429 and res.retried
    assert len(http.calls) == 2 and len(sleeps) == 1 and sleeps[0] > 0


def test_a_429_then_success_is_sent(sb_cfg):
    client, http, sleeps = _client(sb_cfg, _Resp(429), _Resp(200))
    assert client.send_message(PHONE, "hello").ok
    assert len(http.calls) == 2


def test_a_5xx_or_network_error_is_retried_once(sb_cfg):
    import requests
    client, http, sleeps = _client(
        sb_cfg, _Resp(502), requests.ConnectionError("down"))
    res = client.send_message(PHONE, "hello")
    assert not res.ok and len(http.calls) == 2 and len(sleeps) == 1


@pytest.mark.parametrize("code", [4007, 4008, 4009, 4010])
def test_pre_reply_limits_are_not_retried(sb_cfg, code):
    client, http, sleeps = _client(sb_cfg, _Resp(400, {
        "status": "ERROR", "error_code": code,
        "error_message": f"limit for {PHONE}"}))
    res = client.send_message(PHONE, "hello")
    assert not res.ok and res.error_code == code
    assert len(http.calls) == 1 and sleeps == []


def test_an_error_status_in_a_200_is_not_a_send(sb_cfg):
    client, http, _ = _client(sb_cfg, _Resp(200, {"status": "ERROR",
                                                  "error_code": 5000}))
    assert not client.send_message(PHONE, "hello").ok


def test_typing_indicator_is_best_effort(sb_cfg):
    import requests
    client, http, _ = _client(sb_cfg, requests.Timeout("slow"))
    assert client.send_typing(PHONE) is False
    client, http, _ = _client(sb_cfg, _Resp(200))
    assert client.send_typing(PHONE) is True
    assert http.calls[0]["url"].endswith("/api/send-typing-indicator")
    assert http.calls[0]["json"] == {"number": PHONE, "from_number": LINE,
                                     "state": "start"}


def test_numbers_and_words_never_reach_the_log(
        sb_cfg, sb_svc, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    client, http, _ = _client(sb_cfg, _Resp(400, {
        "error_code": 4008, "error_message": f"limit for {PHONE}"}))
    client.send_message(PHONE, "my private words")
    client, http, _ = _client(sb_cfg, _Resp(429), _Resp(503))
    client.send_message(PHONE, "my private words")

    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _hook(c, "my private words", handle="log-1")
    _hook(c, "", handle="log-2", media_url="https://example.com/a.jpg")
    _hook(c, "x", handle="log-3", headers={"sb-signing-secret": "no"})
    logged = caplog.text
    assert caplog.records, "expected the failures to be logged"
    assert PHONE not in logged and "0100123" not in logged
    assert "my private words" not in logged
    assert SECRET not in logged and "key-secret" not in logged
