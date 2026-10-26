"""QA for #1037, fix round: the conversation's language, on the real engine.

The fix moved the turn's language from the POST to the worker, which reads
the conversation's history (routing.conversation_language) and appends an
`agent.lang` event. The PR's carry-over test runs on FakeClient, whose
history is whatever the fake keeps. These run two turns through the real
HealthClawClient onto the real engine WSGI, so the history the worker
reads is the engine's transcript.

They also pin what the fix round still gets wrong: lowercase names and
accent-free Portuguese. Synthetic data only.
"""
# ruff: noqa: F811  (the cfg and svc fixtures, imported then requested)

from __future__ import annotations

import json

import pytest

from careagents import routing
from careagents.agent import SAMPLE_TOOL_LABELS, SAMPLE_TOOL_LABELS_ES
from tests.test_beta_acceptance_rows import Chain
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, _turn, cfg, svc)
from tests.test_careagents_sample_framing import _real_chat_app
from tests.test_careagents_tone_and_spanish_labels import _labels_for

SPANISH = "¿Cuáles son mis análisis más recientes?"
ENGLISH = "What are my most recent labs?"


def _frames(body: str) -> list[dict]:
    return [json.loads(line[len("data: "):])
            for block in body.split("\n\n") for line in block.splitlines()
            if line.startswith("data: ")]


def _two_turns(cfg, svc, monkeypatch, first, second):
    """Two turns that each read the labs, on one real engine. Returns each
    turn's browser frames."""
    from careagents import agent as agent_mod
    from careagents.worker import RunWorker

    def _ask(n):
        class _Call:
            id, name, arguments = f"qa1037r2-c{n}", "get_labs", {}

        class _Ask:
            text, tool_calls, raw_tool_calls = "", [_Call()], []
        return _Ask()

    class _Done:
        text, tool_calls, raw_tool_calls = "Listo.", [], []

    turns = iter([_ask(1), _Done(), _ask(2), _Done()])
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: next(turns))
    chain = Chain(cfg, svc, monkeypatch)
    c = chain.app.test_client()
    _login(c, svc, monkeypatch, email="acceptance@example.com")
    RunWorker(cfg, chain.hc, svc, "qa1037r2-worker").run_once()
    out = []
    for i, message in enumerate((first, second)):
        r = _turn(c, chain.agent, message, request_id=f"qa1037r2-{i}")
        assert r.status_code == 200
        out.append(_frames(r.get_data(as_text=True)))
    return out


def _lang_and_labels(frames):
    return ([f for f in frames if f["type"] == "lang"],
            [f["label"] for f in frames if f["type"] == "tool"])


def test_si_after_a_spanish_turn_stays_spanish_on_the_real_engine(
        cfg, svc, monkeypatch):
    """F4 against the engine's own transcript, not a fake's history."""
    first, second = _two_turns(cfg, svc, monkeypatch, SPANISH, "sí")
    for frames in (first, second):
        langs, labels = _lang_and_labels(frames)
        assert langs == [{"type": "lang", "lang": "es"}], frames
        assert labels == [SAMPLE_TOOL_LABELS_ES["get_labs"]], frames


def test_ok_after_an_english_turn_stays_english_on_the_real_engine(
        cfg, svc, monkeypatch):
    first, second = _two_turns(cfg, svc, monkeypatch, ENGLISH, "ok")
    for frames in (first, second):
        langs, labels = _lang_and_labels(frames)
        assert langs == [], frames
        assert labels == [SAMPLE_TOOL_LABELS["get_labs"]], frames


def test_an_english_turn_after_a_spanish_one_is_english_again(
        cfg, svc, monkeypatch):
    _first, second = _two_turns(cfg, svc, monkeypatch, SPANISH, ENGLISH)
    langs, labels = _lang_and_labels(second)
    assert langs == [] and labels == [SAMPLE_TOOL_LABELS["get_labs"]]


@pytest.mark.parametrize("value", [
    "ES", "es ", " es", "fr", "en", "", None, ["es"], {"lang": "es"}, 1,
    "es<script>"])
def test_only_the_exact_code_es_reaches_the_page(cfg, svc, monkeypatch,
                                                 value):
    """Whatever an agent.lang payload holds, the browser sees a lang frame
    only for exactly "es"."""
    c, fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    _labels_for(c, fake, agent_id, cfg, svc, monkeypatch, ENGLISH)
    [run_id] = list(fake.events)
    fake._append_run_event(run_id, "agent.lang", {"lang": value})
    r = c.get(f"/api/chat/runs/{run_id}/events",
              query_string={"agent_id": agent_id, "after": 0})
    frames = _frames(r.get_data(as_text=True))
    assert [f for f in frames if f["type"] == "lang"] == [], frames


@pytest.mark.parametrize("text", [
    "Clinica Mi Salud Para Todos",
    "We use Farmacia Mi Salud Por Favor",
])
def test_a_capitalized_spanish_name_is_not_counted(text):
    """MUTATION: drop the capitalized-word filter in _content_words ->
    "es". The particle filter alone does not cover a name made of other
    Spanish words (the answer to "which pharmacy?")."""
    assert routing.turn_language(text) != "es"


# --- what the fix round still reads wrong ------------------------------------

#: English typed in lower case on a phone, naming a Spanish surname or
#: place. With no capital, the name filter does not see a name, and the
#: particles count as two Spanish words against at most one English one.
LOWERCASE_ENGLISH = [
    "appt with dr de la rosa",
    "seeing dr de los santos tmrw",
    "dr de la cruz",
    "plaza de la raza pharmacy",
    "clinica de la comunidad",
    "del mar la jolla",
]


@pytest.mark.xfail(strict=True, reason=(
    "QA #1037 R2-F1: lowercase English naming a Spanish surname or place "
    "reads as Spanish, after an English turn too. Remove with the fix."))
@pytest.mark.parametrize("text", LOWERCASE_ENGLISH)
def test_lowercase_english_with_spanish_names_is_not_spanish(text):
    assert routing.conversation_language([ENGLISH, text]) == "en"


@pytest.mark.xfail(strict=True, reason=(
    "QA #1037 R2-F5: Portuguese typed without ã/õ/ç (or with only é) reads "
    "as Spanish. Remove with the fix."))
@pytest.mark.parametrize("text", [
    "Quais sao os meus resultados de exames?",
    "Preciso saber sobre os meus medicamentos",
    "O que significa o resultado do meu exame de sangue?",
    "Meus resultados de sangue estao bons?",
])
def test_accent_free_portuguese_is_not_spanish(text):
    assert routing.turn_language(text) != "es"
