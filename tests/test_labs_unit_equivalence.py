# tests/test_labs_unit_equivalence.py
"""Equivalent spellings of a blood-count unit score; different units don't.

Real feeds send the same quantity as 10*3/uL, K/uL, x10E3/uL, 10^3/uL,
thou/uL or 10*9/L, and a range quoted in another of them. Each of those was
refused as "units didn't match the reference range" for platelets and white
cells. 10*9/L is the same number: a microlitre is 10^-6 L, so
10^3/uL = 10^9/L exactly. Synthetic values only.
"""
import pytest

from r6.labs.interpret import UNIT_MISMATCH, interpret_observation

PLATELETS, WBC = "777-3", "6690-2"

#: Every spelling of thousands-per-microlitre the interpreter accepts.
EQUIVALENT = ["10*3/uL", "K/uL", "x10E3/uL", "10^3/uL", "thou/uL", "10*9/L",
              "10*3/µL", "10*3/μL", "10E3/uL", "x10^9/L"]

#: Each spelling paired with a different one, so every spelling appears on
#: both the value side and the range side.
PAIRS = [(a, EQUIVALENT[(i + 1) % len(EQUIVALENT)])
         for i, a in enumerate(EQUIVALENT)]

#: Count units that are NOT the same quantity: per microlitre without the
#: thousands (1000x), millions (RBC-shaped), per millilitre (1000x), and
#: 10^9 per microlitre (10^6x). Each must still refuse to score.
NOT_EQUIVALENT = ["/uL", "10*6/uL", "10*3/mL", "10*9/uL", "x10E6/uL"]


def _obs(loinc, value, unit, ref=None):
    o = {"resourceType": "Observation", "status": "final",
         "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
         "valueQuantity": {"value": value, "unit": unit}}
    if ref is not None:
        o["referenceRange"] = ref
    return o


@pytest.mark.parametrize("loinc,value,flag", [
    (PLATELETS, 250, "N"), (PLATELETS, 120, "L"), (WBC, 7.0, "N"),
    (WBC, 12.5, "H")])
@pytest.mark.parametrize("unit", EQUIVALENT)
def test_equivalent_spelling_scores_against_table(unit, loinc, value, flag):
    r = interpret_observation(_obs(loinc, value, unit))
    assert r["range_source"] == "table" and r["flag"] == flag, r
    # The stored spelling is reported, not the canonical one.
    assert r["unit"] == unit


@pytest.mark.parametrize("value_unit,range_unit", PAIRS)
def test_equivalent_spelling_uses_the_labs_own_range(value_unit, range_unit):
    # 420 is high on the table (400) but inside the lab's own 150-450: the
    # lab's range must be the one read, so a match proves it was trusted.
    ref = [{"low": {"value": 150, "unit": range_unit},
            "high": {"value": 450, "unit": range_unit}}]
    r = interpret_observation(_obs(PLATELETS, 420, value_unit, ref=ref))
    assert r["range_source"] == "resource" and r["flag"] == "N", r


@pytest.mark.parametrize("value_unit,range_unit", PAIRS)
def test_equivalent_one_sided_range_still_guards_the_missing_side(
        value_unit, range_unit):
    # The lab gave only a low bound; 600 is above the table's high (400).
    # Calling it normal would be a false normal: indeterminate, as when the
    # spellings are identical.
    ref = [{"low": {"value": 150, "unit": range_unit}}]
    r = interpret_observation(_obs(PLATELETS, 600, value_unit, ref=ref))
    assert r["flag"] is None and r["indeterminate_reason"] != UNIT_MISMATCH


@pytest.mark.parametrize("unit", NOT_EQUIVALENT)
@pytest.mark.parametrize("loinc", [PLATELETS, WBC])
def test_non_equivalent_unit_stays_unscored(unit, loinc):
    r = interpret_observation(_obs(loinc, 250, unit))
    assert r["flag"] is None and r["range_source"] == "none"
    assert r["indeterminate_reason"] == UNIT_MISMATCH


@pytest.mark.parametrize("unit", NOT_EQUIVALENT)
def test_non_equivalent_lab_range_is_not_trusted(unit):
    # Value in thousands per microlitre, the lab's range in another unit: the
    # range is skipped and the table read, never the range's numbers.
    ref = [{"low": {"value": 150000, "unit": unit},
            "high": {"value": 450000, "unit": unit}}]
    r = interpret_observation(_obs(PLATELETS, 250, "K/uL", ref=ref))
    assert r["range_source"] == "table" and r["flag"] == "N"


def test_other_analytes_are_not_widened():
    # The equivalence is for count units only: a potassium in a count unit
    # is still a mismatch, and mg/dL against mmol/L still falls back.
    r = interpret_observation(_obs("2823-3", 4.2, "K/uL"))
    assert r["indeterminate_reason"] == UNIT_MISMATCH


UCUM = "http://unitsofmeasure.org"


@pytest.mark.parametrize("code,flag", [("10*3/uL", "N"), ("10*9/L", "N"),
                                       ("/uL", None)])
def test_coded_unit_alone_is_what_is_compared(code, flag):
    o = _obs(PLATELETS, 250, None)
    o["valueQuantity"] = {"value": 250, "system": UCUM, "code": code}
    assert interpret_observation(o)["flag"] == flag


def test_a_code_from_another_system_is_not_a_unit():
    o = _obs(PLATELETS, 250, "K/uL")
    o["valueQuantity"].update({"system": "urn:x", "code": "/uL"})
    assert interpret_observation(o)["flag"] == "N"
