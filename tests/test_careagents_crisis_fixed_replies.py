"""A fixed sentence that answers instead of the model still leads with 911.

QA on #1017: "me duele el pecho" or "chest pain" sent at the day's limit got
only "You've used today's messages", with no emergency line, in English
too. Where a fixed sentence answers (the limit at admission, web and text;
the limit, pause or terms in the worker; a failed turn) and what the person
said reads as a crisis, beta.emergency_first puts the bilingual 911 line
first, and the 988 line for self-harm wording. The limit still holds and no
model is called.

Security re-review (round 4): every refusal or failure now carries a 911
line whatever was said (beta.with_911). Crisis wording in the current
message puts the emergency line first; anything else gets
EMERGENCY_TRAILER after the sentence. The word list decides the order,
never whether there is a line. Synthetic text only.
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
    led = beta.with_911(GENERIC_FAILURE_TEXT, "chest pain")
    past = [{"role": "user", "content": "chest pain"},
            {"role": "assistant", "content": led},
            {"role": "user", "content": "hello?"},
            {"role": "assistant", "content": beta.with_911(
                GENERIC_FAILURE_TEXT)}]
    shown = [m["content"] for m in errors.collapse_failed_turns(past)]
    assert led in shown


def test_failures_ending_with_the_911_line_still_collapse():
    """The trailer must not stop a wall of ordinary failures collapsing,
    nor take "Report a problem" (`failed`) off the one kept."""
    from careagents.agent import GENERIC_FAILURE_TEXT
    fail = beta.with_911(GENERIC_FAILURE_TEXT)
    assert fail.endswith("\n\n" + beta.EMERGENCY_TRAILER)
    past = [{"role": "user", "content": "how are my labs?"},
            {"role": "assistant", "content": fail},
            {"role": "user", "content": "how are my labs?"},
            {"role": "assistant", "content": fail}]
    shown = errors.collapse_failed_turns(past)
    assert [m["role"] for m in shown] == ["user", "assistant"]
    assert shown[-1]["failed"] is True


@pytest.mark.parametrize("reply", (
    "You've used today's messages.", "Something went wrong."))
def test_with_911_always_carries_the_line(reply):
    plain = beta.with_911(reply, "how are my labs?")
    assert plain == reply + "\n\n" + beta.EMERGENCY_TRAILER
    assert beta.with_911(plain, "how are my labs?") == plain    # once
    assert beta.with_911(reply) == plain                        # no message
    led = beta.with_911(reply, "chest pain")
    assert led == beta.EMERGENCY_LINE + "\n\n" + reply
    assert beta.with_911(led, "chest pain") == led
    assert beta.EMERGENCY_TRAILER not in led
    assert "911" in beta.EMERGENCY_TRAILER and "llame" in (
        beta.EMERGENCY_TRAILER)


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


def test_web_at_the_limit_without_a_crisis_ends_with_911(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _spend_the_day(cfg, svc)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "lim-plain"})
    assert r.status_code == 429
    assert r.get_json()["message"] == (beta.daily_limit_text() + "\n\n"
                                       + beta.EMERGENCY_TRAILER)


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


def test_text_at_the_limit_without_a_crisis_ends_with_911(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _spend_the_day(cfg, svc)
    assert _inbound(c, PHONE, "one more").get_json()["reply"] == (
        beta.daily_limit_text() + "\n\n" + beta.EMERGENCY_TRAILER)


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


def test_a_paused_ordinary_turn_ends_with_911(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _queue(c, agent_id, "how are my labs?", "p-2")
    svc.set_paused(EMAIL, True)
    _work(cfg, svc, fake)
    assert _answers(fake) == [beta.PAUSED_TEXT + "\n\n"
                              + beta.EMERGENCY_TRAILER]


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
    expected = (f"{lead}\n\n{expected}" if lead else
                f"{expected}\n\n{beta.EMERGENCY_TRAILER}")
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


def test_text_ordinary_refusals_end_with_911(cfg, svc, monkeypatch):  # noqa: F811
    from careagents import imessage
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    tail = "\n\n" + beta.EMERGENCY_TRAILER
    assert _inbound(c, PHONE, "x" * 2100).get_json() == {
        "error": "message must be 1-2000 characters",
        "reply": imessage.TOO_LONG_TEXT + tail}
    deps = app.extensions["careagents_imessage"]
    monkeypatch.setattr(deps, "workers_ready", lambda: False)
    assert _inbound(c, PHONE, "hi").get_json() == {
        "error": "run_workers_unavailable",
        "reply": imessage.UNAVAILABLE_TEXT + tail}


def test_text_answers_that_are_not_refusals_get_no_trailer(
        cfg, svc, monkeypatch):  # noqa: F811
    """HELP and STOP confirmations, and a stranger's sign-in link, are not
    refusals: carriers want STOP's confirmation to be one plain text."""
    from careagents import imessage
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    assert _inbound(c, PHONE, "HELP").get_json() == {
        "reply": imessage.HELP_TEXT}
    assert beta.EMERGENCY_TRAILER not in _inbound(
        c, "+15550100779", "hello").get_json()["reply"]
    assert _inbound(c, PHONE, "STOP").get_json() == {
        "reply": imessage.STOP_TEXT}


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


