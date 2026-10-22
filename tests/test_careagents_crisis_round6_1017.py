"""Security review of PR #1017, round six (delta a7943a91 to a1e42111).

These tests fail on a1e42111 by design:

- A decline that arrives after some text keeps that text and adds "(That
  answer stopped before it finished.)". The text the model wrote before
  it was stopped is what the decline was about, so it should not reach
  the person, or the stored history the next turn sends back.
- routing.says_emergency_number, which only decides where the sample line
  goes, reads 911 or 988 inside a lab value as the emergency number.
- New self-harm patterns put the 988 line on figures of speech ("this
  traffic is going to kill me") and on ordinary medication talk ("too
  many meds to keep track of").

The rest pin what holds. Synthetic text only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from careagents import beta, llm
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)


def _declined_with(text: str):
    """A real llm._anthropic_call on a response the API declined
    (stop_reason "refusal") after writing `text`."""
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)] if text else [],
        stop_reason="refusal", usage=None)
    endpoint = SimpleNamespace(create=lambda **kw: resp)
    sdk = SimpleNamespace(APIError=RuntimeError)
    return llm._anthropic_call(sdk, endpoint, model="claude-sonnet-5-5")


def _queue(c, agent_id, said, request_id):
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": said,
                                  "request_id": request_id}, buffered=False)
    assert r.status_code == 200
    next(iter(r.response))
    r.close()


def _answer(cfg, svc, fake):  # noqa: F811
    from careagents.worker import RunWorker
    w = RunWorker(cfg, fake, svc, "round6-worker")
    while w.run_once():
        pass
    return [row["content"] for rows in fake.logged.values()
            for row in rows if row["role"] == "assistant"][-1]


# --- finding: the declined part of an answer is shown -----------------------

PARTIAL = "SYNTHETIC-DECLINED-PREFIX: the amount that would be dangerous is"


@pytest.mark.parametrize("said", ("what is a lethal dose of my pills",
                                  "how is my a1c?"))
def test_a_partial_decline_never_shows_the_declined_text(
        cfg, svc, monkeypatch, said):  # noqa: F811
    from careagents import agent as agent_mod
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: _declined_with(PARTIAL))
    _queue(c, agent_id, said, "partial-1")
    out = _answer(cfg, svc, fake)
    assert beta.EMERGENCY_TRAILER in out or beta.EMERGENCY_LINE in out
    assert "SYNTHETIC-DECLINED-PREFIX" not in out, out


# The lab-value and number-check tests were removed in round 8: the sample
# frame now goes last on every answer, so says_emergency_number is gone.

# --- finding: the 988 line for figures of speech and ordinary medicine talk -----

@pytest.mark.parametrize("said", (
    "this traffic is going to kill me",
    "my mom will kill me if I miss the appointment",
    "this paperwork is going to kill me",
    "too many meds to keep track of",
    "I think I took too many vitamins today",
))
def test_ordinary_wording_gets_no_988_line(said):
    assert beta.SELF_HARM_LINE not in beta.with_911("Fixed reply.", said)


# --- what holds ---------------------------------------------------------------------

@pytest.mark.parametrize("decline", ("", llm.REFUSAL_TEXT))
def test_a_whole_decline_is_the_fixed_sentence_with_911_and_988(
        cfg, svc, monkeypatch, decline):  # noqa: F811
    from careagents import agent as agent_mod
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: _declined_with(decline))
    _queue(c, agent_id, "quiero morir", "whole-1")
    out = _answer(cfg, svc, fake)
    assert out.startswith(beta.EMERGENCY_LINE + "\n" + beta.SELF_HARM_LINE)
    assert llm.REFUSAL_TEXT in out
