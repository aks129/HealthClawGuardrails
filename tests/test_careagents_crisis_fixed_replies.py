"""A fixed sentence that answers instead of the model still leads with 911.

QA on #1017: "me duele el pecho" or "chest pain" sent at the day's limit got
only "You've used today's messages", with no emergency line, in English
too. Where a fixed sentence answers (the limit at admission, web and text;
the limit, pause or terms in the worker; a failed turn) and what the person
said reads as a crisis, beta.emergency_first puts the bilingual 911 line
first, and the 988 line for self-harm wording. The limit still holds and no
model is called. Synthetic text only.
"""

from __future__ import annotations

import pytest

from careagents import beta, errors
from careagents.agent import failure_text
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)
from tests.test_careagents_imessage import PHONE, _acct_id, _inbound, _pair

EMAIL = "gene@example.com"   # the address _login uses
URGENT = ("chest pain", "me duele el pecho", "no puedo respirar")
SELF_HARM = ("quiero morir", "I want to kill myself", "me quiero hacer daño")


# --- the helper ---------------------------------------------------------------

def test_the_lines_say_911_and_988_in_english_and_spanish():
    assert beta.EMERGENCY_LINE == ("If this is an emergency, call 911 now. "
                                   "Si es una emergencia, llame al 911 "
                                   "ahora.")
    assert "988" in beta.SELF_HARM_LINE and "llame" in beta.SELF_HARM_LINE


@pytest.mark.parametrize("said", URGENT)
def test_urgent_wording_leads_with_911_then_the_reply(said):
    out = beta.emergency_first("You've used today's messages.", said)
    assert out == (beta.EMERGENCY_LINE
                   + "\n\nYou've used today's messages.")
    assert beta.emergency_first(out, said) == out      # said once


@pytest.mark.parametrize("said", SELF_HARM)
def test_self_harm_wording_adds_988(said):
    out = beta.emergency_first("reply", said)
    assert out == (beta.EMERGENCY_LINE + "\n" + beta.SELF_HARM_LINE
                   + "\n\nreply")


@pytest.mark.parametrize("said", (
    "hi", "how are my labs?", "tengo una pregunta sobre mi presión", ""))
def test_anything_else_is_the_reply_exactly(said):
    assert beta.emergency_first("reply", said) == "reply"


def test_a_crisis_led_failure_stays_on_the_page():
    """collapse_failed_turns shows a wall of failures once; it must never
    do that by hiding the emergency line."""
    from careagents.agent import GENERIC_FAILURE_TEXT
    led = beta.emergency_first(GENERIC_FAILURE_TEXT, "chest pain")
    past = [{"role": "user", "content": "chest pain"},
            {"role": "assistant", "content": led},
            {"role": "user", "content": "hello?"},
            {"role": "assistant", "content": GENERIC_FAILURE_TEXT}]
    shown = [m["content"] for m in errors.collapse_failed_turns(past)]
    assert led in shown


# --- the day's limit at admission: web and text -------------------------------

def _spend_the_day(cfg, svc):  # noqa: F811
    acct = _acct_id(svc, EMAIL)
    for _ in range(cfg.chat_turns_per_day):
        assert svc.claim_daily_turn(acct, cfg.chat_turns_per_day)[0]


def _no_model(monkeypatch):
    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: pytest.fail("a model was called"))


@pytest.mark.parametrize("said", URGENT + SELF_HARM)
def test_web_at_the_limit_leads_with_911(cfg, svc, monkeypatch, said):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _spend_the_day(cfg, svc)
    _no_model(monkeypatch)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": said,
                                  "request_id": "lim-crisis"})
    assert r.status_code == 429
    message = r.get_json()["message"]
    assert message.startswith(beta.EMERGENCY_LINE)
    assert (beta.SELF_HARM_LINE in message) == (said in SELF_HARM)
    assert message.endswith("\n\n" + beta.daily_limit_text())
    assert not fake.runs


def test_web_at_the_limit_without_a_crisis_is_unchanged(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _spend_the_day(cfg, svc)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "lim-plain"})
    assert r.status_code == 429
    assert r.get_json()["message"] == beta.daily_limit_text()


@pytest.mark.parametrize("said", URGENT + SELF_HARM)
def test_text_at_the_limit_leads_with_911(cfg, svc, monkeypatch, said):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _spend_the_day(cfg, svc)
    runs_before = len(fake.runs)
    reply = _inbound(c, PHONE, said).get_json()["reply"]
    assert reply.startswith(beta.EMERGENCY_LINE)
    assert (beta.SELF_HARM_LINE in reply) == (said in SELF_HARM)
    assert reply.endswith("\n\n" + beta.daily_limit_text())
    assert len(fake.runs) == runs_before


