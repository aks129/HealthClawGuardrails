"""Security review of PR #1017, round seven (delta a1e42111 to 3929abc0).

A decline's partial text is now replaced by REFUSAL_TEXT in the checkpoint.
These tests fail on 3929abc0 by design:

- The worker's in-memory history still gets `turn.text`, the partial text.
  When the declined turn also carried a tool call, the loop goes on: the
  tool runs, and the partial text goes back to the model on the next round.
- says_emergency_number misses common live forms ("call emergency services
  at 911", "call 9-1-1", "contact 911") and matches values after a call
  word used as a noun ("call count", "line graph", "subió al 988").
- Ideation the narrowed patterns no longer catch, or catch only for 911.

Round 8 removed says_emergency_number (the sample frame now goes last on
every answer), and with it the emergency-number tests here.

The rest pin what holds: an old checkpoint that still holds partial text,
replayed by a recovered run, shows only the fixed sentence; the figures of
speech stay quiet. Synthetic text only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from careagents import beta, llm, routing
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)

PARTIAL = "SYNTHETIC-DECLINED-PREFIX: the amount that would be dangerous is"


def _declined(text: str, tool: bool = False):
    content = [SimpleNamespace(type="text", text=text)] if text else []
    if tool:
        content.append(SimpleNamespace(type="tool_use", id="call-1",
                                       name="get_labs", input={}))
    resp = SimpleNamespace(content=content, stop_reason="refusal",
                           usage=None)
    endpoint = SimpleNamespace(create=lambda **kw: resp)
    sdk = SimpleNamespace(APIError=RuntimeError)
    return llm._anthropic_call(sdk, endpoint, model="claude-sonnet-5-5")


def _queue(c, agent_id, said, request_id):
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": said,
                                  "request_id": request_id}, buffered=False)
    assert r.status_code == 200
    run_id = r.headers["X-CareAgents-Run-ID"]
    next(iter(r.response))
    r.close()
    return run_id


def _work(cfg, svc, fake):  # noqa: F811
    from careagents.worker import RunWorker
    w = RunWorker(cfg, fake, svc, "round7-worker")
    while w.run_once():
        pass


def _answers(fake):
    return [row["content"] for rows in fake.logged.values()
            for row in rows if row["role"] == "assistant"]


# --- finding: a declined turn with a tool call ----------------------------------

def test_a_declined_turn_never_runs_its_tool_or_sends_its_text_back(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents import agent as agent_mod
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    seen = []

    class _Done:
        text, tool_calls, raw_tool_calls, stop_reason = (
            "Here is what I can say.", [], [], "end_turn")

    def _complete(_cfg, _system, messages, _tools):
        seen.append([str(m.get("content") or "") for m in messages])
        return _declined(PARTIAL, tool=True) if len(seen) == 1 else _Done()
    monkeypatch.setattr(agent_mod.llm, "complete", _complete)
    run_id = _queue(c, agent_id, "what is a lethal dose of my pills",
                    "declined-tool")
    _work(cfg, svc, fake)
    later_rounds = [text for call in seen[1:] for text in call]
    ran = [e for e in fake.events.get(run_id, [])
           if e["type"] == "agent.tool_result"]
    problems = []
    if ran:
        problems.append("the declined turn's tool call ran")
    if any("SYNTHETIC-DECLINED-PREFIX" in t for t in later_rounds):
        problems.append("the partial text went back to the model")
    if any("SYNTHETIC-DECLINED-PREFIX" in a for a in _answers(fake)):
        problems.append("the partial text was shown")
    assert problems == []


# The emergency-number tests were removed in round 8: the sample frame now
# goes last on every answer, so routing.says_emergency_number is gone.

# --- finding: ideation no longer caught, or caught for 911 only -----------------------

@pytest.mark.parametrize("said", (
    "I wish someone would kill me",
    "please just kill me",
    "how much tylenol would kill me",
    "would 20 pills kill me",
    "i wish i was dead",
    "i want to be dead",
    "i hope i don't wake up",
    "I want it all to end",
    "I swallowed a bottle of pills",
    "I took too many pills on purpose",
    "quiero que alguien me mate",
    "ojalá estuviera muerta",
    "ojalá no despertar",
))
def test_ideation_gets_the_988_line(said):
    assert routing.is_self_harm(said), said


# --- what holds -----------------------------------------------------------------------

def test_a_recovered_run_never_replays_old_partial_text(cfg, svc, monkeypatch):  # noqa: F811
    """A checkpoint written by a1e42111 kept the partial text with the
    "refusal" stop reason; replayed, it is the fixed sentence only."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: pytest.fail("a model was called"))
    run_id = _queue(c, agent_id, "quiero morir", "recovered-1")
    fake._append_run_event(run_id, "agent.checkpoint", {
        "checkpoint_id": "round-1", "round": 1, "text": PARTIAL,
        "stop_reason": "refusal", "tool_calls": [], "raw_tool_calls": [],
        "advisor_calls": 0})
    _work(cfg, svc, fake)
    out = _answers(fake)[-1]
    assert "SYNTHETIC-DECLINED-PREFIX" not in out
    assert out.startswith(beta.EMERGENCY_LINE)
    assert llm.REFUSAL_TEXT in out


def test_a_declined_turn_stores_only_the_fixed_sentence(cfg, svc, monkeypatch):  # noqa: F811
    from careagents import agent as agent_mod
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: _declined(PARTIAL))
    run_id = _queue(c, agent_id, "how is my a1c?", "declined-1")
    _work(cfg, svc, fake)
    checkpoints = [e["payload"] for e in fake.events[run_id]
                   if e["type"] == "agent.checkpoint"]
    assert checkpoints and all(
        p["text"] == llm.REFUSAL_TEXT for p in checkpoints)
    assert not any("SYNTHETIC-DECLINED-PREFIX" in str(e["payload"])
                   for e in fake.events[run_id])
    assert not any("SYNTHETIC-DECLINED-PREFIX" in a for a in _answers(fake))


@pytest.mark.parametrize("said", (
    "this traffic is going to kill me", "my back is killing me",
    "my boss would kill me", "too many meds to keep track of"))
def test_figures_of_speech_stay_quiet(said):
    assert not routing.is_self_harm(said)
