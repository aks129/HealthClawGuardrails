"""#926: a completed run shown as "Something went wrong", and chat failures
counted by cause.

1. The browser's run-event replay ended the stream on the first failed read.
   Under concurrent turns a read sometimes never reaches the engine: the poll
   backs off to 2 seconds, gunicorn's gthread keep-alive is 2 seconds, and a
   request sent on a pooled connection as the server closes it fails with
   ConnectionError(RemoteDisconnected). Measured against a local gthread
   gunicorn: 9 of 120 reads at a 2.0s wait, 0 of 240 at 0.5s. The engine
   logs nothing, the run completes, and the patient read "Something went
   wrong on our side."
2. chat_error is also counted under one fixed cause code, decided by the
   exception class and HTTP status only, never by message text.
3. The worker logs the class and the cause, never the message.
4. A refused or out-of-credit model key tells the tester the assistant is
   unavailable and emails the owner, at most once an hour.

Synthetic data only.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest

from careagents import agent, beta, causes, funnel, llm, mail
from careagents.healthclaw import HealthClawError, HealthClawUnconfirmed
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _turn, cfg, svc)


def _frames(body: str) -> list[dict]:
    return [json.loads(line[len("data: "):])
            for line in body.splitlines() if line.startswith("data: ")]


def _today(svc):  # noqa: F811
    return funnel.daily(svc.session, days=1)["days"][0]


# --- 1. the replay ----------------------------------------------------------

def _scripted_run(cfg, svc, monkeypatch, script):  # noqa: F811
    """A run whose event reads follow `script`: "fail" raises the transport
    error a dropped keep-alive connection raises, "event" appends a text
    event, "done" completes the run. Returns the stream's frames and app."""
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    created, message_id = fake.claim_inbound_message(
        tenant, "q", agent_id, fake.conversation_id(agent_id), "web", "r926")
    assert created is True
    run_id = fake.create_agent_run(tenant, message_id)["id"]
    steps = iter(script)
    real = fake.agent_run_events

    def scripted(tenant_id, rid, after=0, limit=100):
        step = next(steps, "done")
        if step == "fail":
            raise HealthClawError("run event replay failed", 0)
        if step == "event":
            fake._append_run_event(rid, "agent.text", {"text": "the answer"})
        elif step == "done":
            fake.runs[rid]["status"] = "completed"
        return real(tenant_id, rid, after=after, limit=limit)
    monkeypatch.setattr(fake, "agent_run_events", scripted)
    monkeypatch.setattr("careagents.app.time.sleep", lambda _s: None)
    body = c.get(f"/api/chat/runs/{run_id}/events",
                 query_string={"agent_id": agent_id, "after": 0}
                 ).get_data(as_text=True)
    return _frames(body)


def test_one_failed_read_does_not_fail_a_run_that_completes(
        cfg, svc, monkeypatch):  # noqa: F811
    """The reported defect. MUTATION: _REPLAY_MAX_MISSES = 1 -> red."""
    frames = _scripted_run(cfg, svc, monkeypatch,
                           ["event", "fail", "event", "done"])
    types = [f["type"] for f in frames]
    assert "error" not in types, frames
    assert frames[-1]["type"] == "done"
    assert frames[-1]["status"] == "completed"
    assert types.count("text") == 2
    assert _today(svc)["run_replay_error"] == 0


def test_misses_reset_after_a_good_read(cfg, svc, monkeypatch):  # noqa: F811
    """Two misses, a good read, two misses: never three in a row.
    MUTATION: drop `misses = 0` after a page -> red."""
    frames = _scripted_run(cfg, svc, monkeypatch,
                           ["fail", "fail", "event", "fail", "fail", "done"])
    assert frames[-1]["type"] == "done", frames


