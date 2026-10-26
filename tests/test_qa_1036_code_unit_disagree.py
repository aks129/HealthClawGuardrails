"""QA for #1036: a UCUM `valueQuantity.code` that contradicts the
human-readable `unit` must not let the number be scored.

FHIR's `code` (with system UCUM) is the computable unit; `unit` is display
text. `r6/labs/interpret.py` reads `unit` alone, so when a feed writes
"K/uL" beside a UCUM code of "/uL" and a value of 250000, the value is
scored as thousands per microlitre: a critical-high platelet count from a
normal one. `r6/labs/trend.py` `_unit` already prefers the coded unit.

On main the "K/uL" rows were refused as a unit mismatch; #1036 widened the
spellings that score, so they now score. The exact "10*3/uL" row was
already wrong on main. Synthetic values only.
"""

import pytest

from r6.labs.interpret import interpret_observation

UCUM = "http://unitsofmeasure.org"


def _obs(value, unit, code):
    return {"resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": "777-3"}]},
            "valueQuantity": {"value": value, "unit": unit,
                              "system": UCUM, "code": code}}


@pytest.mark.xfail(strict=True, reason=(
    "#1036 QA: interpret_observation ignores a contradicting UCUM code. "
    "Remove this marker when the coded unit is checked."))
@pytest.mark.parametrize("value,unit,code", [
    (250000, "K/uL", "/uL"),         # newly scored by #1036
    (250000, "thou/uL", "/uL"),      # newly scored by #1036
    (250000, "10*3/uL", "/uL"),      # already scored on main
    (0.25, "10*9/L", "10*6/uL"),     # main: 10*9/L was a mismatch
])
def test_contradicting_ucum_code_is_not_scored(value, unit, code):
    r = interpret_observation(_obs(value, unit, code))
    assert r["flag"] is None, r


def test_agreeing_ucum_code_still_scores():
    # Control: the fix must not refuse a consistent pair.
    r = interpret_observation(_obs(250, "K/uL", "10*3/uL"))
    assert r["flag"] == "N", r
