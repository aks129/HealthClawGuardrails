"""Security review of PR #1017: Spanish crisis wording the PR does not catch.

The PR says Spanish crisis wording routes as English crisis wording does.
These are the common Spanish and Spanglish ways of saying an English phrase
_CRISIS already covers ("don't want to live", "bleeding", "unconscious",
"chest pain", "heart attack", "end my life", "hurt myself", "abuse") that
is_crisis still misses on the PR head.

A miss does not put the crisis message itself on Haiku: classify sends any
text off the app-question allowlist to Sonnet. The miss is in the sticky
window (crisis_in_window). After "no quiero vivir", a plain "ok" goes to
Haiku, which sees only "ok", unless the assistant's reply happened to say
911, 988, "emergencia" or "ambulancia". A calm check-in in Spanish, or the
refusal text, does not, so the person's goodbye after a crisis gets a
cheerful answer from a model with no history (#931 F2 / QA Q1).

Synthetic phrases only.
"""

from __future__ import annotations

import time

import pytest

from careagents import routing
from careagents.llm import REFUSAL_TEXT

# Each one is a direct way of saying an English phrase _CRISIS covers.
SPANISH_MISSES = (
    # don't want to live / gonna die / end my life
    "no quiero vivir", "ya no quiero vivir", "me voy a morir",
    "me estoy muriendo", "siento que me muero", "quitarme la vida",
    "acabar con mi vida", "terminar con todo",
    # hurt / cut myself
    "hacerme daño", "lastimarme", "cortarme", "me corto",
    # can't breathe
    "me falta el aire", "falta de aire", "me cuesta respirar", "me ahogo",
    # passed out / unconscious
    "perdí el conocimiento", "perdio el conocimiento", "inconsciente",
    "mi mamá no responde cuando le hablo", "no despierta",
    # bleeding
    "estoy sangrando mucho", "sangro mucho", "mucha sangre",
    # stroke signs
    "no siento el brazo", "no puedo mover el brazo",
    "se me durmió la mitad del cuerpo", "la cara caída",
    "se le cayó la cara", "la boca torcida", "no puede hablar bien",
    "habla arrastrada", "embolia", "ictus",
    # chest pain / heart attack
    "me duele el pecho", "me duele mucho el pecho",
    "dolor fuerte en el pecho", "presión en el pecho",
    "opresión en el pecho", "ataque cardiaco", "ataque cardíaco",
    # seizure
    "ataque epiléptico",
    # overdose
    "me tomé todas las pastillas", "me tome un frasco de pastillas",
    # emergency / abuse / unsafe
    "es urgente", "urgencia", "abuso", "mi esposo me pega",
    "violencia doméstica", "no estoy segura en casa",
    # texting spelling
    "kiero morir", "me kiero matar",
    # Spanglish
    "no puedo breathe", "i can't respirar", "i want to morir",
    "mi pecho hurts", "my chest duele",
)


@pytest.mark.parametrize("text", SPANISH_MISSES)
def test_spanish_crisis_wording_is_crisis(text):
    assert routing.is_crisis(text), text


# A reply with no 911, 988, "emergencia", "crisis" or "ambulancia": the
# refusal text, and a calm check-in that asks first and gives numbers later.
REPLIES_WITHOUT_A_MARKER = (
    REFUSAL_TEXT,
    "Lo siento mucho. ¿Estás a salvo ahora mismo? Estoy aquí contigo.",
)


@pytest.mark.parametrize("reply", REPLIES_WITHOUT_A_MARKER)
@pytest.mark.parametrize("first", (
    "no quiero vivir", "me falta el aire", "perdí el conocimiento",
    "estoy sangrando mucho", "me duele el pecho"))
@pytest.mark.parametrize("follow", ("ok", "thanks", "ok bye"))
def test_a_spanish_crisis_keeps_the_conversation_on_sonnet(first, reply,
                                                           follow):
    history = [{"role": "user", "content": first},
               {"role": "assistant", "content": reply},
               {"role": "user", "content": follow}]
    assert routing.tier_for(history) == routing.TIER_SONNET


def test_the_english_twin_already_stays_on_sonnet():
    # The control: the same chain in English never reaches Haiku.
    for reply in REPLIES_WITHOUT_A_MARKER:
        history = [{"role": "user", "content": "I don't want to live anymore"},
                   {"role": "assistant", "content": reply},
                   {"role": "user", "content": "ok"}]
        assert routing.tier_for(history) == routing.TIER_SONNET


# --- no regression from the accent stripping ---------------------------------

UNICODE_TRICKS = (
    "suícide", "sui͏cide", "sui️cide", "sui⃝cide",
    "ｓｕｉｃｉｄｅ", "sui​cide",
    "sui­cide", "оverdose", "kill mysеlf",
    "\U0001d42cuicide", "suıcide", "SUİCIDE", "ſuicide",
    "me desmãyo", "no puedo res͏pirar", "chest pain",
    "chest　pain", "can’t breathe", "canʼt breathe",
    "no puedo respirar",
)


@pytest.mark.parametrize("text", UNICODE_TRICKS)
def test_accent_stripping_only_adds_matches(text):
    before = bool(routing._CRISIS.search(routing.normalize(text)))
    assert routing.is_crisis(text) >= before


@pytest.mark.parametrize("text", UNICODE_TRICKS)
def test_unicode_tricks_never_reach_haiku(text):
    # Those is_crisis misses (a combining grapheme joiner, a Cyrillic
    # letter, a dotless i) are refused by classify's ASCII rule.
    assert routing.classify(text) == routing.TIER_SONNET


def test_is_crisis_is_linear_on_long_input():
    for text in ("dolor " * 35_000, "no puedo " * 22_000,
                 "ataque al " * 20_000, "é" * 100_000):
        start = time.perf_counter()
        routing.is_crisis(text)
        assert time.perf_counter() - start < 1.0
