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
