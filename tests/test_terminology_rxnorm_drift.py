"""Every RxNorm row in the static label table agrees with RxNav (#1119).

Nine rows once named the wrong medicine or the wrong strength, and nothing
caught it: the table was the only source, so a test that read the table
agreed with it. This checks each row against a recorded copy of what RxNav
says the code is (tests/fixtures/rxnav_concepts.json, refreshed with
scripts/record_rxnav_fixture.py; no network here).

The rule, both directions:

- the code is Active;
- label to concept: each medicine the label names is an ingredient of the
  concept, and each number in the label is a number in the RxNorm name;
- concept to label: each active ingredient is named; a single-ingredient
  product keeps a strength; an extended or delayed release form is said;
- an ingredient-level code carries no strength.

Stated exception: a multi-ingredient product or pack is named by its
ingredients and form without strengths, as the #1058 rows are.

MUTATION: label 42463 "Amlodipine" (it is pravastatin), 617314 "Atorvastatin
20 mg" (it is 10 mg), or 860975 "Metformin 500 mg" (it is extended release)
and test_each_label_agrees_with_the_recorded_concept fails for that row.
"""

import json
import re
from pathlib import Path

import pytest

from r6.terminology import RXNORM, _LABELS

FIXTURE = Path(__file__).parent / "fixtures" / "rxnav_concepts.json"
CONCEPTS = json.loads(FIXTURE.read_text())
ROWS = sorted(((code, label) for (system, code), label in _LABELS.items()
               if system == RXNORM), key=lambda row: int(row[0]))

INGREDIENT_LEVEL = {"IN", "MIN", "PIN"}
NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _active_ingredients(concept: dict) -> list[str]:
    names = []
    for part in concept["ingredients"]:
        name = part["ingredient"]
        if name and name != "inert ingredients" and name not in names:
            names.append(name)
    if not names:  # an ingredient-level concept is its own ingredient
        names = [n.strip().lower() for n in concept["name"].split(" / ")]
    return names


def _named_medicines(label: str) -> list[str]:
    """The medicine names a label leads with: text before the first digit or
    bracket, split on "and"."""
    head = re.split(r"[\d(]", label.lower(), maxsplit=1)[0]
    return [part.strip() for part in head.split(" and ") if part.strip()]


def _matches(named: str, ingredient: str) -> bool:
    # "penicillin" names "penicillin g"; "ethinyl estradiol vaginal ring"
    # names "ethinyl estradiol".
    return named.startswith(ingredient) or ingredient.startswith(named)


def problems(label: str, concept: dict) -> list[str]:
    found = []
    if concept["status"] != "Active":
        found.append("code is %s, not Active" % concept["status"])
    ingredients = _active_ingredients(concept)
    named = _named_medicines(label)
    for name in named:
        if not any(_matches(name, ing) for ing in ingredients):
            found.append("label names %r, not an ingredient of %s"
                         % (name, ingredients))
    for ing in ingredients:
        if not any(_matches(name, ing) for name in named):
            found.append("ingredient %r is not named" % ing)

    plain = re.sub(r"\([^)]*\)", "", label)
    label_numbers = NUMBER.findall(plain)
    concept_numbers = set(NUMBER.findall(concept["name"]))
    for number in label_numbers:
        if number not in concept_numbers:
            found.append("label says %s; the concept is %r"
                         % (number, concept["name"]))

    if concept["tty"] in INGREDIENT_LEVEL:
        if label_numbers:
            found.append("an ingredient-level code carries no strength")
    elif len(ingredients) == 1 and not label_numbers:
        found.append("a single-ingredient product keeps its strength")

    forms = " ".join(concept["dose_forms"] + [concept["name"]]).lower()
    for release in ("extended release", "delayed release"):
        if release in forms and release not in label.lower():
            found.append("label drops %r" % release)
    return found


def test_the_fixture_covers_exactly_the_table():
    assert ROWS, "no RxNorm rows examined"
    assert {code for code, _ in ROWS} == set(CONCEPTS)


@pytest.mark.parametrize("code,label", ROWS, ids=[c for c, _ in ROWS])
def test_each_label_agrees_with_the_recorded_concept(code, label):
    assert not problems(label, CONCEPTS[code]), (code, label)


@pytest.mark.parametrize("code,wrong", [
    ("42463", "Amlodipine"),            # a different medicine
    ("617314", "Atorvastatin 20 mg"),   # the wrong strength
    ("860975", "Metformin 500 mg"),     # drops extended release
    ("860974", "Metformin"),            # a product without its strength
    ("6809", "Metformin 500 mg"),       # an ingredient with a strength
])
def test_the_rule_catches_each_kind_of_wrong_label(code, wrong):
    assert problems(wrong, CONCEPTS[code]), (code, wrong)
