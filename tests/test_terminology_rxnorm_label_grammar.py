"""An RxNorm label says only what its concept supports (QA on #1121).

tests/test_terminology_rxnorm_drift.py reads a label's leading medicine names
and its bare digits. Everything else in the label is unread, so it accepts
"Metformin 500 mcg", "Metformin 500 mg injection", "Amlodipine 5 mg and
atorvastatin" and "Metformin 500 mg extended release" on the plain tablet.
Those labels reach a clinic's intake form, the visit brief (which shows the
label's strength as the dose) and the pharmacy-transfer call script.

This is the tightened rule, proposed for folding into `problems()`. A label
is read as a whole, from the end:

    <medicine>[ and <medicine>] [<number> <unit>] [<form>] [(<N>-day pack)]

- each medicine IS an ingredient of the concept (equal, not a prefix), and
  every ingredient is named;
- a strength is a number WITH its unit, and equals the concept's strength
  (for a concentration, volume times concentration);
- a form is one of a closed list, and the concept has that form; a concept
  whose form is not a plain tablet or capsule must say it;
- a pack length is the pack's own;
- nothing else may appear: an unread word fails.

`test_every_row_passes` shows all 41 rows of the table already meet it.

MUTATION: label 861007 "Metformin 500 mg extended release" (it is the plain
tablet) in r6/terminology.py and test_every_row_passes[861007] fails, while
tests/test_terminology_rxnorm_drift.py stays green.
"""

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from r6.terminology import RXNORM, _LABELS

FIXTURE = Path(__file__).parent / "fixtures" / "rxnav_concepts.json"
CONCEPTS = json.loads(FIXTURE.read_text())
ROWS = sorted(((code, label) for (system, code), label in _LABELS.items()
               if system == RXNORM), key=lambda row: int(row[0]))

INGREDIENT_LEVEL = {"IN", "MIN", "PIN"}
PACKS = {"GPCK", "BPCK"}

#: A label name that is deliberately not the RxNorm ingredient name. #959:
#: the check-in wallet shows the allergen penicillin G as "Penicillin".
ALIASES = {"penicillin": "penicillin g"}

#: Milligrams per label unit, and per RxNorm unit.
LABEL_UNITS = {"mg": Decimal(1), "mcg": Decimal("0.001"), "g": Decimal(1000)}
RXNORM_UNITS = {"MG": Decimal(1), "MCG": Decimal("0.001"), "G": Decimal(1000)}

#: Every form a label may say, and the RxNorm dose form it claims.
FORMS = {
    "extended release": "Extended Release",
    "delayed release": "Delayed Release",
    "chewable tablet": "Chewable Tablet",
    "implant": "Drug Implant",
    "auto-injector": "Auto-Injector",
    "vaginal ring": "Vaginal System",
}
#: Dose forms a label may leave unsaid. Any other form must be said.
UNSAID_FORMS = {"Oral Tablet", "Oral Capsule", "Pack"}

PACK = re.compile(r" \((\d+)-day pack\)$")
STRENGTH = re.compile(r" (\d+(?:\.\d+)?) ([A-Za-z]+)$")
CONCENTRATION = re.compile(
    r"(\d+(?:\.\d+)?) ML .*?(\d+(?:\.\d+)?) (MG|MCG)/ML")


def _ingredients(concept: dict) -> list[str]:
    names = []
    for part in concept["ingredients"]:
        name = part["ingredient"]
        if name and name != "inert ingredients" and name not in names:
            names.append(name)
    if not names:  # an ingredient-level concept is its own ingredient
        names = [n.strip().lower() for n in concept["name"].split(" / ")]
    return names


def _concept_mg(concept: dict) -> Decimal | None:
    """The strength of a single-ingredient product, in milligrams."""
    by_volume = CONCENTRATION.search(concept["name"])
    if by_volume:
        volume, per_ml, unit = by_volume.groups()
        return Decimal(volume) * Decimal(per_ml) * RXNORM_UNITS[unit]
    parts = [p for p in concept["ingredients"]
             if p["ingredient"] != "inert ingredients"]
    if len(parts) != 1 or parts[0]["unit"] not in RXNORM_UNITS:
        return None
    return Decimal(parts[0]["numerator"]) * RXNORM_UNITS[parts[0]["unit"]]


