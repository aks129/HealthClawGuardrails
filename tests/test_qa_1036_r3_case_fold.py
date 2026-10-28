"""QA for #1036 round 3: `_unit_key` lower-cases the whole unit, so case
that carries meaning in UCUM is folded away.

In case-sensitive UCUM, m is milli and M is mega, p is pico and P is peta,
ms is a millisecond and Ms a megasecond. `_same_unit` joins each pair. An
all-capitals display ("MG/DL", "MMOL/L") is UCUM's case-insensitive form
and must keep matching; a mixed-case spelling whose prefix changes meaning
must not.

Reachable today only through the table's units (mmol/L, mg/dL, g/dL, %,
10*3/uL, mL/min/{1.73_m2}): a potassium written "Mmol/L" scores HH. A
megamole is no real result, so no wrong number reaches a person yet; it
does as soon as an analyte in U/L or mIU/L joins LOINC_RANGES.
Synthetic values only.
"""

import pytest

from r6.labs.interpret import _same_unit, interpret_observation

pytestmark = pytest.mark.extension


#: Mixed case, so not UCUM's all-capitals case-insensitive form: the capital
#: is a prefix. ("MU/L" in capitals alone is ambiguous: case-insensitive
#: UCUM reads it as milli-units, case-sensitive as mega-units.)
@pytest.mark.parametrize("a,b", [
    ("mU/mL", "MU/mL"), ("mIU/mL", "MIU/mL"), ("mmol/L", "Mmol/L"),
    ("pmol/L", "Pmol/L"), ("ms", "Ms"), ("mg", "Mg"), ("mm/h", "Mm/h"),
])
def test_prefixes_that_differ_by_case_are_different_units(a, b):
    assert not _same_unit(a, b)


def _obs(loinc, value, unit):
    return {"resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": loinc}]},
            "valueQuantity": {"value": value, "unit": unit}}


@pytest.mark.parametrize("loinc,value,unit", [
    ("2823-3", 6.8, "Mmol/L"),   # megamole per litre
    ("2345-7", 250, "Mg/dL"),    # megagram per decilitre
])
def test_a_mega_spelling_of_a_table_unit_is_not_scored(loinc, value, unit):
    assert interpret_observation(_obs(loinc, value, unit))["flag"] is None


@pytest.mark.parametrize("a,b", [
    ("MG/DL", "mg/dL"), ("MMOL/L", "mmol/L"), ("mg/dl", "mg/dL"),
    ("mEq/L", "meq/L"), ("PG/ML", "pg/mL"), ("NMOL/L", "nmol/L"),
    ("mL/min/1.73m2", "mL/min/{1.73_m2}"), ("10*3/µL", "10*3/uL"),
])
def test_spellings_that_are_one_unit_still_match(a, b):
    # Control: the round-3 fix's purpose. Must stay true after the fix.
    assert _same_unit(a, b)