def test_an_engine_that_stays_gone_still_ends_the_stream_once(
        cfg, svc, monkeypatch):  # noqa: F811
    """#219 still holds: three misses in a row end the stream with the
    generic sentence, counted once."""
    frames = _scripted_run(cfg, svc, monkeypatch, ["fail"] * 10)
    assert frames[-1] == {"type": "error", "text": beta.with_911(
        agent.GENERIC_FAILURE_TEXT)}
    assert [f["type"] for f in frames] == ["accepted", "error"]
    assert _today(svc)["run_replay_error"] == 1


def test_the_replay_warning_names_class_and_status_not_the_message(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    fake.run_events_error = "CANARY-REPLAY-5521 db-7 password=x"
    monkeypatch.setattr("careagents.app.time.sleep", lambda _s: None)
    with caplog.at_level(logging.WARNING, logger="careagents.app"):
        c.post("/api/chat", json={"agent_id": agent_id, "message": "q",
                                  "request_id": "r926-log"},
               buffered=False).get_data()
    logged = caplog.text
    assert "HealthClawError" in logged and "status=0" in logged
    assert "CANARY-REPLAY-5521" not in logged


# --- 2. the classifier ------------------------------------------------------

@pytest.mark.parametrize("exc,cause", [
    (llm.LLMError("model call failed (HTTP 401)", 401), "model_auth"),
    (llm.LLMError("model call failed (HTTP 403)", 403), "model_auth"),
    (llm.LLMOutOfCredit("model call refused on billing"), "model_quota"),
    (llm.LLMRateLimited("model call rate limited (HTTP 429)"), "model_quota"),
    (llm.LLMTimeout("model call timed out"), "model_timeout"),
    (llm.LLMTransient("model call failed (InternalServerError)", 503),
     "model_other"),
    (llm.LLMError("model call failed (HTTP 400)", 400), "model_other"),
    (llm.LLMError("model call failed (ConnectionError)"), "model_other"),
    (HealthClawError("x", 0), "engine_unavailable"),
    (HealthClawError("x", 503), "engine_unavailable"),
    (HealthClawError("x", 429), "engine_unavailable"),
    (HealthClawError("x", 408), "engine_unavailable"),
    (HealthClawUnconfirmed("x", 0), "engine_unavailable"),
    (HealthClawError("x", 400), "engine_refused"),
    (HealthClawError("x", 404), "engine_refused"),
    (HealthClawError("worker lease was lost", 409), "engine_refused"),
    (RuntimeError("x"), "internal"),
    (KeyError("x"), "internal"),
])
def test_each_failure_has_one_cause(exc, cause):
    """MUTATION: swap any branch in causes.cause_of -> red."""
    assert causes.cause_of(exc) == cause
    assert cause in causes.CAUSES


@pytest.mark.parametrize("exc,cause", [
    # A message that names a cause is still classified by class and status.
    (llm.LLMError("401 Unauthorized: invalid x-api-key"), "model_other"),
    (llm.LLMError("credit balance is too low", 400), "model_other"),
    (HealthClawError("engine unavailable (503)", 404), "engine_refused"),
    (RuntimeError("HTTP 401 timed out, quota exceeded"), "internal"),
])
def test_message_text_never_decides_the_cause(exc, cause):
    assert causes.cause_of(exc) == cause


def test_the_codes_are_the_fixed_set():
    assert causes.CAUSES == (
        "model_auth", "model_quota", "model_timeout", "model_other",
        "engine_unavailable", "engine_refused", "deadline", "internal")
    assert funnel.CHAT_ERROR_CAUSES == causes.CAUSES
    for c in causes.CAUSES:                 # fits ca_issue_days.kind
        assert len(c) <= 24


def test_the_openai_path_carries_the_status(monkeypatch):
    class _Cfg:
        provider = "openai"
        openai_base = "https://provider.invalid/v1"
        openai_api_key = "k"
        openai_model = "m"

    class _Resp:
        status_code = 401
        headers: dict = {}

        def json(self):
            return {"error": {"type": "invalid_api_key"}}
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(llm.LLMError) as info:
        llm.complete(_Cfg(), "s", [{"role": "user", "content": "hi"}], [])
    assert info.value.status == 401
    assert causes.cause_of(info.value) == "model_auth"


def test_the_anthropic_path_carries_the_status(monkeypatch):
    from tests.test_llm_retry import _anthropic_raising
    call = _anthropic_raising(monkeypatch, 401, {"type": "error", "error": {
        "type": "authentication_error", "message": "invalid x-api-key"}})
    with pytest.raises(llm.LLMError) as info:
        call()
    assert info.value.status == 401
    assert causes.cause_of(info.value) == "model_auth"


# --- counting, the digest and the owner page -------------------------------

def test_a_chat_error_is_counted_with_its_cause(svc):  # noqa: F811
    funnel.count_chat_error(svc.session, "model_quota")
    funnel.count_chat_error(svc.session, "engine_unavailable")
    funnel.count_chat_error(svc.session, "engine_unavailable")
    today = _today(svc)
    assert today["chat_error"] == 3
    assert today["model_quota"] == 1 and today["engine_unavailable"] == 2
    with pytest.raises(ValueError):
        funnel.count_chat_error(svc.session, "provider said no")


def test_the_digest_lists_chat_errors_by_cause(svc):  # noqa: F811
    day = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)
    funnel.count_issue(svc.session, "chat_error", day)
    funnel.count_issue(svc.session, "model_auth", day)
    report = funnel.daily(svc.session, days=1, moment=day)
    text = funnel.digest_text(report, "2026-10-01")
    assert "chat_error by cause" in text
    assert "  model_auth: 1  (7 days: 1)" in text
    assert "  engine_unavailable: 0" in text


