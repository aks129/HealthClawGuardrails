# tests/test_labs_interpret.py
from r6.labs.interpret import LOINC_RANGES, REFERENCES


def test_every_range_has_a_nonempty_source():
    # Principle 4: no un-sourced range may ship.
    for loinc, entry in LOINC_RANGES.items():
        assert entry.get("source"), f"{loinc} ({entry.get('name')}) missing source"
        assert entry["source"] in REFERENCES, f"{loinc} source not in REFERENCES"


def test_core_analytes_present():
    for loinc in ("2823-3", "2951-2", "2345-7", "4548-4", "718-7", "777-3"):
        assert loinc in LOINC_RANGES


from r6.labs.interpret import interpret_observation


def _obs(loinc, value, unit="mmol/L", ref=None):
    o = {"resourceType": "Observation", "status": "final",
         "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
         "valueQuantity": {"value": value, "unit": unit}}
    if ref is not None:
        o["referenceRange"] = ref
    return o


def test_normal_potassium_is_N():
    r = interpret_observation(_obs("2823-3", 4.2))
    assert r["flag"] == "N" and r["critical"] is False
    assert r["range_source"] == "table" and r["analyte"] == "Potassium"


def test_high_potassium_is_H():
    assert interpret_observation(_obs("2823-3", 5.6))["flag"] == "H"


def test_critical_high_potassium_is_HH():
    r = interpret_observation(_obs("2823-3", 7.0))
    assert r["flag"] == "HH" and r["critical"] is True


def test_low_and_critical_low():
    assert interpret_observation(_obs("2823-3", 3.0))["flag"] == "L"
    assert interpret_observation(_obs("2823-3", 2.0))["flag"] == "LL"


def test_resource_range_wins_over_table():
    # Value 5.6 is table-high, but the lab's own range makes it normal.
    ref = [{"low": {"value": 3.0}, "high": {"value": 6.0}}]
    r = interpret_observation(_obs("2823-3", 5.6, ref=ref))
    assert r["flag"] == "N" and r["range_source"] == "resource"


def test_unit_mismatch_is_indeterminate():
    r = interpret_observation(_obs("2823-3", 4.2, unit="mg/dL"))
    assert r["flag"] is None and r["range_source"] == "none"
    assert "indeterminate" in r["note"].lower()


def test_unknown_loinc_is_indeterminate():
    r = interpret_observation(_obs("9999-9", 4.2))
    assert r["flag"] is None and r["range_source"] == "none"


def test_missing_value_is_indeterminate():
    o = {"resourceType": "Observation",
         "code": {"coding": [{"system": "http://loinc.org", "code": "2823-3"}]}}
    assert interpret_observation(o)["flag"] is None


def test_sex_specific_hemoglobin():
    female = {"resourceType": "Patient", "gender": "female"}
    male = {"resourceType": "Patient", "gender": "male"}
    # 12.5 g/dL: normal for female (>=12.0), low for male (<13.5)
    assert interpret_observation(_obs("718-7", 12.5, unit="g/dL"), female)["flag"] == "N"
    assert interpret_observation(_obs("718-7", 12.5, unit="g/dL"), male)["flag"] == "L"


def test_component_only_observation_skipped():
    o = {"resourceType": "Observation",
         "code": {"coding": [{"system": "http://loinc.org", "code": "55284-4"}]},
         "component": [{"code": {"coding": [{"code": "8480-6"}]},
                        "valueQuantity": {"value": 138, "unit": "mmHg"}}]}
    assert interpret_observation(o)["range_source"] == "none"


def test_resource_range_wrong_unit_falls_back_to_table():
    # Glucose 54 mg/dL with a referenceRange quoted in mmol/L must NOT be read
    # against the mmol/L numbers (that would call a near-critical low "high").
    ref = [{"low": {"value": 3.9, "unit": "mmol/L"},
            "high": {"value": 6.1, "unit": "mmol/L"}}]
    r = interpret_observation(_obs("2345-7", 54, unit="mg/dL", ref=ref))
    assert r["range_source"] == "table" and r["flag"] == "L"