def test_text_at_the_limit_without_a_crisis_is_unchanged(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _spend_the_day(cfg, svc)
    assert _inbound(c, PHONE, "one more").get_json()["reply"] == (
        beta.daily_limit_text())


# --- in the worker: the limit, a pause, a failed turn ------------------------

def _queue(c, agent_id, said, request_id):
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": said,
                                  "request_id": request_id}, buffered=False)
    assert r.status_code == 200
    next(iter(r.response))
    r.close()


def _answers(fake):
    return [row["content"] for rows in fake.logged.values()
            for row in rows if row["role"] == "assistant"]


def _work(cfg, svc, fake):  # noqa: F811
    from careagents.worker import RunWorker
    w = RunWorker(cfg, fake, svc, "crisis-worker")
    while w.run_once():
        pass


@pytest.mark.parametrize("surface", ("web", "imessage"))
def test_a_turn_charged_past_the_limit_leads_with_911(
        cfg, svc, monkeypatch, surface):  # noqa: F811
    """Two turns admitted before either ran; the second, a crisis, finds
    the day spent in the worker."""
    from careagents import agent as agent_mod
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    calls = []

    class _Turn:
        text, tool_calls, raw_tool_calls = "model answer", [], []
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: calls.append(1) or _Turn())
    monkeypatch.setattr(cfg, "chat_turns_per_day", 1)
    if surface == "web":
        _queue(c, agent_id, "hi", "w-1")
        _queue(c, agent_id, "me duele el pecho", "w-2")
    else:
        _pair(c, agent_id)
        assert "run_id" in _inbound(c, PHONE, "hi").get_json()
        assert "run_id" in _inbound(
            c, PHONE, "me duele el pecho").get_json()
    _work(cfg, svc, fake)
    assert calls == [1]
    last = _answers(fake)[-1]
    assert last.startswith(beta.EMERGENCY_LINE + "\n\n")
    assert beta.DAILY_LIMIT_TEXT in last


