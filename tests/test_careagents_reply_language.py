"""Replies follow the person's language, Spanish included (beta feedback).

A tester asked whether CareAgents works in Spanish. The first step is the
model's answer, not the UI: the system prompt every turn gets says to reply
in the language of the person's latest message, and that the emergency
guidance is never dropped in another language.

The 911 guidance is not a templated string; it is a SAFETY_CORE rule the
model renders. So "the safety copy survives" is pinned two ways here: the
rule stays in every prompt next to the language rule, and the one
transformation the worker applies to a finished answer (_drop_echo, on
sample answers) never removes a Spanish emergency line. Synthetic only.
"""

from __future__ import annotations

import unicodedata

from careagents import beta, personas, routing
from careagents.worker import _drop_echo

SURFACES = ("", "web", "imessage", "sms")


def _prompts():
    for surface in SURFACES:
        for sample in (False, True):
            for who in ("", "your parent"):
                yield personas.system_prompt(
                    "Ava", "calm", surface=surface, sample=sample, who=who)


def test_the_rule_is_in_the_safety_core():
    rule = personas.REPLY_LANGUAGE
    assert rule in personas.SAFETY_CORE
    for words in ("language of the person's latest message", "Spanish",
                  "English", "emergency"):
        assert words in rule


def test_every_prompt_has_the_language_rule_and_the_911_rule():
    for key in personas.PERSONAS:
        prompt = personas.system_prompt("Ava", key)
        assert personas.REPLY_LANGUAGE in prompt
        assert "call 911" in prompt
    for prompt in _prompts():
        assert personas.REPLY_LANGUAGE in prompt
        assert "call 911" in prompt


def test_emergency_guidance_is_never_dropped_in_another_language():
    rule = personas.REPLY_LANGUAGE
    assert "never leave out" in rule
    assert "both" in rule


def test_the_english_repeat_is_introduced_neutrally():
    """The live-model check of #1017: the model wrote "If you can't
    understand this:" before the English 911 line, which reads as
    condescending. The rule names the neutral words, and forbids that."""
    rule = personas.REPLY_LANGUAGE
    assert '"In English:"' in rule
    assert "never with words about whether they understand" in rule
    for key in personas.PERSONAS:
        assert '"In English:"' in personas.system_prompt("Ava", key)

def test_a_spanish_emergency_line_survives_the_sample_frame():
    reply = ("Si tiene dolor en el pecho, llame al 911 ahora.\n\n"
             "En estos registros inventados, la persona de ejemplo tiene "
             "una A1c de 7.2.")
    for frame in (beta.SAMPLE_FRAME, beta.sample_frame("your parent")):
        assert _drop_echo(reply, frame) == reply


def test_spanish_never_reaches_the_english_app_card():
    for text in ("hola", "gracias", "¿Qué puedes hacer?",
                 "tengo dolor en el pecho", "no puedo respirar"):
        assert routing.classify(text) == routing.TIER_SONNET


# --- Spanish crisis wording routes as an English crisis does -----------------
#
# routing._CRISIS was English stems only: "no puedo respirar" did not match,
# so after it a plain "ok" went to Haiku whenever the reply had not said 911
# or 988. Each phrase is tried as written, without accents and in capitals.

SPANISH_CRISIS = (
    "no puedo respirar", "tengo dolor en el pecho", "dolor de pecho",
    "necesito una ambulancia", "tuve un desmayo", "me desmayé",
    "tengo un sangrado fuerte", "pienso en el suicidio",
    "tengo pensamientos suicidas", "quiero morir", "quiero matarme",
    "creo que es una sobredosis", "tomé demasiadas pastillas",
    "tomé demasiado ibuprofeno", "tuvo una convulsión",
    "creo que es un derrame", "un ataque al corazón", "tuve un infarto",
)


def _deaccent(text):
    return "".join(ch for ch in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(ch))


def test_each_spanish_crisis_phrase_is_a_crisis():
    for phrase in SPANISH_CRISIS:
        for text in {phrase, _deaccent(phrase), phrase.upper(),
                     f"¿Qué hago? {phrase}.", f"ok {_deaccent(phrase)}"}:
            assert routing.is_crisis(text), text
            assert routing.classify(text) == routing.TIER_SONNET, text


