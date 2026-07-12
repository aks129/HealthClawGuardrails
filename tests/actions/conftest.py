"""Fixtures for the action-contract test package."""
import pytest


@pytest.fixture(autouse=True)
def _fresh_nonce_cache():
    """The step-up replay guard is a process-global cache; /confirm consumes
    nonces (single-use execution credential), so clear it around every test
    to keep token consumption from leaking across tests."""
    from r6.stepup import clear_nonce_cache
    clear_nonce_cache()
    yield
    clear_nonce_cache()
