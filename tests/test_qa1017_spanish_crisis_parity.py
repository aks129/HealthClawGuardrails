"""QA on #1017: Spanish crisis wording, row for row with the English list.

The brief: route Spanish crisis phrases like English ones. routing._CRISIS
has English stems for self-harm, not wanting to live, ending it, being
unconscious, abuse and bleeding. Each row below is the plain Spanish for an
English stem the list already has, so an English speaker saying the same
thing stays on Sonnet after a reply that did not say 911 or 988 (#931 Q1),
and a Spanish speaker should too.

Also here: the sample answer's disclaimer, which worker._drop_echo only
recognises in English (PR body, "Gaps noted"). Pinned as a strict xfail so
the gap is visible and the test turns red when it is fixed.

Synthetic text only.
"""

from __future__ import annotations

import pytest

from careagents import beta, routing
from careagents.worker import _drop_echo

#: (Spanish, the English the list already matches for the same meaning)
PARITY = (
    ("no quiero vivir", "i don't want to live"),
    ("ya no quiero vivir", "i don't want to live anymore"),
    ("quiero quitarme la vida", "i want to end my life"),
    ("quiero acabar con mi vida", "i want to end my life"),
    ("quiero acabar con todo", "i want to end it all"),
    ("me quiero hacer daño", "i want to hurt myself"),
    ("quiero lastimarme", "i want to hurt myself"),
    ("me estoy cortando", "i am cutting myself"),
    ("quiero cortarme", "i want to cut myself"),
    ("me duele el pecho", "chest pain"),
    ("me duele mucho el pecho", "chest pain"),
    ("tuve un ataque cardíaco", "i had a heart attack"),
    ("está inconsciente", "he is unconscious"),
    ("perdió el conocimiento", "he passed out"),
    ("estoy sangrando mucho", "i am bleeding a lot"),
    ("sufro abuso en casa", "i suffer abuse at home"),
    ("me falta el aire", "i can't breathe"),
)


@pytest.mark.parametrize("spanish,english", PARITY,
                         ids=[s for s, _ in PARITY])
def test_spanish_crisis_matches_where_the_english_does(spanish, english):
    assert routing.is_crisis(english), english  # the English row is covered
    assert routing.is_crisis(spanish), spanish


@pytest.mark.parametrize("spanish,english", PARITY,
                         ids=[s for s, _ in PARITY])
def test_ok_after_a_spanish_crisis_stays_on_sonnet(spanish, english):
    """The #931 Q1 shape: crisis, a reply without 911 or 988, then "ok"."""
    def turn(said):
        return [
            {"role": "user", "content": said},
            {"role": "assistant", "content": "Lo siento mucho. Estoy aquí."},
            {"role": "user", "content": "ok"},
        ]
    assert routing.tier_for(turn(english)) == routing.TIER_SONNET
    assert routing.tier_for(turn(spanish)) == routing.TIER_SONNET, spanish


@pytest.mark.parametrize("text", [
    "me quiero matar", "ME QUIERO MATAR", "dolor del pecho",
    "tengo dolor del pecho"])
def test_alternatives_the_pr_tests_do_not_reach(text):
    """Mutation survivors on the PR head: removing r"me quiero matar" or
    the "del" in r"dolor (?:en el|de|del) pecho" left its tests green."""
    assert routing.is_crisis(text), text


@pytest.mark.xfail(strict=True, reason=(
    "#1017 known gap: _disclaimer_only reads English words only, so a "
    "Spanish sample answer shows the English frame and its own Spanish "
    "disclaimer"))
def test_a_spanish_disclaimer_is_said_once_under_the_banner():
    model = ("Estos son registros inventados, no son suyos.\n\n"
             "En estos registros inventados, la persona de ejemplo tiene "
             "una A1c de 7.2.")
    finished = f"{beta.SAMPLE_FRAME}\n\n{_drop_echo(model, beta.SAMPLE_FRAME)}"
    shown = beta.under_banner(finished)
    assert "no son suyos" not in shown
