from r6.actions import errors


def test_taxonomy_is_frozen_and_complete():
    expected = {
        'provider_not_configured', 'contact_not_allowlisted', 'daily_cap_reached',
        'payload_invalid', 'provider_error', 'extraction_ambiguous',
        'emergency_indicated', 'stale_source_data',
    }
    assert set(errors.ALL) == expected
    assert errors.EMERGENCY_INDICATED == 'emergency_indicated'
