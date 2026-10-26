"""QA for #1037: Spanish status lines and the turn's language, beyond fakes.

The PR's own label tests run the worker against FakeClient, which stores
any payload. These drive a Spanish turn on the SAMPLE connection through
the real HealthClawClient onto the real engine WSGI (tests'
`Chain`), so the accented label is the engine's round trip: written by
append_agent_run_event, read back by get_agent_run_events, and streamed to
the page by /api/chat.

They also pin the detector's English bias on sentences a US patient
writes every day, and the composed prompt the cheerful tone is part of.

Synthetic data only.
"""
# ruff: noqa: F811  (the cfg and svc fixtures, imported then requested)

from __future__ import annotations

import json

import pytest

from careagents import personas, routing
from careagents.agent import SAMPLE_TOOL_LABELS_ES, TOOL_LABELS
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, _turn, cfg, svc)


def _frames(body: str) -> list[dict]:
    out = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):]))
    return out


def _spanish_sample_turn(cfg, svc, monkeypatch, message):
    """One turn that reads the labs, then answers, on the real engine."""
    from careagents import agent as agent_mod

    class _Call:
        id, name, arguments = "qa1037-c1", "get_labs", {}

    class _Ask:
        text, tool_calls, raw_tool_calls = "", [_Call()], []

    class _Done:
        text = "Estos son análisis de muestra, no los suyos."
        tool_calls, raw_tool_calls = [], []

    turns = iter([_Ask(), _Done()])
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: next(turns))
    chain = Chain(cfg, svc, monkeypatch)
    c = chain.app.test_client()
    _login(c, svc, monkeypatch, email="acceptance@example.com")
    # A worker has to have checked in, or /api/chat refuses the turn (503).
    from careagents.worker import RunWorker
    RunWorker(cfg, chain.hc, svc, "qa1037-worker").run_once()
    r = _turn(c, chain.agent, message, request_id="qa1037-es-1")
    assert r.status_code == 200
    return chain, _frames(r.get_data(as_text=True))


def _engine_tool_labels(chain):
    from r6.agent_runs.models import AgentRun, AgentRunEvent
    with chain.engine_app.app_context():
        [run] = AgentRun.query.filter_by(tenant_id=TENANT).all()
        rows = AgentRunEvent.query.filter_by(run_id=run.id).all()
        return [r.to_dict() for r in rows]


def test_a_spanish_sample_turn_round_trips_its_label_on_the_real_engine(
        cfg, svc, monkeypatch):
    """The sample set (never "su/sus"), accent intact, on the wire the
    browser reads, and the `accepted` language agrees with the worker."""
    chain, frames = _spanish_sample_turn(
        cfg, svc, monkeypatch, "¿Cuáles son mis análisis más recientes?")
    accepted = [f for f in frames if f["type"] == "accepted"]
    tools = [f for f in frames if f["type"] == "tool"]
    texts = [f for f in frames if f["type"] == "text"]
    assert accepted and accepted[0].get("lang") == "es", frames
    assert [t["label"] for t in tools] == [SAMPLE_TOOL_LABELS_ES["get_labs"]]
    assert tools[0]["label"] == "Interpretando los análisis inventados"
    assert texts and "análisis" in texts[0]["text"]
    # And what the engine itself stored, not only what was streamed.
    stored = [e for e in _engine_tool_labels(chain)
              if e.get("event_type", e.get("type")) == "agent.tool"]
    assert stored, _engine_tool_labels(chain)
    payload = stored[0].get("payload") or {}
    assert payload.get("label") == SAMPLE_TOOL_LABELS_ES["get_labs"]


def test_an_english_sample_turn_on_the_real_engine_stays_english(
        cfg, svc, monkeypatch):
    from careagents.agent import SAMPLE_TOOL_LABELS
    _chain, frames = _spanish_sample_turn(
        cfg, svc, monkeypatch, "What are my most recent labs?")
    accepted = [f for f in frames if f["type"] == "accepted"]
    assert accepted and "lang" not in accepted[0]
    assert [f["label"] for f in frames if f["type"] == "tool"] == [
        SAMPLE_TOOL_LABELS["get_labs"]]
    assert SAMPLE_TOOL_LABELS["get_labs"] != TOOL_LABELS["get_labs"]


# --- the detector's English bias ---------------------------------------------

@pytest.mark.parametrize("text", [
    # Mutation killers: `es > en` decides this one (es=2, en=2), and the
    # opening mark decides the one-word reply.
    "Is the clinic on El Camino Real near Los Altos?",
])
def test_two_spanish_place_words_against_two_english_words_is_english(text):
    """MUTATION: drop `and es > en` -> True."""
    assert not routing.looks_spanish(text)


@pytest.mark.parametrize("text", ["¿Sí?", "¡Gracias!"])
def test_an_opening_spanish_mark_alone_reads_as_spanish(text):
    """MUTATION: drop the ¿/¡ branch -> False (one word is not enough
    without it)."""
    assert routing.looks_spanish(text)


#: Everyday English from US patients, with Spanish surnames, clinics and
#: places. Each reads as Spanish today: the particles (de, la, del, los,
#: las, el, y) count as Spanish words and the English list is 26 words, so
#: "wants", "an", "at", "from", "with" offset nothing.
ENGLISH_WITH_SPANISH_NAMES = [
    "Dr. de la Cruz wants an A1C recheck",
    "Appointment with Dr. De Los Santos",
    "Labs at Del Mar clinic, La Jolla",
    "Drive from El Paso to Las Cruces tomorrow",
    "Saw Dr. Garcia y Lopez at Los Alamos",
    "Pharmacy at Plaza de la Raza",
    "Visit at Clinica de la Comunidad",
    "La Clinica del Pueblo called about results",
    "Moved from Puerto Rico, records from Hospital de la Concepcion",
    "Prescribed by Dr. Mi-Young Kim, follow up with Dr. Del Toro",
]


@pytest.mark.xfail(strict=True, reason=(
    "QA #1037 F1: an English sentence naming a Spanish surname or place "
    "reads as Spanish, so an English reader gets Spanish status lines and "
    "'Esto no estuvo bien'. Remove this marker with the fix."))
@pytest.mark.parametrize("text", ENGLISH_WITH_SPANISH_NAMES)
def test_english_with_spanish_names_reads_as_english(text):
    assert not routing.looks_spanish(text)


# --- the cheerful tone in the composed prompt ---------------------------------

@pytest.mark.xfail(strict=True, reason=(
    "QA #1037 F3: the healthy-habits routine still says 'Celebrate what the "
    "person keeps doing' (careagents/advisors.py), so the composed prompt "
    "tells the model both to celebrate and never to praise. Remove this "
    "marker with the fix."))
@pytest.mark.parametrize("tone", ["sunny", "calm"])
def test_no_composed_prompt_asks_the_model_to_celebrate(tone):
    from careagents import advisors
    for key in advisors.ADVISORS:
        prompt = personas.system_prompt("Juniper", tone, advisor_key=key)
        assert "celebrate" not in prompt.lower(), (tone, key)
