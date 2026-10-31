"""Chat polish observed live 2026-10-26: the cheerful tone, Spanish status
lines, and "This wasn't right" in Spanish.

1. With the cheerful tone the answers gushed: "Preventive Care Champ!",
   "huge win!", "fantastic!". The same model in a calm register read as far
   more credible. Calm is the default for a new assistant (it already was;
   pinned here), and cheerful stays warm but never praises the person or
   calls anything a win. A saved choice is never changed.
2. A Spanish question got a Spanish answer (#1017) under English status
   lines. The worker reads the turn's language once, from the person's
   latest message that says clearly (routing.conversation_language), as
   #1017's rule reads it. It picks the status lines with it, and says it to
   the page as an `agent.lang` event, so "This wasn't right" and its sheet
   follow. Names and places in English ("Dr. de la Cruz") are not Spanish
   (QA on #1037).

Prompt content only: no live model is called. Synthetic data only.
"""
# ruff: noqa: F811  (the cfg and svc fixtures, imported then requested)

from __future__ import annotations

import re

import pytest

from careagents import beta, personas, routing
from careagents.agent import (SAMPLE_TOOL_LABELS, SAMPLE_TOOL_LABELS_ES,
                              TOOL_LABELS, TOOL_LABELS_ES)
from careagents.models import Agent
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, _login, cfg, svc)
from tests.test_careagents_sample_framing import _real_chat_app

pytestmark = pytest.mark.extension

SPANISH = "¿Cuáles son mis resultados de laboratorio más recientes?"
ENGLISH = "What are my most recent lab results?"


# --- 1. tone -----------------------------------------------------------------

def test_calm_is_the_default_tone():
    assert personas.DEFAULT_PERSONA == "calm"
    assert Agent.__table__.c.persona.default.arg == "calm"


def test_a_new_assistant_is_calm_unless_another_tone_is_picked(
        cfg, svc, monkeypatch):
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    with svc.session() as s:
        assert s.get(Agent, conn["agent_id"]).persona == "calm"
    # Made without a tone, through the route a page calls.
    assert svc.delete_agent(_account_of(svc, conn["agent_id"]),
                            conn["agent_id"])
    made = c.post("/api/agents", json={"name": "Juniper",
                                       "connection_id": conn["id"]})
    with svc.session() as s:
        assert s.get(Agent, made.get_json()["id"]).persona == "calm"


def _account_of(svc, agent_id):
    with svc.session() as s:
        return s.get(Agent, agent_id).account_id


def test_a_saved_cheerful_choice_is_kept(cfg, svc, monkeypatch):
    """Nothing migrates a saved tone: an assistant set to cheerful stays
    cheerful, and gets the toned-down cheerful voice."""
    _app, c, _fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    r = c.post(f"/api/agents/{agent_id}/settings",
               json={"persona": "sunny"})
    assert r.status_code == 200
    with svc.session() as s:
        assert s.get(Agent, agent_id).persona == "sunny"
    prompt = personas.system_prompt("Juniper", "sunny")
    assert personas.PERSONAS["sunny"]["voice"] in prompt


SUNNY = personas.PERSONAS["sunny"]


def test_cheerful_forbids_praise_wins_and_superlatives():
    voice = SUNNY["voice"].lower()
    assert "never praise the person" in voice
    assert "never call anything a win" in voice
    assert "no superlatives" in voice
    # The words seen live, named so the model knows what is meant.
    for word in ("champ", "huge", "fantastic"):
        assert word in voice


@pytest.mark.parametrize("asks", [
    "celebrate", "easy wins", "celebrates wins", "encouraging"])
def test_cheerful_no_longer_asks_for_cheering(asks):
    """MUTATION: put back "Celebrate what the person has done" or "Frame
    gaps as easy wins" -> red."""
    assert asks not in SUNNY["voice"].lower()
    assert asks not in SUNNY["tagline"].lower()


def test_cheerful_keeps_its_warmth_and_the_results_rule():
    voice = SUNNY["voice"].lower()
    assert "warm" in voice
    assert "never call a lab" in voice