def test_missing_unit_is_indeterminate():
    o = {"resourceType": "Observation",
         "code": {"coding": [{"system": "http://loinc.org", "code": "2823-3"}]},
         "valueQuantity": {"value": 7.0}}
    assert interpret_observation(o)["flag"] is None


def test_non_numeric_value_is_indeterminate():
    r = interpret_observation(_obs("2823-3", "4.2"))
    assert r["flag"] is None and r["range_source"] == "none"


def test_boolean_value_is_indeterminate():
    assert interpret_observation(_obs("2823-3", True))["flag"] is None


def test_one_sided_high_cholesterol():
    assert interpret_observation(_obs("2093-3", 201, unit="mg/dL"))["flag"] == "H"
    assert interpret_observation(_obs("2093-3", 200, unit="mg/dL"))["flag"] == "N"


def test_one_sided_low_hdl_sex_specific():
    female = {"resourceType": "Patient", "gender": "female"}
    assert interpret_observation(_obs("2085-9", 49, unit="mg/dL"), female)["flag"] == "L"
    assert interpret_observation(_obs("2085-9", 50, unit="mg/dL"), female)["flag"] == "N"
    male = {"resourceType": "Patient", "gender": "male"}
    assert interpret_observation(_obs("2085-9", 39, unit="mg/dL"), male)["flag"] == "L"


def test_egfr_low_and_critical_low():
    assert interpret_observation(_obs("33914-3", 50, unit="mL/min/{1.73_m2}"))["flag"] == "L"
    assert interpret_observation(_obs("33914-3", 10, unit="mL/min/{1.73_m2}"))["flag"] == "LL"


def test_boundary_values_land_on_less_severe_side():
    # Normal-range bounds are inclusive-normal (at the bound = N)...
    assert interpret_observation(_obs("2823-3", 3.5))["flag"] == "N"
    assert interpret_observation(_obs("2823-3", 5.1))["flag"] == "N"
    # ...but PANIC thresholds are inclusive-critical: institutional critical
    # value lists are "K <= 2.5", so the exact threshold must alert
    # (convention changed 2026-07-08 per audit — safer side for decision support).
    assert interpret_observation(_obs("2823-3", 2.5))["flag"] == "LL"
    assert interpret_observation(_obs("2823-3", 6.5))["flag"] == "HH"


def test_resource_range_with_only_low_bound():
    ref = [{"low": {"value": 3.0}}]
    r = interpret_observation(_obs("2823-3", 2.0, ref=ref))
    assert r["range_source"] == "resource" and r["flag"] == "LL"  # crit_low 2.5 from table


def test_exact_panic_threshold_is_critical():
    # A potassium of exactly 6.5 (the panic threshold) must flag HH, not H
    # (audit finding 2026-07-08: strict > let the boundary value through).
    r = interpret_observation(_obs("2823-3", 6.5))
    assert r["flag"] == "HH" and r["critical"] is True
    r = interpret_observation(_obs("2823-3", 2.5))
    assert r["flag"] == "LL" and r["critical"] is True


def test_one_sided_resource_range_never_yields_false_normal():
    # Lab supplied only a LOW bound (3.5). Value 6.0 exceeds the table's high
    # (5.1) but not the lab's (absent) high — calling that "N" is a false
    # normal. It must be indeterminate, never N.
    ref = [{"low": {"value": 3.5, "unit": "mmol/L"}}]
    r = interpret_observation(_obs("2823-3", 6.0, ref=ref))
    assert r["flag"] != "N"
    assert "indeterminate" in (r["note"] or "")


def test_one_sided_resource_range_defined_side_still_works():
    # The defined side of a one-sided range still flags normally.
    ref = [{"low": {"value": 3.5, "unit": "mmol/L"}}]
    r = interpret_observation(_obs("2823-3", 3.0, ref=ref))
    assert r["flag"] == "L"
    # and a value inside BOTH the lab's low and the table's high is normal
    r = interpret_observation(_obs("2823-3", 4.2, ref=ref))
    assert r["flag"] == "N"
