"""Red-team of PR #922 at 1d7ec0d: digits-only payloads. Synthetic only.
Run from the repo with PYTHONPATH=."""
import pytest
from r6.labs.report import _undecided_name
from r6.redaction import unit_is_shaped


@pytest.mark.parametrize("code", [
    "5550137-7",                                            # phone digits + check
    "٥٥٥٠١٣٧-٧",    # Arabic-Indic digits
])
def test_interpret_names_only_a_loinc_shaped_code(code):
    # Reaches the model through get_labs consumer_summary.unevaluated_analytes.
    assert code not in _undecided_name({"loinc": code})


@pytest.mark.parametrize("unit", ["555-013-7719", "123-45-6789", "5550137719"])
def test_engine_unit_refuses_identifier_shaped_digits(unit):
    assert not unit_is_shaped(unit)
