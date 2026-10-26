"""QA for #1036's fix round: the Greek mu (U+03BC) in `unit_is_shaped`.

The fix admits U+03BC beside the micro sign (U+00B5) in three regexes in
r6/redaction.py, so "10*3/μL" is no longer blanked. The security property
to hold is that the Greek mu admits nothing the micro sign did not already
admit: it must count as a letter everywhere the micro sign does, or it
becomes a separator that splits a long word or a pair of names into runs
the rules let through. Only the one character is admitted; the rest of the
Greek block stays out.

Measured by mutation: with U+03BC dropped from `_LONG_WORD` or
`_LETTER_RUN`, or the whole Greek block admitted to `_UNIT_SHAPE`, the
existing unit tests all still passed. Each row below fails under one of
them. Synthetic strings only.
"""

import random

import pytest

from r6.redaction import unit_is_shaped

MU, MICRO = "μ", "µ"
_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
               "0123456789%/.*^[]{}()#_'-°" + MU + MICRO)


@pytest.mark.parametrize("unit", ["10*3/μL", "μmol/L",
                                  "μIU/mL", "μg/dL"])
def test_real_units_with_the_greek_mu_pass(unit):
    assert unit_is_shaped(unit)


@pytest.mark.parametrize("text", [
    "abcdefghμ/L",          # nine letters only if mu is one (_LONG_WORD)
    "Abcμ/Defg",            # two 4-letter runs only if mu is a letter
    "Zeldμ/Quor",           # (_LETTER_RUN)
    "Μαρία/mL",   # a Greek name: not the block
    "αβγ/dL",
    "Jμne Doe",
    "μ5551234",
    "IGNOREμALLμPREVIOUS",
])
def test_free_text_with_the_greek_mu_is_refused(text):
    assert not unit_is_shaped(text)


def test_the_greek_mu_admits_nothing_the_micro_sign_does_not():
    rng = random.Random(1036)
    alphabet = (list("abcdeLmgdlSmithJonesXYZ0123456789%/.*^[]{}()#_'-°")
                + [MU, MICRO] * 6
                + ["α", "Μ", "ά", "а", " ", "​"])
    for _ in range(50_000):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
        if unit_is_shaped(s):
            assert set(s) <= _ALLOWED, s
            assert unit_is_shaped(s.replace(MU, MICRO)), s