def test_the_owner_page_shows_the_causes(svc, monkeypatch):  # noqa: F811
    from tests.test_careagents import _login
    from tests.test_careagents_funnel import _app
    app = _app(svc, CARE_OWNER_EMAILS="owner@example.com")
    funnel.count_chat_error(svc.session, "model_timeout")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="owner@example.com")
    html = c.get("/admin/funnel").get_data(as_text=True)
    assert 'id="funnel-chat-causes"' in html
    for c_ in causes.CAUSES:
        assert c_.replace("_", " ") in html


# --- 3 and 4. the worker ----------------------------------------------------

def _failed_turn(cfg, svc, monkeypatch, exc, request_id):  # noqa: F811
    """One chat turn whose model call raises `exc`. Returns the frames."""
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(agent.llm, "complete", boom)
    monkeypatch.setattr("careagents.app.time.sleep", lambda _s: None)
    r = _turn(c, agent_id, "what does CANARY-Q-8812 mean",
              request_id=request_id)
    return _frames(r.get_data(as_text=True)), fake


@pytest.fixture
def owner_mail(cfg, monkeypatch):  # noqa: F811
    sent = []

    def fake_notice(_cfg, to, subject, line):
        sent.append((to, subject, line))
        return mail.SENT
    monkeypatch.setattr(mail, "send_notice", fake_notice)
    monkeypatch.setattr(cfg, "owner_emails",
                        frozenset({"owner@example.com"}))
    return sent


@pytest.mark.parametrize("exc", [
    llm.LLMError("model call failed (AuthenticationError)", 401),
    llm.LLMOutOfCredit("model call refused on billing"),
])
def test_a_dead_model_key_says_unavailable_and_tells_the_owner(
        cfg, svc, monkeypatch, owner_mail, exc):  # noqa: F811
    """MUTATION: drop the needs_owner branch in failure_text -> red;
    drop the alert_owner call in the worker -> red."""
    frames, _fake = _failed_turn(cfg, svc, monkeypatch, exc, "dead-key")
    errors = [f for f in frames if f["type"] == "error"]
    assert errors and errors[-1]["text"] == beta.with_911(
        "The assistant is unavailable right now; we've been told.")
    assert len(owner_mail) == 1
    to, subject, line = owner_mail[0]
    assert to == "owner@example.com"
    assert causes.cause_of(exc) in line
    assert "CANARY-Q-8812" not in line
    today = _today(svc)
    assert today["chat_error"] == 1 and today[causes.cause_of(exc)] == 1