def test_no_voice_or_composed_prompt_asks_to_celebrate():
    """No voice may cheer, and neither may a routine composed with it: the
    healthy-habits routine said "Celebrate what the person keeps doing" in
    every tone (QA on #1037 F3)."""
    from careagents import advisors
    for key, p in personas.PERSONAS.items():
        assert "celebrate" not in p["voice"].lower(), key
        for routine in [None, *advisors.ADVISORS]:
            prompt = personas.system_prompt("Juniper", key,
                                            advisor_key=routine)
            assert "celebrate" not in prompt.lower(), (key, routine)


# --- 2. the turn's language --------------------------------------------------

@pytest.mark.parametrize("text", [
    SPANISH,
    "Cuales son mis resultados de laboratorio",
    "Revisa mis análisis por favor",
    "¿Tengo vacunas pendientes?",
    "Hola, necesito saber qué medicamentos tomo",
    "me duele la cabeza y tengo fiebre",
])
def test_spanish_reads_as_spanish(text):
    assert routing.looks_spanish(text)


@pytest.mark.parametrize("text", [
    ENGLISH, "ok", "thanks", "hola", "Is my A1c normal?",
    "what are mis labs",
    "I had a visit in La Jolla last year",
    "Show me the de-identified summary",
    "Can you check my blood pressure readings?",
    "",
])
def test_english_and_unclear_text_read_as_english(text):
    """A false positive puts Spanish status lines on an English reader, so
    one Spanish-looking word is not enough."""
    assert not routing.looks_spanish(text)


@pytest.mark.parametrize("text", [
    "Dr. de la Cruz wants an A1C recheck",
    "Labs at Del Mar clinic, La Jolla",
    "Drive from El Paso to Las Cruces",
    "Saw Dr. Garcia y Lopez at Los Alamos",
    "La Clinica del Pueblo called about results",
    "\u00a1Thanks!",
    "\u00bfThanks?",
    # In lower case, as typed on a phone: the particles never count.
    "appt with dr de la rosa",
])
def test_english_naming_spanish_places_is_clearly_english(text):
    """QA on #1037 F1: names and places are not counted, and a lone opening
    mark around an English word decides nothing."""
    assert routing.turn_language(text) == "en"


@pytest.mark.parametrize("text", [
    "s\u00ed", "gracias", "no s\u00e9", "ok", "hola", "",
])
def test_a_short_reply_says_too_little_to_tell(text):
    assert routing.turn_language(text) is None


@pytest.mark.parametrize("text", [
    "Voc\u00ea pode ver meus exames?", "N\u00e3o entendi", "obrigado",
    "Quais s\u00e3o os meus resultados de laborat\u00f3rio?",
    "Preciso de informa\u00e7\u00e3o sobre a minha press\u00e3o",
])
def test_portuguese_falls_back_to_english(text):
    """QA on #1037 F5: Portuguese is not Spanish."""
    assert routing.turn_language(text) == "en"


@pytest.mark.parametrize("said, lang", [
    ([SPANISH, "s\u00ed"], "es"),
    ([SPANISH, "gracias"], "es"),
    ([SPANISH, "no s\u00e9"], "es"),
    ([SPANISH, "ok"], "es"),
    ([ENGLISH, "gracias"], "en"),
    ([SPANISH, ENGLISH], "en"),
    ([ENGLISH, SPANISH], "es"),
    (["s\u00ed"], "en"),
    ([], "en"),
])
def test_a_short_reply_keeps_the_conversations_language(said, lang):
    """QA on #1037 F4: "s\u00ed" after a Spanish turn stays Spanish."""
    assert routing.conversation_language(said) == lang


def test_spanish_labels_cover_every_tool():
    assert set(TOOL_LABELS_ES) == set(TOOL_LABELS)
    assert set(SAMPLE_TOOL_LABELS_ES) == set(SAMPLE_TOOL_LABELS)


@pytest.mark.parametrize("tool, says", [
    ("search_records", "Buscando en sus registros"),
    ("get_labs", "Interpretando sus análisis"),
    ("get_care_gaps", "Revisando los exámenes preventivos"),
])
def test_the_status_lines_seen_live_are_in_spanish(tool, says):
    assert TOOL_LABELS_ES[tool] == says


