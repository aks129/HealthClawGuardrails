"""Security review of PR #1017, round three, at a6fee38f.

beta.with_911 now puts a 911 line on every refusal and failure: first when
the current message reads as a crisis, else as a trailer. These tests fail
on a6fee38f by design. Each is a refusal or failure the person can still
get with no 911 line at all, or a pattern that is still quadratic.

Synthetic text only.
"""

from __future__ import annotations

import json
import pathlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from careagents import beta, imessage, llm, routing, sendblue_surface
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)
from tests.test_careagents_imessage import PHONE, _pair


def _carries_911(text: str) -> bool:
    return (text.startswith(beta.EMERGENCY_LINE)
            or beta.EMERGENCY_LINE in text
            or text.rstrip().endswith(beta.EMERGENCY_TRAILER))


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
    w = RunWorker(cfg, fake, svc, "round3-worker")
    while w.run_once():
        pass


# --- the model's own refusal --------------------------------------------------

@pytest.mark.parametrize("said", ("quiero morir", "I want to kill myself",
                                  "me duele el pecho"))
def test_a_model_refusal_of_crisis_wording_carries_911(
        cfg, svc, monkeypatch, said):  # noqa: F811
    """llm.complete answers a stop_reason "refusal" with REFUSAL_TEXT
    (careagents/llm.py), the likeliest fixed reply to self-harm wording.
    The worker finishes it as an ordinary answer, outside with_911."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply=llm.REFUSAL_TEXT)
    _queue(c, agent_id, said, "refused-1")
    _work(cfg, svc, fake)
    answers = [row["content"] for rows in fake.logged.values()
               for row in rows if row["role"] == "assistant"]
    assert llm.REFUSAL_TEXT in answers[-1]
    assert _carries_911(answers[-1]), answers[-1]


# --- a crisis run the engine sweeps, seen on the web ---------------------------

def test_web_stream_of_a_swept_crisis_run_carries_911(cfg, svc, monkeypatch):  # noqa: F811
    """The engine fails an overdue queued run with no answer. The relay now
    gets run_reply's fallback with the 911 line; the web stream ends with
    a bare `done: failed`, and chat.js shows nothing at all."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    run_id = _queue(c, agent_id, "no puedo respirar", "swept-web")
    fake.runs[run_id]["deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    r = c.get(f"/api/chat/runs/{run_id}/events",
              query_string={"agent_id": agent_id})
    frames = [json.loads(line[len("data: "):])
              for line in r.get_data(as_text=True).splitlines()
              if line.startswith("data: ")]
    assert {"type": "done", "status": "failed"}.items() <= frames[-1].items()
    said = [f.get("text", "") for f in frames
            if f.get("type") in ("text", "error")]
    assert any(_carries_911(t) for t in said), frames


# --- the page's own failure sentence --------------------------------------------

def test_the_page_connection_failure_carries_911():
    """When no server sentence arrives (a proxy 502, a dropped network, a
    503 without JSON) the page says "Connection hiccup", with no 911 line.
    The page cannot read crisis wording, so the trailer belongs there."""
    src = (pathlib.Path(__file__).resolve().parents[1] / "careagents"
           / "static" / "chat.js").read_text(encoding="utf-8")
    line = next(ln for ln in src.splitlines()
                if "addAgentText(" in ln and "Connection hiccup" in ln)
    assert "911" in line, line.strip()


# --- a transport's own refusal ---------------------------------------------------

def test_a_transport_refusal_carries_911(cfg, svc, monkeypatch):  # noqa: F811
    """`transport_block` (the real-records pointer on Sendblue and the
    relay) returns before the refusal() helper, so it gets the 911 line
    only when the wording is caught as a crisis. Every other refusal on
    the text path ends with the trailer."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    deps = app.extensions["careagents_imessage"]
    pointer = sendblue_surface.real_records_text("https://example.test")
    body, _status = imessage.handle_inbound(
        deps, PHONE, "hola",
        transport_block=lambda ctx: pointer)
    assert body["reply"].startswith(pointer)
    assert _carries_911(body["reply"]), body["reply"]


# --- crisis wording still missed (leads the reply, keeps the window) ---------

STILL_MISSED = (
    "my mom is not waking up", "he won't wake up", "she is unresponsive",
    "he collapsed", "i'm dying", "i am dying", "cant breath",
    "i can not breath", "sangra mucho", "no puedo hablar", "se desplomó",
)


@pytest.mark.parametrize("said", STILL_MISSED)
def test_crisis_wording_is_crisis(said):
    assert routing.is_crisis(said), said


# --- still quadratic ---------------------------------------------------------------

@pytest.mark.parametrize("unit", ("cort", "perd"))
def test_is_crisis_stays_linear(unit):
    """`cort\\w* las venas` and `perd\\w* el conocimiento`: an unbounded
    \\w* before a literal, from every start in a long word. 32k
    characters take about 0.35 s on a6fee38f and quadruple per doubling."""
    text = unit * (32_000 // len(unit))
    start = time.perf_counter()
    routing.is_crisis(text)
    assert time.perf_counter() - start < 0.1


# --- what holds ----------------------------------------------------------------------

def test_with_911_never_echoes_and_is_idempotent():
    for said in ("chest pain <script>x</script>", "hola", ""):
        for reply in ("Fixed.", beta.with_911("Fixed."),
                      beta.with_911("Fixed.", "quiero morir")):
            out = beta.with_911(reply, said)
            assert "script" not in out
            assert _carries_911(out)
            assert out.count(beta.EMERGENCY_TRAILER) <= 1


def test_the_trailer_alone_never_makes_a_conversation_sticky():
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant",
                 "content": beta.with_911("Something went wrong.")},
                {"role": "user", "content": "ok"}]
    assert not routing.crisis_in_window(messages)
    assert routing.tier_for(messages) == routing.TIER_HAIKU
    led = beta.with_911("Something went wrong.", "chest pain")
    messages[1]["content"] = led
    assert routing.crisis_in_window(messages)