def test_with_no_owner_address_nobody_claims_to_have_been_told(
        cfg, svc, monkeypatch):  # noqa: F811
    """"We've been told" only where an owner address receives the email.
    MUTATION: always return UNAVAILABLE_TEXT -> red."""
    monkeypatch.setattr(cfg, "owner_emails", frozenset())
    monkeypatch.setattr(mail, "send_notice",
                        lambda *a: pytest.fail("nobody to tell"))
    frames, _ = _failed_turn(cfg, svc, monkeypatch,
                             llm.LLMOutOfCredit("billing"), "untold")
    errors = [f for f in frames if f["type"] == "error"]
    assert errors[-1]["text"] == beta.with_911(
        "The assistant is unavailable right now. Please try again later.")
    assert "told" not in errors[-1]["text"]


def test_the_owner_is_emailed_at_most_once_an_hour(svc, cfg, owner_mail):  # noqa: F811
    t = dt.datetime(2026, 10, 16, 14, 5, tzinfo=dt.timezone.utc)
    assert funnel.alert_owner(svc.session, cfg, "model_quota", t) == mail.SENT
    later = t + dt.timedelta(minutes=50)
    assert funnel.alert_owner(svc.session, cfg, "model_auth", later) is None
    assert len(owner_mail) == 1
    nxt = t + dt.timedelta(hours=1)
    assert funnel.alert_owner(svc.session, cfg, "model_auth", nxt) == mail.SENT
    assert len(owner_mail) == 2


def test_an_unsent_owner_alert_is_tried_again(svc, cfg, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cfg, "owner_emails", frozenset({"o@example.com"}))
    outcomes = iter([mail.NOT_SENT, mail.SENT])
    monkeypatch.setattr(mail, "send_notice", lambda *a: next(outcomes))
    t = dt.datetime(2026, 10, 16, 14, 5, tzinfo=dt.timezone.utc)
    assert funnel.alert_owner(svc.session, cfg, "model_auth", t) == \
        mail.NOT_SENT
    assert funnel.alert_owner(svc.session, cfg, "model_auth", t) == mail.SENT


def test_no_owner_address_means_no_alert(svc, cfg, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cfg, "owner_emails", frozenset())
    monkeypatch.setattr(mail, "send_notice",
                        lambda *a: pytest.fail("nobody to tell"))
    assert funnel.alert_owner(svc.session, cfg, "model_auth") is None


@pytest.mark.parametrize("exc", [
    llm.LLMRateLimited("model call rate limited (HTTP 429)"),
    llm.LLMTimeout("model call timed out"),
    RuntimeError("bug"),
])
def test_other_failures_do_not_email_the_owner(
        cfg, svc, monkeypatch, owner_mail, exc):  # noqa: F811
    frames, _ = _failed_turn(cfg, svc, monkeypatch, exc, "other")
    assert owner_mail == []
    assert _today(svc)[causes.cause_of(exc)] == 1
    errors = [f for f in frames if f["type"] == "error"]
    assert errors[-1]["text"] != agent.UNAVAILABLE_TEXT


def test_the_worker_logs_class_and_cause_never_the_message(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    """MUTATION: log the exception (logger.exception, or `exc` in the
    format) -> red: the canary is in the message."""
    exc = RuntimeError("CANARY-MSG-3307 the patient asked about CANARY-Q")
    with caplog.at_level(logging.INFO):
        _failed_turn(cfg, svc, monkeypatch, exc, "log-check")
    worker_lines = [r for r in caplog.records
                    if r.name == "careagents.worker"]
    text = "\n".join(r.getMessage() for r in worker_lines)
    assert "RuntimeError" in text and "cause=internal" in text
    assert "CANARY" not in caplog.text
    assert all(r.exc_info is None for r in worker_lines)