def test_web_burst_limit_without_a_crisis_ends_with_911(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.app import PACE_TEXT
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(cfg, "chat_turns_per_window", 1)
    _queue(c, agent_id, "hi", "b-3")
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "b-4"})
    assert r.status_code == 429 and r.get_json() == {
        "error": "rate_limited",
        "message": PACE_TEXT + "\n\n" + beta.EMERGENCY_TRAILER}


UNAVAILABLE = "Chat is temporarily unavailable. Try again soon."


def _unavailable(code, said, lead):
    if lead:
        return {"error": code, "emergency": True,
                "message": _lead(said) + "\n\n" + UNAVAILABLE}
    return {"error": code,
            "message": UNAVAILABLE + "\n\n" + beta.EMERGENCY_TRAILER}


CASES = (("me duele el pecho", True), ("quiero morir", True), ("hi", False))


@pytest.mark.parametrize("said,lead", CASES)
def test_web_without_workers(cfg, svc, monkeypatch, said, lead):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    fake.worker_available = False
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": said,
                                  "request_id": "w-down"})
    assert r.status_code == 503
    assert r.get_json() == _unavailable("run_workers_unavailable", said, lead)
    assert fake.runs == {}


@pytest.mark.parametrize("said,lead", CASES)
def test_web_message_store_down(cfg, svc, monkeypatch, said, lead):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(fake, "claim_inbound_message",
                        lambda *a, **k: (None, None))
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": said,
                                  "request_id": "s-down"})
    assert r.status_code == 503
    assert r.get_json() == _unavailable("message store unavailable", said,
                                        lead)


@pytest.mark.parametrize("said,lead", CASES)
def test_web_run_queue_down(cfg, svc, monkeypatch, said, lead):  # noqa: F811
    from careagents.healthclaw import HealthClawError
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)

    def _down(*_a, **_k):
        raise HealthClawError("synthetic queue outage", 503)
    monkeypatch.setattr(fake, "create_agent_run", _down)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": said,
                                  "request_id": "q-down"})
    assert r.status_code == 503
    assert r.get_json() == _unavailable("run queue unavailable", said, lead)


def test_a_stream_error_ends_with_911():
    """The error frame the page shows for a failed run: the worker's text
    already carries the line; the engine's own, or none, gets it here."""
    from careagents.agent import GENERIC_FAILURE_TEXT
    from careagents.imessage import run_reply
    tail = "\n\n" + beta.EMERGENCY_TRAILER
    assert run_reply([{"type": "agent.error", "payload": {}}], "http://x",
                     "a") == GENERIC_FAILURE_TEXT + tail
    led = beta.with_911(GENERIC_FAILURE_TEXT, "chest pain")
    assert run_reply([{"type": "agent.error", "payload": {"text": led}}],
                     "http://x", "a") == led


# --- the sample frame never comes before the 911 guidance --------------------