def strict_problems(label: str, concept: dict) -> list[str]:
    found = []
    if concept["status"] != "Active":
        found.append("code is %s, not Active" % concept["status"])
    ingredients = _ingredients(concept)
    forms = concept["dose_forms"]
    rest = label

    # (N-day pack), only on a pack, and only the pack's own length.
    pack = PACK.search(rest)
    if pack:
        rest = rest[:pack.start()]
        days = sum(int(n) for n in re.findall(r"(\d+) \(", concept["name"]))
        if concept["tty"] not in PACKS:
            found.append("label says a pack; the concept is not one")
        elif int(pack.group(1)) != days:
            found.append("label says a %s-day pack; it holds %d"
                         % (pack.group(1), days))
    elif concept["tty"] in PACKS:
        found.append("a pack must say its length")

    # Forms, from the closed list, each one true of the concept.
    said = []
    while True:
        phrase = next((p for p in FORMS if rest.endswith(" " + p)), None)
        if phrase is None:
            break
        said.append(phrase)
        rest = rest[:-len(phrase) - 1]
    for phrase in said:
        if not any(FORMS[phrase] in form for form in forms):
            found.append("label says %r; the concept's form is %s"
                         % (phrase, forms or "not given"))
    for form in forms:
        if form in UNSAID_FORMS:
            continue
        if not any(FORMS[phrase] in form for phrase in said):
            found.append("form %r is not said" % form)
        for phrase, claim in FORMS.items():
            if claim in form and phrase not in said:
                found.append("label drops %r" % phrase)

    # A strength: number and unit together, equal to the concept's.
    strength = STRENGTH.search(rest)
    if strength:
        rest = rest[:strength.start()]
        number, unit = strength.groups()
        expected = _concept_mg(concept)
        if concept["tty"] in INGREDIENT_LEVEL:
            found.append("an ingredient-level code carries no strength")
        elif len(ingredients) != 1:
            found.append("a strength on a multi-ingredient label is unchecked")
        elif unit not in LABEL_UNITS:
            found.append("unit %r is not one the rule can check" % unit)
        elif expected is None:
            found.append("the concept gives no strength to check against")
        elif Decimal(number) * LABEL_UNITS[unit] != expected:
            found.append("label says %s %s; the concept is %s mg (%r)"
                         % (number, unit, expected, concept["name"]))
    elif concept["tty"] not in INGREDIENT_LEVEL and len(ingredients) == 1:
        found.append("a single-ingredient product keeps its strength")

    # What is left is the medicines, and only the medicines.
    if re.search(r"[\d()\[\]]", rest):
        found.append("unread text in the label: %r" % rest)
    named = [ALIASES.get(n.strip().lower(), n.strip().lower())
             for n in rest.split(" and ") if n.strip()]
    for name in named:
        if name not in ingredients:
            found.append("label names %r, not an ingredient of %s"
                         % (name, ingredients))
    for ingredient in ingredients:
        if ingredient not in named:
            found.append("ingredient %r is not named" % ingredient)
    return found


def test_rows_were_examined():
    assert len(ROWS) >= 41 and {code for code, _ in ROWS} == set(CONCEPTS)


@pytest.mark.parametrize("code,label", ROWS, ids=[c for c, _ in ROWS])
def test_every_row_passes(code, label):
    assert not strict_problems(label, CONCEPTS[code]), (code, label)


