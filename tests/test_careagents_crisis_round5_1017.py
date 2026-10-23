"""Security review of PR #1017, round five, at a7943a91.

The worker now detects the model's refusal by its exact text
(`text == llm.REFUSAL_TEXT`) and passes it through beta.with_911. Exact
equality means an answer that only quotes the sentence is left alone, as
it should be. It also means two declines are missed, and those tests fail
on a7943a91 by design:

- a refusal that arrives with some text already written: llm only puts
  REFUSAL_TEXT in when the declined response has no text at all;
- an empty answer, which the worker shows as "…". That is also what an
  OpenAI decline becomes, since `message.refusal` is never read.

Checking the turn's stop_reason, or treating an empty answer as a
failure, catches both. Synthetic text only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from careagents import beta, llm
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)


def _carries_911(text: str) -> bool:
    return (beta.EMERGENCY_LINE in text
            or text.rstrip().endswith(beta.EMERGENCY_TRAILER)
            or beta.EMERGENCY_TRAILER in text)


def _queue(c, agent_id, said, request_id):
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": said,
                                  "request_id": request_id}, buffered=False)
    assert r.status_code == 200
    next(iter(r.response))
    r.close()


def _answer(cfg, svc, fake):  # noqa: F811
    from careagents.worker import RunWorker
    w = RunWorker(cfg, fake, svc, "round5-worker")
    while w.run_once():
        pass
    return [row["content"] for rows in fake.logged.values()
            for row in rows if row["role"] == "assistant"][-1]


def _declined_with(text: str):
    """A real llm._anthropic_call on a response the API declined
    (stop_reason "refusal") after writing `text`."""
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)] if text else [],
        stop_reason="refusal", usage=None)
    endpoint = SimpleNamespace(create=lambda **kw: resp)
    sdk = SimpleNamespace(APIError=RuntimeError)
    return llm._anthropic_call(sdk, endpoint, model="claude-sonnet-5-5")


# --- finding: declines the exact-text check misses ---------------------------

def test_a_refusal_with_partial_text_is_not_the_refusal_sentence():
    """The mechanism: llm keeps the partial text and the stop_reason, and
    the worker never looks at stop_reason."""
    turn = _declined_with("Entiendo que estás pasando por algo muy ")
    assert turn.stop_reason == "refusal"
    assert turn.text != llm.REFUSAL_TEXT


@pytest.mark.parametrize("said", ("quiero morir", "I want to kill myself"))
def test_a_refusal_after_partial_text_carries_911(
        cfg, svc, monkeypatch, said):  # noqa: F811
    from careagents import agent as agent_mod
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(
        agent_mod.llm, "complete",
        lambda *a, **k: _declined_with("I hear how much pain you're "))
    _queue(c, agent_id, said, "partial-refusal")
    out = _answer(cfg, svc, fake)
    assert _carries_911(out), out


@pytest.mark.parametrize("said", ("quiero morir", "me duele el pecho"))
def test_an_empty_answer_to_crisis_wording_carries_911(
        cfg, svc, monkeypatch, said):  # noqa: F811
    """An empty answer is shown as "…", with nothing else."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch, reply="")
    _queue(c, agent_id, said, "empty-answer")
    out = _answer(cfg, svc, fake)
    assert _carries_911(out), out


# --- what holds: no false trigger ------------------------------------------------

QUOTING = (
    "If I ever say \"" + llm.REFUSAL_TEXT + "\", tap Tell us.",
    llm.REFUSAL_TEXT + " That said, your A1c of 6.1 is in range.",
    "Your last A1c was 6.1.\n\n" + llm.REFUSAL_TEXT,
    "I can't help with that one, but your doctor can.",
)


@pytest.mark.parametrize("reply", QUOTING)
def test_an_answer_that_quotes_the_refusal_is_left_alone(
        cfg, svc, monkeypatch, reply):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply=reply)
    _queue(c, agent_id, "how is my a1c?", "quote-1")
    out = _answer(cfg, svc, fake)
    assert beta.EMERGENCY_TRAILER not in out
    assert beta.EMERGENCY_LINE not in out
    assert reply in out


def test_the_exact_refusal_carries_the_trailer_when_not_a_crisis(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(
        cfg, svc, monkeypatch, reply="  " + llm.REFUSAL_TEXT + "\n")
    _queue(c, agent_id, "how is my a1c?", "exact-1")
    out = _answer(cfg, svc, fake)
    assert out.count(beta.EMERGENCY_TRAILER) == 1
    assert not out.startswith(beta.EMERGENCY_LINE)


def test_an_empty_refusal_from_the_api_is_wrapped(cfg, svc, monkeypatch):  # noqa: F811
    from careagents import agent as agent_mod
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: _declined_with(""))
    _queue(c, agent_id, "quiero morir", "empty-refusal")
    out = _answer(cfg, svc, fake)
    # On a sample answer _drop_echo rejoins the first paragraph's sentences
    # with spaces, so the line break between the two lines becomes a space.
    assert out.startswith(beta.EMERGENCY_LINE)
    assert beta.SELF_HARM_LINE in out
    assert out.index(beta.SELF_HARM_LINE) < out.index(llm.REFUSAL_TEXT)