def test_a_sample_crisis_answer_puts_the_frame_at_its_end(
        cfg, svc, monkeypatch):  # noqa: F811
    """#1015 live run: "Nothing here is about you." read before "call
    911". QA on #1017: after the first paragraph is not enough when the
    answer opens with sympathy. For a crisis message it goes at the end."""
    answer = ("I'm so sorry.\n\nCall 911 now. Llame al 911 ahora.\n\n"
              "In these made-up records, the sample person has asthma.")
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply=answer)
    _queue(c, agent_id, "me duele el pecho", "fr-1")
    _work(cfg, svc, fake)
    [said] = _answers(fake)
    assert said == answer + "\n\n" + beta.SAMPLE_FRAME


def test_a_sample_ordinary_answer_ends_with_the_frame_too(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply="The A1c was 7.2.")
    _queue(c, agent_id, "how is my a1c?", "fr-2")
    _work(cfg, svc, fake)
    assert _answers(fake) == ["The A1c was 7.2.\n\n" + beta.SAMPLE_FRAME]


def test_a_one_paragraph_crisis_answer_ends_with_the_frame(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply="Call 911 now.")
    _queue(c, agent_id, "chest pain", "fr-3")
    _work(cfg, svc, fake)
    assert _answers(fake) == ["Call 911 now.\n\n" + beta.SAMPLE_FRAME]


# --- what a crisis window reads ----------------------------------------------

def test_the_trailer_is_not_a_crisis_signal():
    """Every refusal ends with the 911 trailer; reading it as a crisis would
    hold every conversation that ever hit a limit on Sonnet."""
    from careagents import routing
    history = [{"role": "user", "content": "how are my labs?"},
               {"role": "assistant",
                "content": beta.with_911(beta.daily_limit_text())},
               {"role": "user", "content": "ok"}]
    assert not routing.crisis_in_window(history)
    assert routing.tier_for(history) == routing.TIER_HAIKU
    led = beta.with_911(beta.daily_limit_text(), "chest pain")
    history[1]["content"] = led
    assert routing.crisis_in_window(history)


def _chat_js():
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[1] / "careagents"
            / "static" / "chat.js").read_text(encoding="utf-8")


def test_the_pace_sentence_matches_the_page():
    import re
    from careagents.app import PACE_TEXT
    m = re.search(r'const PACE_TEXT = "([^"]+)";', _chat_js())
    assert m and m.group(1) == PACE_TEXT


def test_the_page_says_a_503s_sentence_after_its_retries():
    """A crisis 503 (`emergency`) is said at once. Any other is retried
    silently, as a blip usually passes, and its sentence, which ends with
    the 911 line, replaces "Connection hiccup" once the retries run out.
    Only for the POST that asked (`!state.runId`). The branch sits inside
    the streaming loop, past the reach of a node harness, so its source is
    pinned, as the 429 branch's is."""
    src = _chat_js()
    send = src[src.index("async function send("):]
    send = send[:send.index("\n  }\n")]
    head = "if (resp.status === 503 && !state.runId) {"
    branch = send.split(head, 1)[1].split("if (!resp.ok", 1)[0]
    now, later = branch.split("refusalText = refusal.message;", 1)
    # Said at once only inside the emergency check, which then returns.
    flagged = now.split("if (refusal.emergency === true) {", 1)[1]
    assert "addAgentText(refusal.message);" in flagged
    assert "return;" in flagged
    assert "return;" not in now.split("if (refusal.emergency", 1)[0]
    # Anything else falls through to the throw that retries it.
    assert "return" not in later
    assert send.index(head) < send.index(
        'throw new Error("event stream unavailable")')
    assert "if (reconnectFailures > 5) throw streamError;" in send
    # Once the retries are spent: the sentence, else the hiccup line.
    spent = send.split("} catch (e) {", 1)[1]
    said = 'if (refusalText) addAgentText(refusalText, null, "connection");'
    assert spent.index(said) < spent.index('"Connection hiccup')


