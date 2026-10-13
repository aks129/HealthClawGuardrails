"""Red-team of PR #922 at 818998e: a unit word in front of digits. Synthetic only."""
import pytest
from r6.redaction import unit_is_shaped


@pytest.mark.parametrize("unit", ["mg/555-013-7719", "g/123-45-6789", "g5550137719", "{beats}/5550137719"])
def test_engine_unit_refuses_identifier_digits_beside_a_unit_word(unit):
    assert not unit_is_shaped(unit)