#: Each of these is accepted by test_terminology_rxnorm_drift.problems() at
#: 05863497 (the last two columns are the code and its true RxNorm name).
WRONG = [
    # a strength with the right digits and the wrong unit
    ("861007", "Metformin 500 mcg"),
    ("861007", "Metformin 500 g"),
    ("197361", "Amlodipine 5 mcg"),
    ("1870230", "Epinephrine 0.3 mL auto-injector"),
    # a number that is in the RxNorm name but is not the strength
    ("1870230", "Epinephrine 1 mg auto-injector"),      # 1 MG/ML
    ("860975", "Metformin 24 mg extended release"),     # 24 HR
    ("861007", "Metformin 500 mg (1000 mg)"),
    # the wrong dose form
    ("861007", "Metformin 500 mg injection"),
    ("197361", "Amlodipine 5 mg capsule"),
    ("665078", "Loratadine 5 mg tablet"),               # chewable
    ("389221", "Etonogestrel 68 mg tablet"),            # implant
    ("1870230", "Epinephrine 0.3 mg tablet"),           # auto-injector
    ("1367439", "Etonogestrel and ethinyl estradiol tablet"),
    ("6809", "Metformin injection"),                    # an ingredient
    ("860974", "Metformin 500 mg injection"),           # a component
    # a release form the concept does not have, or a negated one
    ("861007", "Metformin 500 mg extended release"),
    ("197361", "Amlodipine 5 mg delayed release"),
    ("6809", "Metformin extended release"),
    ("860975", "Metformin 500 mg not extended release"),
    ("860975", "Metformin 500 mg extended release (not delayed release)"),
    # a second medicine after the strength: a combination that is not one
    ("197361", "Amlodipine 5 mg and atorvastatin"),
    ("861007", "Metformin 500 mg with glipizide"),
    # a salt or a brand the concept does not carry
    ("861007", "Metformin pamoate 500 mg"),
    ("197361", "Amlodipine maleate 5 mg"),
    ("617314", "Atorvastatin 10 mg (Crestor)"),         # it is Lipitor
    ("617314", "Atorvastatin 10 mg Crestor"),
    ("617314", "Atorvastatin Crestor 10 mg"),
    # a name that only shares a prefix with the ingredient
    ("6809", "Met"),
    ("6918", "Metoprololol"),
    # a pack
    ("748856", "Drospirenone and ethinyl estradiol (21-day pack)"),
    ("748856", "Drospirenone and ethinyl estradiol tablet"),
    ("748856", "Drospirenone and ethinyl estradiol"),
    ("748856", "Drospirenone and ethinyl estradiol 24 mg (28-day pack)"),
]


@pytest.mark.parametrize("code,wrong", WRONG)
def test_a_wrong_label_is_caught(code, wrong):
    assert strict_problems(wrong, CONCEPTS[code]), (code, wrong)


#: Wrong labels the drift rule already catches stay caught here.
@pytest.mark.parametrize("code,wrong", [
    ("42463", "Amlodipine"),
    ("617314", "Atorvastatin 20 mg"),
    ("860975", "Metformin 500 mg"),
    ("860974", "Metformin"),
    ("6809", "Metformin 500 mg"),
    ("219228", "Acetaminophen"),
    ("7980", "Penicillin V"),
])
def test_what_the_drift_rule_catches_is_still_caught(code, wrong):
    assert strict_problems(wrong, CONCEPTS[code]), (code, wrong)


def _concept(name, tty, numerator, unit, form, ingredient):
    return {"name": name, "tty": tty, "status": "Active", "pack": [],
            "dose_forms": [form],
            "ingredients": [{"ingredient": ingredient, "numerator": numerator,
                             "unit": unit, "active_ingredient": ""}]}


def test_a_unit_is_compared_by_value_not_by_spelling():
    # RxNorm writes levothyroxine in MG; a label may say micrograms.
    levo = _concept("levothyroxine sodium 0.05 MG Oral Tablet", "SCD",
                    "0.05", "MG", "Oral Tablet", "levothyroxine")
    assert not strict_problems("Levothyroxine 50 mcg", levo)
    assert not strict_problems("Levothyroxine 0.05 mg", levo)
    assert strict_problems("Levothyroxine 0.05 mcg", levo)
    assert strict_problems("Levothyroxine 50 mg", levo)


def test_a_name_that_only_starts_like_the_ingredient_is_caught():
    # Niacin and niacinamide are different medicines; a prefix match passes
    # either label on the other's code.
    niacinamide = {"name": "niacinamide", "tty": "IN", "status": "Active",
                   "ingredients": [], "dose_forms": [], "pack": []}
    assert strict_problems("Niacin", niacinamide)
    assert not strict_problems("Niacinamide", niacinamide)


def test_a_dose_form_outside_the_list_fails_until_it_is_added():
    insulin = _concept("insulin glargine 100 UNT/ML Injectable Solution",
                       "SCD", "100", "UNT", "Injectable Solution",
                       "insulin glargine")
    assert strict_problems("Insulin glargine", insulin)
    assert strict_problems("Insulin glargine 100 units", insulin)


@pytest.mark.parametrize("status", ["Obsolete", "Remapped", "Quantified",
                                    "NotCurrent", ""])
def test_a_code_that_is_not_active_is_caught(status):
    concept = dict(CONCEPTS["861007"], status=status)
    assert strict_problems("Metformin 500 mg", concept)
