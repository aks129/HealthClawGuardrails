"""QA for #1036 round 4: `_unit_key`'s micro-sign fold, outside the count
family.

The count family folds its micro signs in `_count_key`, so every count row
passes without `_unit_key`'s fold. Measured by mutation: with the micro
fold taken out of `_unit_key`, the whole unit suite still passed. It is
reachable through a lab's own range: a creatinine reported in umol/L
beside a range written with either micro sign is one unit, and the range
must be read. Synthetic values only.
"""

import pytest

from r6.labs.interpret import interpret_observation


@pytest.mark.parametrize("range_unit", ["µmol/L", "μmol/L"])
def test_a_micro_sign_in_the_labs_range_is_the_same_unit(range_unit):
    # 120 umol/L is above the lab's 62-106: high on the lab's own range.
    # The table is mg/dL, so a skipped range would be a unit mismatch.
    o = {"resourceType": "Observation", "status": "final",
         "code": {"coding": [{"system": "http://loinc.org",
                              "code": "2160-0"}]},
         "valueQuantity": {"value": 120, "unit": "umol/L"},
         "referenceRange": [{"low": {"value": 62, "unit": range_unit},
                             "high": {"value": 106, "unit": range_unit}}]}
    r = interpret_observation(o)
    assert r["range_source"] == "resource" and r["flag"] == "H", r