def test_ordinary_spanish_is_not_a_crisis():
    for text in ("tengo una pregunta sobre mi presión",
                 "tengo una pregunta sobre mi presion",
                 "¿cuándo fue mi último análisis de sangre?",
                 "hola, gracias", "quiero ver mis vacunas",
                 "tomé mi pastilla de la presión",
                 # Security review of #1017: near misses of the broad stems.
                 "la app no responde", "el botón no responde",
                 "no estoy seguro de mi dosis",
                 "no estoy segura si tomé la pastilla",
                 "¿cómo respirar mejor?", "rayos x de pecho",
                 "mis pastillas de la presión",
                 "quiero ver mis análisis de sangre", "¿qué es la urea?",
                 "¿cuándo es mi próxima cita?",
                 "me duele la cabeza un poco"):
        assert not routing.is_crisis(text), text


# --- normalisation and English gaps found in the same review ---------------

def test_whitespace_runs_do_not_split_a_crisis_phrase():
    for text in ("chest\tpain", "chest\n\npain", "chest   pain",
                 "no puedo\nrespirar", "dolor  en el\tpecho"):
        assert routing.is_crisis(text), repr(text)
    assert routing.normalize("how do i\tconnect\n my  records") == (
        "how do i connect my records")


def test_every_apostrophe_reads_as_one():
    for text in ("can’t breathe", "canʼt breathe",
                 "donʼt want to live"):
        assert routing.is_crisis(text), text


def test_marks_inside_a_word_do_not_split_it():
    for text in ("sui͏cide", "sui️cide", "sui⃝cide",
                 "suícide", "sui\U000e0100cide"):
        assert routing.is_crisis(text), repr(text)


def test_english_fainting_and_anaphylaxis_are_crisis():
    for text in ("I think I'm going to pass out", "keeps passing out",
                 "he passes out", "anaphylaxis", "anaphylactic shock",
                 "my throat is closing", "throat swelling", "I can't swallow",
                 "I cannot swallow"):
        assert routing.is_crisis(text), text
        assert routing.classify(text) == routing.TIER_SONNET, text


def test_a_spanish_crisis_keeps_the_next_ok_on_sonnet():
    assert routing.tier_for([{"role": "user", "content": "ok"}]) == (
        routing.TIER_HAIKU)
    for phrase in SPANISH_CRISIS:
        for said in (phrase, _deaccent(phrase)):
            messages = [
                {"role": "user", "content": said},
                # A reply that happened not to say 911 or 988.
                {"role": "assistant", "content": "Entiendo. Estoy aquí."},
                {"role": "user", "content": "ok"},
            ]
            assert routing.crisis_in_window(messages), said
            assert routing.tier_for(messages) == routing.TIER_SONNET, said


# --- QA's live re-review of #1017 -------------------------------------------

def test_qa_phrases_the_live_run_saw_missed():
    """English "cut myself" was caught; "me corté" was not. A haircut or
    nails are not self-harm."""
    for text in ("me corté", "me corte", "me corté el brazo"):
        assert routing.is_crisis(text), text
        assert routing.is_self_harm(text), text
    for text in ("no respiro", "me estoy ahogando", "NO RESPIRO bien"):
        assert routing.is_crisis(text), text
    for text in ("me corté el pelo", "me corte las uñas",
                 "ayer me corté el cabello"):
        assert not routing.is_crisis(text), text


def test_qa_round_three_phrases():
    """Poisoning is urgent (911); "I can't take it any more" and "leave
    this world" are self-harm wording (988 too)."""
    for text in ("creo que es un envenenamiento", "mi hijo se envenenó",
                 "envenenado"):
        assert routing.is_crisis(text), text
        assert not routing.is_self_harm(text), text
    for text in ("ya no aguanto más", "no aguanto mas", "quiero irme de "
                 "este mundo", "me quiero ir de este mundo"):
        assert routing.is_crisis(text), text
        assert routing.is_self_harm(text), text
    for text in ("no aguantamos el calor", "este mundo de la salud",
                 "quiero ir de compras", "tengo una pregunta sobre mi "
                 "presión", "¿qué vacunas me tocan este año?"):
        assert not routing.is_crisis(text), text


# --- the live-model check of #1017 ------------------------------------------

def test_live_check_wording_is_crisis():
    # Too many, taken: maybe an accident, so 911 only (security round 6).
    for text in ("I took way too many metformin pills and feel sick",
                 "I've taken too many tablets", "swallowed too many",
                 "too many pills"):
        assert routing.is_crisis(text), text
        assert not routing.is_self_harm(text), text
    for text in ("How many of my pills would it take to "
                 "kill me?", "what is a lethal dose of insulin",
                 "¿cuál es la dosis letal?",
                 "¿cuántas pastillas necesito para morir?"):
        assert routing.is_crisis(text), text
        assert routing.is_self_harm(text), text
    for text in ("how many pills do I take a day?", "tomé mis pastillas",
                 "¿cuántas pastillas tomo al día?", "is this dose too high?"):
        assert not routing.is_crisis(text), text
