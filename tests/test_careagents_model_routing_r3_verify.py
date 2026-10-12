"""Security review of #931, round three (fixes at 2ecdf65).

Checks the R1-R3 fixes through the real anthropic SDK over an
httpx.MockTransport. The one strict-xfail row pins a remaining gap: a 429 on
the advised call is neither retried nor answered without the advisor.
Synthetic data only, no network.
"""
from __future__ import annotations

import json
import logging
import types

import pytest

from careagents import llm, routing

CANARY = "ZZCANARY-R3"


def _cfg(**over):
    base = dict(provider="anthropic", anthropic_api_key="k",
                anthropic_model="claude-sonnet-5-5", anthropic_oauth_token="",
                anthropic_oauth_beta="oauth-2025-04-20",
                run_deadline_seconds=120, anthropic_max_tokens=8000,
                anthropic_effort="medium", routing=True, advisor=True,
                database_url="")
    base.update(over)
    return types.SimpleNamespace(**base)


def _ok(model, content=None, stop="end_turn"):
    import httpx
    return httpx.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": model,
        "stop_reason": stop, "stop_sequence": None,
        "content": content or [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 1, "output_tokens": 1}})


def _err(status):
    import httpx
    kind = {429: "rate_limit_error", 529: "overloaded_error"}.get(
        status, "api_error")
    return httpx.Response(status, json={"type": "error", "error": {
        "type": kind, "message": "synthetic"}})


def _sdk(monkeypatch, script):
    import anthropic
    import httpx
    log: list[dict] = []
    clients: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        log.append({"body": body, "client": clients[-1],
                    "beta": request.headers.get("anthropic-beta", "")})
        return script(len(log), body)

    real = anthropic.Anthropic

    def wrapped(**kw):
        clients.append({k: kw.get(k) for k in ("timeout", "max_retries")})
        return real(http_client=httpx.Client(
            transport=httpx.MockTransport(handler)), **kw)

    monkeypatch.setattr(anthropic, "Anthropic", wrapped)
    monkeypatch.setattr(anthropic._base_client.BaseClient,
                        "_calculate_retry_timeout",
                        lambda *a, **k: 0.0, raising=False)
    return log


def _advised(entry) -> bool:
    return any(t.get("type") == routing.ADVISOR_TOOL["type"]
               for t in entry["body"].get("tools") or [])


def _counts(monkeypatch):
    got: list[dict] = []
    monkeypatch.setattr(routing, "count", lambda d: got.append(dict(d)))
    return got


# --- R1 ---------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "bye \U0001F48A\U0001F48A \U0001FAA6", "thanks \U0001F52A",
    "hi \u2620\ufe0f", "thanks \U0001F198",
    "hi \u0301", "good night \u2764", "hi \u00e9",
    "how do i delete my account \U0001F52B",
    "hi\u200b\U0001F48A",          # a format char stripped, the pill kept
])
def test_any_non_ascii_character_goes_to_sonnet(text):
    assert routing.classify(text) == routing.TIER_SONNET, repr(text)


@pytest.mark.parametrize("text", [
    "hi", "thanks!", "how do i add a passkey", "Thank you so much!",
    # NFKC folds these to ASCII (no-break space, ellipsis): intended
    "ok \u00a0thanks", "thanks\u2026"])
def test_plain_small_talk_still_goes_to_haiku(text):
    assert routing.classify(text) == routing.TIER_HAIKU, text


# --- R2 ---------------------------------------------------------------------

@pytest.mark.parametrize("failure", [500, 502, 503, 529, "connect"])
def test_a_failed_advised_call_is_answered_without_the_advisor(
        monkeypatch, caplog, failure):
    import httpx
    counted = _counts(monkeypatch)

    def script(n, body):
        if n == 1:
            if failure == "connect":
                raise httpx.ConnectError("synthetic")
            return _err(failure)
        return _ok(body["model"])

    log = _sdk(monkeypatch, script)
    with caplog.at_level(logging.DEBUG, logger="careagents"):
        turn = llm.complete(_cfg(), "sys", [
            {"role": "user", "content": f"is {CANARY} high?"}], [])
    assert turn.text == "ok" and turn.advisor_failed
    assert [_advised(e) for e in log] == [True, False]
    assert log[0]["client"]["max_retries"] == 0
    assert routing.ADVISOR_SPENT_NOTE.strip()[:20] in log[1]["body"]["system"]
    assert counted[-1]["advisor_consults"] == 0
    assert counted[-1]["advisor_errors"] == 1
    assert CANARY not in caplog.text


def test_the_fallback_itself_failing_still_fails_the_turn(monkeypatch):
    log = _sdk(monkeypatch, lambda n, body: _err(529))
    with pytest.raises(llm.LLMError):
        llm.complete(_cfg(), "sys",
                     [{"role": "user", "content": "How is my A1c?"}], [])
    # one advised attempt, then the fallback and its one retry
    assert [_advised(e) for e in log] == [True, False, False]


@pytest.mark.xfail(strict=True, reason="#931 round three: a 429 on the "
                   "advised call (max_retries=0) fails the turn at once; "
                   "before #931 the SDK retried it twice")
def test_a_429_on_the_advised_call_is_survived(monkeypatch):
    def script(n, body):
        return _err(429) if n == 1 else _ok(body["model"])

    _sdk(monkeypatch, script)
    turn = llm.complete(_cfg(), "sys",
                        [{"role": "user", "content": "How is my A1c?"}], [])
    assert turn.text == "ok"


def test_routing_off_still_retries_5xx_twice(monkeypatch):
    log = _sdk(monkeypatch, lambda n, body: _err(529) if n < 3
               else _ok(body["model"]))
    turn = llm.complete(_cfg(routing=False, advisor=False), "sys",
                        [{"role": "user", "content": "How is my A1c?"}], [])
    assert turn.text == "ok" and len(log) == 3


# --- R3 ---------------------------------------------------------------------

def test_a_failed_advised_attempt_spends_the_turns_consult(monkeypatch):
    import httpx

    def script(n, body):
        if n == 1:
            raise httpx.ReadTimeout("synthetic timeout")
        if n == 2:
            return _ok(body["model"], [{"type": "tool_use", "id": "t1",
                                        "name": "get_labs", "input": {}}],
                       stop="tool_use")
        return _ok(body["model"])

    log = _sdk(monkeypatch, script)
    history = [{"role": "user", "content": "How is my A1c?"}]
    first = llm.complete(_cfg(), "sys", history, [])
    assert first.advisor_timed_out and first.advisor_calls == 1
    history += [{"role": "assistant", "content": first.text,
                 "tool_calls": [{"id": "t1", "name": "get_labs",
                                 "arguments": {}}],
                 "advisor_calls": first.advisor_calls},
                {"role": "tool", "tool_call_id": "t1", "content": "{}"}]
    llm.complete(_cfg(), "sys", history, [])
    assert [_advised(e) for e in log] == [True, False, False]
    advised, fallback, ordinary = (e["client"] for e in log)
    assert advised == {"timeout": 54.0, "max_retries": 0}
    assert fallback == {"timeout": 18.0, "max_retries": 1}
    # worst case of the advised round plus one first attempt of the next
    # round, before SDK backoff, inside the 120s run deadline
    worst = (advised["timeout"]
             + fallback["timeout"] * (1 + fallback["max_retries"])
             + ordinary["timeout"])
    assert worst <= 120, worst
