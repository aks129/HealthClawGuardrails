"""QA for #1036's fix round: a display that spells the coded unit
differently must not lose a result main scored.

The fix refuses to score when `valueQuantity.unit` and a UCUM `code` are
both present and differ after `_canonical_unit`, which folds count units
only. So a display that is the same unit in another spelling ("mEq/L"
beside the UCUM code "meq/L", "mL/min/1.73m2" beside "mL/min/{1.73_m2}",
"mg/dl" beside "mg/dL") now reads as a disagreement. On main (516a8793)
each row below was scored against the performing lab's own range, the
potassium as HH (critical). On the fix-round head each is indeterminate.

The rule the rows ask for: a display that is the coded unit in another
spelling is agreement, not contradiction. The contradiction rows in
tests/test_qa_1036_code_unit_disagree.py must keep refusing.
Synthetic values only.
"""

import pytest

from r6.labs.interpret import interpret_observation

UCUM = "http://unitsofmeasure.org"


def _obs(loinc, value, unit, code, ref):
    return {"resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": loinc}]},
            "valueQuantity": {"value": value, "unit": unit,
                              "system": UCUM, "code": code},
            "referenceRange": ref}


def _rng(low, high, unit):
    r = {}
    if low is not None:
        r["low"] = {"value": low, "unit": unit}
    if high is not None:
        r["high"] = {"value": high, "unit": unit}
    return [r]


@pytest.mark.xfail(strict=True, reason=(
    "#1036 fix-round QA: a display that spells the UCUM code differently "
    "is read as a contradiction, and a result main scored is lost. Remove "
    "this marker when fixed."))
@pytest.mark.parametrize("loinc,value,unit,code,ref,flag", [
    # Critical potassium, display mEq/L, UCUM code meq/L: HH on main.
    ("2823-3", 6.8, "mEq/L", "meq/L", _rng(3.5, 5.1, "mEq/L"), "HH"),
    # eGFR 45 against the lab's >= 60, display 1.73m2: L on main.
    ("33914-3", 45, "mL/min/1.73m2", "mL/min/{1.73_m2}",
     _rng(60, None, "mL/min/1.73m2"), "L"),
    # Glucose 250, display in lower case: H on main.
    ("2345-7", 250, "mg/dl", "mg/dL", _rng(70, 99, "mg/dl"), "H"),
], ids=["potassium-mEq", "egfr-1.73m2", "glucose-lowercase"])
def test_display_spelling_of_the_code_keeps_the_labs_range(
        loinc, value, unit, code, ref, flag):
    r = interpret_observation(_obs(loinc, value, unit, code, ref))
    assert r["flag"] == flag, r


def test_a_display_contradicting_a_matching_code_is_refused():
    # The fix round's stated rule: code and display both present and not
    # equivalent is a mismatch, even when the code alone would score.
    # Without this row, deleting the disagreement check passed every test
    # (the table compare refuses the K/uL-beside-/uL rows on its own).
    r = interpret_observation(_obs("777-3", 250, "/uL", "10*3/uL", []))
    assert r["flag"] is None, r


def test_a_code_alone_is_reported_as_the_unit():
    o = _obs("777-3", 250, None, "10*3/uL", [])
    del o["valueQuantity"]["unit"]
    r = interpret_observation(o)
    assert r["flag"] == "N" and r["unit"] == "10*3/uL", r


def test_identical_display_and_code_still_score():
    # Control: passes on main and on the fix round.
    r = interpret_observation(_obs("2823-3", 6.8, "mmol/L", "mmol/L",
                                   _rng(3.5, 5.1, "mmol/L")))
    assert r["flag"] == "HH", r