def test_spanish_sample_labels_never_say_the_records_are_theirs():
    """As in English (#908 re-walk): "sus análisis" on made-up records read
    as the tester's own."""
    second = re.compile(r"\b(su|sus|usted|tu|tus)\b", re.IGNORECASE)
    assert not any(second.search(v) for v in SAMPLE_TOOL_LABELS_ES.values())
    for v in SAMPLE_TOOL_LABELS_ES.values():
        assert "inventad" not in v and "de muestra" not in v, v


def _labels_for(c, fake, agent_id, cfg, svc, monkeypatch, message):
    """The status lines a turn that reads the labs shows. `fake.events`
    keeps every run's, so a second turn returns both turns' lines."""
    from careagents import agent as agent_mod
    from careagents.worker import RunWorker

    class _Call:
        id, name, arguments = "c1", "get_labs", {}

    class _Ask:
        text, tool_calls, raw_tool_calls = "", [_Call()], []

    class _Done:
        text, tool_calls, raw_tool_calls = "listo", [], []
    turns = iter([_Ask(), _Done()])
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: next(turns))
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": message,
                                  "request_id": f"lang-{len(message)}"},
               buffered=False)
    r.close()
    RunWorker(cfg, fake, svc, "lang-worker").run_once()
    return [e["payload"]["label"] for events in fake.events.values()
            for e in events if e["type"] == "agent.tool"]


def test_a_spanish_turn_gets_spanish_status_lines(cfg, svc, monkeypatch):
    c, fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    assert _labels_for(c, fake, agent_id, cfg, svc, monkeypatch,
                       SPANISH) == [TOOL_LABELS_ES["get_labs"]]


def test_an_english_turn_keeps_english_status_lines(cfg, svc, monkeypatch):
    c, fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    assert _labels_for(c, fake, agent_id, cfg, svc, monkeypatch,
                       ENGLISH) == [TOOL_LABELS["get_labs"]]


def test_a_spanish_sample_turn_gets_the_made_up_spanish_lines(
        cfg, svc, monkeypatch):
    _app, c, fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    assert _labels_for(c, fake, agent_id, cfg, svc, monkeypatch,
                       SPANISH) == [SAMPLE_TOOL_LABELS_ES["get_labs"]]


def test_a_short_spanish_reply_keeps_spanish_status_lines(
        cfg, svc, monkeypatch):
    """QA on #1037 F4: "s\u00ed" after a Spanish turn, from the history the
    worker already reads. Nothing new is stored in CareAgents."""
    c, fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    _labels_for(c, fake, agent_id, cfg, svc, monkeypatch, SPANISH)
    assert _labels_for(c, fake, agent_id, cfg, svc, monkeypatch,
                       "s\u00ed") == [TOOL_LABELS_ES["get_labs"]] * 2


def _lang_events(fake):
    return [e["payload"] for events in fake.events.values()
            for e in events if e["type"] == "agent.lang"]


def test_the_worker_tells_the_page_a_spanish_turn_is_spanish(
        cfg, svc, monkeypatch):
    c, fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    _labels_for(c, fake, agent_id, cfg, svc, monkeypatch, SPANISH)
    assert _lang_events(fake) == [{"lang": "es"}]


def test_an_english_turn_says_no_language(cfg, svc, monkeypatch):
    c, fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    _labels_for(c, fake, agent_id, cfg, svc, monkeypatch, ENGLISH)
    assert _lang_events(fake) == []


def test_the_page_gets_only_a_known_language_code(cfg, svc, monkeypatch):
    """The browser projection passes "es" and drops anything else."""
    c, fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    _labels_for(c, fake, agent_id, cfg, svc, monkeypatch, SPANISH)
    [run_id] = list(fake.events)
    fake._append_run_event(run_id, "agent.lang", {"lang": "<b>x</b>"})
    r = c.get(f"/api/chat/runs/{run_id}/events",
              query_string={"agent_id": agent_id, "after": 0})
    body = r.get_data(as_text=True)
    assert '"type": "lang", "lang": "es"' in body
    assert "<b>x</b>" not in body


def test_the_911_lines_are_untouched():
    """#1017's fixed bilingual lines, word for word."""
    assert beta.EMERGENCY_LINE == ("If this is an emergency, call 911 now. "
                                   "Si es una emergencia, llame al 911 "
                                   "ahora.")
    assert beta.EMERGENCY_TRAILER == ("If this is an emergency, call 911. "
                                      "Si es una emergencia, llame al 911.")