def test_the_history_drops_a_crisis_answers_inline_frame():
    """A sample crisis answer carries the frame at its end (or, defensively,
    between paragraphs); under the banner it is not said twice."""
    f = beta.SAMPLE_FRAME
    assert beta.under_banner(f"Call 911.\n\n{f}\n\nThe A1c was 7.2.") == (
        "Call 911.\n\nThe A1c was 7.2.")
    assert beta.under_banner(f"Call 911.\n\n{f}") == "Call 911."
    assert beta.under_banner(f"{f}\n\nThe A1c was 7.2.") == (
        "The A1c was 7.2.")
    assert beta.under_banner(f"The {f} rest") == f"The {f} rest"
    assert beta.under_banner(f) == f                 # never down to nothing
    who = beta.sample_frame("your parent")
    assert beta.under_banner(f"Call 911.\n\n{who}\n\nx", who) == (
        "Call 911.\n\nx")


def test_a_replayed_sample_crisis_answer_shows_the_frame_once(
        cfg, svc, monkeypatch):  # noqa: F811
    answer = "Call 911 now.\n\nIn these made-up records, nothing urgent."
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply=answer)
    _queue(c, agent_id, "me duele el pecho", "fr-4")
    _work(cfg, svc, fake)
    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert page.count(beta.SAMPLE_FRAME) == 1      # the banner alone
    assert "Call 911 now." in page


# --- round 5: the model's refusal, a silent failed run, the page's own line ---

def test_an_ordinary_model_refusal_ends_with_911(cfg, svc, monkeypatch):  # noqa: F811
    from careagents import llm
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply=llm.REFUSAL_TEXT)
    _queue(c, agent_id, "how are my labs?", "rf-1")
    _work(cfg, svc, fake)
    assert _answers(fake) == [llm.REFUSAL_TEXT + "\n\n"
                              + beta.EMERGENCY_TRAILER + "\n\n"
                              + beta.SAMPLE_FRAME]


def test_a_model_answer_is_never_given_a_trailer(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply="The A1c was 7.2.")
    _queue(c, agent_id, "how is my a1c?", "rf-2")
    _work(cfg, svc, fake)
    assert beta.EMERGENCY_TRAILER not in _answers(fake)[-1]


def _frames(r):
    import json
    return [json.loads(line[len("data: "):])
            for line in r.get_data(as_text=True).splitlines()
            if line.startswith("data: ")]