def test_a_paused_crisis_turn_leads_with_911(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _queue(c, agent_id, "quiero morir", "p-1")
    assert svc.set_paused(EMAIL, True) is True
    _no_model(monkeypatch)
    _work(cfg, svc, fake)
    assert _answers(fake) == [beta.EMERGENCY_LINE + "\n"
                              + beta.SELF_HARM_LINE + "\n\n"
                              + beta.PAUSED_TEXT]


def test_a_paused_ordinary_turn_is_unchanged(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _queue(c, agent_id, "how are my labs?", "p-2")
    svc.set_paused(EMAIL, True)
    _work(cfg, svc, fake)
    assert _answers(fake) == [beta.PAUSED_TEXT]


@pytest.mark.parametrize("said,lead", (
    ("chest pain", beta.EMERGENCY_LINE),
    ("how are my labs?", "")))
def test_a_failed_turn_leads_with_911_for_a_crisis(
        cfg, svc, monkeypatch, said, lead):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("synthetic provider outage")
    monkeypatch.setattr("careagents.worker.llm.complete", _boom)
    _queue(c, agent_id, said, "f-1")
    _work(cfg, svc, fake)
    expected = failure_text(RuntimeError(),
                            owner_told=bool(cfg.owner_emails))
    if lead:
        expected = f"{lead}\n\n{expected}"
    run_id = next(iter(fake.runs))
    errors_said = [e["payload"].get("text") for e in fake.events[run_id]
                   if e["type"] == "agent.error"]
    assert errors_said == [expected]
    assert _answers(fake) == [expected]


# --- every other fixed reply: busy, unavailable, too long, workers down ------
#
# Round two on #1017: no fixed reply to a crisis message may lack the 911
# line. A text surface's replies all pass through imessage.handle_inbound,
# which leads any reply with it; the web's burst 429 and workers-down 503
# carry it in `message`, and chat.js shows a flagged 503 instead of
# retrying into "Connection hiccup".

CRISIS = ("me duele el pecho", "quiero morir")


def _lead(said):
    return beta.emergency_first("", said)[:-2]


@pytest.mark.parametrize("said", CRISIS)
def test_text_busy_leads_with_911(cfg, svc, monkeypatch, said):  # noqa: F811
    from careagents import imessage
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    monkeypatch.setattr(cfg, "chat_turns_per_window", 1)
    assert _inbound(c, PHONE, "one").status_code == 202
    r = _inbound(c, PHONE, said)
    assert r.status_code == 200
    assert r.get_json() == {"reply": _lead(said) + "\n\n"
                            + imessage.busy_text(cfg.chat_window_seconds)}


@pytest.mark.parametrize("said", CRISIS)
def test_text_without_workers_leads_with_911(cfg, svc, monkeypatch, said):  # noqa: F811
    from careagents import imessage
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    deps = app.extensions["careagents_imessage"]
    monkeypatch.setattr(deps, "workers_ready", lambda: False)
    r = _inbound(c, PHONE, said)
    assert r.status_code == 503
    assert r.get_json()["reply"] == (_lead(said) + "\n\n"
                                     + imessage.UNAVAILABLE_TEXT)


@pytest.mark.parametrize("said", CRISIS)
def test_text_queue_down_leads_with_911(cfg, svc, monkeypatch, said):  # noqa: F811
    from careagents import imessage
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    deps = app.extensions["careagents_imessage"]

    def _down(*_a, **_k):
        raise deps.queue_error("synthetic queue outage")
    monkeypatch.setattr(deps, "queue_turn", _down)
    r = _inbound(c, PHONE, said)
    assert r.status_code == 503
    assert r.get_json()["reply"] == (_lead(said) + "\n\n"
                                     + imessage.UNAVAILABLE_TEXT)


@pytest.mark.parametrize("said", CRISIS)
def test_text_too_long_leads_with_911(cfg, svc, monkeypatch, said):  # noqa: F811
    from careagents import imessage
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    r = _inbound(c, PHONE, said + " " + "x" * 2100)
    assert r.status_code == 400
    assert r.get_json()["reply"] == (_lead(said) + "\n\n"
                                     + imessage.TOO_LONG_TEXT)


def test_text_ordinary_replies_are_unchanged(cfg, svc, monkeypatch):  # noqa: F811
    from careagents import imessage
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    assert _inbound(c, PHONE, "x" * 2100).get_json() == {
        "error": "message must be 1-2000 characters",
        "reply": imessage.TOO_LONG_TEXT}
    deps = app.extensions["careagents_imessage"]
    monkeypatch.setattr(deps, "workers_ready", lambda: False)
    assert _inbound(c, PHONE, "hi").get_json() == {
        "error": "run_workers_unavailable",
        "reply": imessage.UNAVAILABLE_TEXT}


def test_a_stranger_in_crisis_gets_911_with_the_sign_in_link(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    reply = _inbound(c, "+15550100777", "no puedo respirar").get_json()[
        "reply"]
    assert reply.startswith(beta.EMERGENCY_LINE + "\n\n")
    assert "/link?t=" in reply


def test_an_opted_out_number_stays_silent_even_in_crisis(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    stranger = "+15550100778"
    _inbound(c, stranger, "STOP")
    r = _inbound(c, stranger, "chest pain")
    assert r.status_code == 200 and r.get_json() == {}


@pytest.mark.parametrize("said", CRISIS)
def test_web_burst_limit_leads_with_911(cfg, svc, monkeypatch, said):  # noqa: F811
    from careagents.app import PACE_TEXT
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(cfg, "chat_turns_per_window", 1)
    _queue(c, agent_id, "hi", "b-1")
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": said,
                                  "request_id": "b-2"})
    assert r.status_code == 429
    assert r.get_json() == {"error": "rate_limited", "emergency": True,
                            "message": _lead(said) + "\n\n" + PACE_TEXT}


def test_web_burst_limit_without_a_crisis_is_unchanged(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(cfg, "chat_turns_per_window", 1)
    _queue(c, agent_id, "hi", "b-3")
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "b-4"})
    assert r.status_code == 429 and r.get_json() == {"error": "rate_limited"}


@pytest.mark.parametrize("said,lead", (
    ("me duele el pecho", True), ("quiero morir", True), ("hi", False)))
def test_web_without_workers(cfg, svc, monkeypatch, said, lead):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    fake.worker_available = False
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": said,
                                  "request_id": "w-down"})
    assert r.status_code == 503
    plain = {"error": "run_workers_unavailable",
             "message": "Chat is temporarily unavailable. Try again soon."}
    if lead:
        assert r.get_json() == {**plain, "emergency": True, "message": (
            _lead(said) + "\n\nChat is temporarily unavailable. Try again "
            "soon.")}
    else:
        assert r.get_json() == plain
    assert fake.runs == {}


def _chat_js():
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[1] / "careagents"
            / "static" / "chat.js").read_text(encoding="utf-8")


def test_the_pace_sentence_matches_the_page():
    import re
    from careagents.app import PACE_TEXT
    m = re.search(r'const PACE_TEXT = "([^"]+)";', _chat_js())
    assert m and m.group(1) == PACE_TEXT


def test_the_page_shows_a_flagged_503_instead_of_retrying():
    """The 503 branch sits inside the streaming loop, past the reach of a
    node harness, so its source is pinned, as the 429 branch's is."""
    src = _chat_js()
    branch = src.split("if (resp.status === 503) {", 1)[1].split(
        "if (!resp.ok", 1)[0]
    assert "refusal.emergency === true" in branch
    assert "addAgentText(refusal.message);" in branch
    assert "return;" in branch
    assert src.index("if (resp.status === 503) {") < src.index(
        'throw new Error("event stream unavailable")')
