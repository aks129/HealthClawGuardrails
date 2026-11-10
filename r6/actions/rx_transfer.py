"""Schedule II check for medication orders.

This module used to build a prescription-transfer phone call: a free-text
script, spoken to a pharmacy, that named each medicine. That builder is gone
with the route that called it. Its script took a medicine's name from the
feed's own `text` and `display` ("recorded as: ..."), and from nothing else
when the code had no label of ours. Feed text is where real feeds put a
patient's name, so it is never repeated to a third party: a spoken or shown
name comes from r6/terminology.py, keyed by code, or there is no name.
tests/test_rx_transfer.py::test_the_feeds_names_reach_nothing_but_the_schedule_ii_check
holds that line for this module.

What stays is the check, because reading a name to refuse an order is not
repeating it. NOTHING CALLS IT TODAY: with the builder gone, no code path in
r6/ refuses anything on its say. It is kept, and tested directly, for the
call builder that replaces the old one; until that lands this module
enforces nothing.
- Schedule II is never transferable or refillable by phone under federal
  rule, which is why the check exists. The deny-list below is a conservative keyword match, NOT an
  authoritative DEA schedule lookup; false positives are acceptable (the
  patient can still call themselves), false negatives are caught by the
  pharmacist.
- The match is not keyed on the feed's word for the drug. It used to read
  `medicationCodeableConcept.text` first and stop there, so RxNorm 7804
  (oxycodone) with `text: "pain reliever"` passed, and so did a code-only
  order, which is exactly what a redacted read yields for a code the label
  table does not know. Every name-bearing signal is checked (our own label
  for the code, the text, every coding display), and the RxNorm ingredient
  codes below are checked directly.
"""

from __future__ import annotations

from r6.terminology import RXNORM, canonical_system, lookup

# Conservative Schedule II keyword deny-list (lowercase). Not authoritative.
SCHEDULE_II_TERMS = (
    "oxycodone", "oxycontin", "percocet", "hydrocodone", "vicodin", "norco",
    "fentanyl", "morphine", "hydromorphone", "dilaudid", "oxymorphone",
    "meperidine", "demerol", "methadone", "codeine sulfate",
    "amphetamine", "dextroamphetamine", "adderall", "vyvanse",
    "lisdexamfetamine", "methylphenidate", "ritalin", "concerta", "focalin",
    "dexmethylphenidate", "cocaine", "pentobarbital", "secobarbital",
    "tapentadol", "nucynta",
)


# RxNorm ingredient-level RxCUIs (TTY=IN) for the same substances, so a
# coded order is caught even when nothing names it. Ingredient level,
# because that is what a static list can hold; product-level codes (a
# specific tablet) are caught by name wherever the feed or our label table
# carries one. Not authoritative, same caveat as the terms above. Every
# entry was checked against RxNav on 2026-09-14
# (https://rxnav.nlm.nih.gov/REST/rxcui/<id>/properties.json); the first
# draft carried one code that resolved to nothing, a silent miss rather
# than a false positive, which is why the check is written down here.
SCHEDULE_II_RXCUI = frozenset({
    "7804",    # oxycodone
    "5489",    # hydrocodone
    "4337",    # fentanyl
    "7052",    # morphine
    "3423",    # hydromorphone
    "7814",    # oxymorphone
    "6813",    # methadone
    "6754",    # meperidine
    "2670",    # codeine (code only: the word is on transferable III-V combos)
    "725",     # amphetamine
    "3288",    # dextroamphetamine
    "6901",    # methylphenidate
    "352372",  # dexmethylphenidate
    "700810",  # lisdexamfetamine
    "787390",  # tapentadol
    "8004",    # pentobarbital
    "9624",    # secobarbital
    "2653",    # cocaine
})

def _codings(med: dict) -> list[dict]:
    concept = med.get("medicationCodeableConcept") or {}
    return [c for c in (concept.get("coding") or []) if isinstance(c, dict)]


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    return [n for n in names if not (n.lower() in seen or seen.add(n.lower()))]


def _label_names(med: dict) -> list[str]:
    """Our own labels for the order's codes (r6/terminology.py)."""
    names = []
    for coding in _codings(med):
        label = lookup(coding.get("system"), coding.get("code"))
        if label:
            names.append(label)
    return _dedupe(names)


def _feed_names(med: dict) -> list[str]:
    """What the feed itself calls the order: `text`, then every display."""
    concept = med.get("medicationCodeableConcept") or {}
    names = []
    if isinstance(concept.get("text"), str) and concept["text"].strip():
        names.append(concept["text"].strip())
    for coding in _codings(med):
        display = coding.get("display")
        if isinstance(display, str) and display.strip():
            names.append(display.strip())
    return _dedupe(names)


def medication_names(med: dict) -> list[str]:
    """Every name the order carries, our own label for the code first.

    For the Schedule II check and nothing else: the feed's names in this
    list are read to refuse an order, never shown or spoken.
    """
    return _dedupe(_label_names(med) + _feed_names(med))


def _is_schedule_ii(med: dict, names: list[str]) -> bool:
    for coding in _codings(med):
        if (canonical_system(coding.get("system")) == RXNORM
                and str(coding.get("code")) in SCHEDULE_II_RXCUI):
            return True
    return any(term in name.lower()
               for name in names for term in SCHEDULE_II_TERMS)