def test_a_failed_run_that_said_its_failure_is_not_said_twice(
        cfg, svc, monkeypatch):  # noqa: F811
    """The worker's agent.error is the run's sentence; a stream read later
    from any cursor adds no second one."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("synthetic provider outage")
    monkeypatch.setattr("careagents.worker.llm.complete", _boom)
    _queue(c, agent_id, "how are my labs?", "tw-1")
    _work(cfg, svc, fake)
    run_id = next(iter(fake.runs))
    last = max(e["id"] for e in fake.events[run_id])
    for after in (0, last):
        r = c.get(f"/api/chat/runs/{run_id}/events",
                  query_string={"agent_id": agent_id, "after": after})
        errors_seen = [f for f in _frames(r) if f.get("type") == "error"]
        assert len(errors_seen) == (1 if after == 0 else 0), errors_seen


def test_the_pages_own_failure_line_is_the_trailer():
    import re
    m = re.search(r'addAgentText\("Connection hiccup — try that again\.'
                  r'\\n\\n([^"]+)", null, "connection"\);', _chat_js())
    assert m and m.group(1) == beta.EMERGENCY_TRAILER


# --- round 6: the decline signal, an empty answer, OpenAI's refusal ----------

class _OpenAIResp:
    status_code = 200
    headers: dict = {}

    def __init__(self, message):
        self._message = message

    def json(self):
        return {"choices": [{"message": self._message}]}


def _openai_turn(monkeypatch, message):
    from types import SimpleNamespace

    from careagents import llm
    monkeypatch.setattr(llm.requests, "post",
                        lambda *a, **k: _OpenAIResp(message))
    ocfg = SimpleNamespace(openai_base="http://openai.invalid/v1",
                           openai_api_key="k", openai_model="m",
                           run_deadline_seconds=120)
    return llm._openai_complete(ocfg, "system", [
        {"role": "user", "content": "hi"}], [])


def test_an_openai_refusal_is_a_refusal(monkeypatch):
    """OpenAI declines with `message.refusal` and no content: read, it is
    the same refusal as Anthropic's, our sentence and never the provider's
    wording."""
    from careagents import llm
    turn = _openai_turn(monkeypatch, {
        "role": "assistant", "content": None,
        "refusal": "I'm sorry, I cannot assist with that request."})
    assert turn.stop_reason == "refusal"
    assert turn.text == llm.REFUSAL_TEXT
    plain = _openai_turn(monkeypatch, {"role": "assistant",
                                       "content": "The A1c was 7.2."})
    assert plain.stop_reason == "" and plain.text == "The A1c was 7.2."


def _declined(text):
    """A turn the model declined after writing `text`, as llm.complete
    returns it."""
    from careagents import llm
    return llm.LLMTurn(text=text, stop_reason="refusal")


@pytest.mark.parametrize("said,lead", (
    ("quiero morir", beta.EMERGENCY_LINE + "\n" + beta.SELF_HARM_LINE),
    ("how are my labs?", "")))
def test_a_declined_partial_answer_is_only_the_fixed_sentence(
        cfg, svc, monkeypatch, said, lead):  # noqa: F811
    """Security round 6: what the model wrote before it declined is what
    it declined. It is never shown, stored, or kept on the checkpoint the
    next round or a recovered run reads."""
    from careagents import agent as agent_mod
    from careagents import llm
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(
        agent_mod.llm, "complete",
        lambda *a, **k: _declined("SYNTHETIC-PARTIAL I hear how much pain"))
    _queue(c, agent_id, said, "dp-1")
    _work(cfg, svc, fake)
    [out] = _answers(fake)
    if lead:
        assert out == (f"{lead}\n\n{llm.REFUSAL_TEXT}\n\n"
                       f"{beta.SAMPLE_FRAME}")
    else:
        assert out == (f"{llm.REFUSAL_TEXT}\n\n{beta.EMERGENCY_TRAILER}"
                       f"\n\n{beta.SAMPLE_FRAME}")
    run_id = next(iter(fake.runs))
    assert "SYNTHETIC-PARTIAL" not in str(fake.events[run_id])
    assert "SYNTHETIC-PARTIAL" not in str(fake.logged)


@pytest.mark.parametrize("reply", ("", "   \n  "))
def test_an_empty_answer_is_a_failure_with_911(cfg, svc, monkeypatch, reply):  # noqa: F811
    from careagents.agent import GENERIC_FAILURE_TEXT
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply=reply)
    _queue(c, agent_id, "how are my labs?", "empty-1")
    _work(cfg, svc, fake)
    assert _answers(fake) == [GENERIC_FAILURE_TEXT + "\n\n"
                              + beta.EMERGENCY_TRAILER + "\n\n"
                              + beta.SAMPLE_FRAME]


def test_the_911_and_988_lines_keep_their_line_break_on_a_sample():
    """_drop_echo used to rejoin the first paragraph's sentences with
    spaces, putting the 911 and 988 lines on one line."""
    from careagents.worker import _drop_echo
    led = beta.with_911("Reply.", "quiero morir")
    assert _drop_echo(led, beta.SAMPLE_FRAME) == led
    echoed = beta.SAMPLE_FRAME + " " + led
    assert _drop_echo(echoed, beta.SAMPLE_FRAME) == led


# --- the live-model check: frame placement off the word list ------------------

def test_the_ok_after_a_crisis_gets_the_frame_last(cfg, svc, monkeypatch):  # noqa: F811
    """The live check: the "ok" after "no quiero vivir" opened with the
    frame. Placement reads the conversation; the 911 lines read only the
    current message, so "ok" itself gets no emergency line."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply="Estoy aquí contigo.")
    _queue(c, agent_id, "no quiero vivir", "win-1")
    _work(cfg, svc, fake)
    _queue(c, agent_id, "ok", "win-2")
    _work(cfg, svc, fake)
    first, second = _answers(fake)
    assert second == "Estoy aquí contigo.\n\n" + beta.SAMPLE_FRAME
    assert beta.EMERGENCY_LINE not in second


