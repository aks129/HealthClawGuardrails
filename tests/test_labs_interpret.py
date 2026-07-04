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