def test_an_answer_that_gives_911_gets_the_frame_last(cfg, svc, monkeypatch):  # noqa: F811
    """Wording the list misses, that the model still answers with 911."""
    answer = "I'm sorry.\n\nCall 911 now."
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply=answer)
    _queue(c, agent_id, "my uncle looks very strange", "num-1")
    _work(cfg, svc, fake)
    assert _answers(fake) == [answer + "\n\n" + beta.SAMPLE_FRAME]


# --- security round 6: the narrowed patterns ---------------------------------

def test_kill_me_only_as_a_wish_or_a_dose():
    from careagents import routing
    for text in ("I want someone to kill me", "I want it to kill me",
                 "kill me now", "how many pills would it take to kill me",
                 "what dose would kill me"):
        assert routing.is_self_harm(text), text
    for text in ("this traffic is going to kill me",
                 "my mom will kill me", "these stairs will kill me"):
        assert not routing.is_crisis(text), text


def test_too_many_pills_is_911_only():
    from careagents import routing
    for text in ("I took too many pills", "she swallowed too many",
                 "I took way too many vitamins"):
        assert routing.is_crisis(text), text
        assert not routing.is_self_harm(text), text
    assert not routing.is_crisis("too many meds to keep track of")


# --- the frame survives the split into texts ---------------------------------
#
# The made-up-records line closes every sample answer (round 8), so on a
# text surface it is in the last part. split_reply cuts a long answer to
# four parts; it cuts the body to make room, never the line.

def _split(text, agent_id="ag_1"):
    from careagents import imessage
    return imessage.split_reply(text, "https://care.example", agent_id)


def test_a_short_sample_answer_is_one_text_ending_with_the_frame():
    text = f"The A1c was 7.2.\n\n{beta.SAMPLE_FRAME}"
    assert _split(text) == [text]


@pytest.mark.parametrize("frame", (beta.SAMPLE_FRAME,
                                   beta.sample_frame("your parent")))
@pytest.mark.parametrize("paragraphs", (6, 4))
def test_a_long_sample_answer_keeps_the_frame_in_its_last_text(
        frame, paragraphs):
    from careagents import imessage
    body = "\n\n".join(f"Part {i}. " + "z" * 950 for i in range(paragraphs))
    parts = _split(f"{body}\n\n{frame}")
    assert len(parts) == imessage.MAX_TEXT_PARTS
    assert all(len(p) <= imessage.TEXT_PART_LIMIT for p in parts)
    assert parts[-1].endswith("\n\n" + frame)
    assert ("The rest is in your chat: https://care.example/chat?agent=ag_1"
            in parts[-1])
    assert sum(p.count(frame) for p in parts) == 1


def test_a_sample_answer_that_just_fits_adds_no_link():
    from careagents import imessage
    body = "\n\n".join(f"Part {i}. " + "z" * 950 for i in range(3))
    parts = _split(f"{body}\n\n{beta.SAMPLE_FRAME}")
    assert len(parts) == imessage.MAX_TEXT_PARTS
    assert parts[-1] == beta.SAMPLE_FRAME


def test_a_card_link_after_the_frame_moves_ahead_of_it():
    from careagents import imessage
    events = [{"type": "agent.text",
               "payload": {"text": f"Here it is.\n\n{beta.SAMPLE_FRAME}"}},
              {"type": "agent.card",
               "payload": {"type": "card", "kind": "brief"}}]
    reply = imessage.run_reply(events, "https://care.example", "ag_1")
    [part] = _split(reply)
    assert part.startswith("Here it is.")
    assert part.endswith("\n\n" + beta.SAMPLE_FRAME)
    assert "visit brief" in part


def test_a_real_answer_is_split_as_before():
    from careagents import imessage
    body = "\n\n".join(f"Part {i}. " + "z" * 950 for i in range(6))
    parts = _split(body)
    assert len(parts) == imessage.MAX_TEXT_PARTS
    assert parts[-1].endswith(
        "The rest is in your chat: https://care.example/chat?agent=ag_1")
    assert beta.SAMPLE_FRAME not in "".join(parts)
