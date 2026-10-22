"""QA pin (PR #977): a bare `?` or `#` on PUBLIC_BASE_URL is unusable.

urlsplit gives an empty `.query` / `.fragment` for a trailing bare `?` / `#`,
so only the raw-string checks in r6/checkin.public_base refuse them;
removing `'?' not in raw` survived tests/test_checkin_public_base.py.
Unrefused, every fullUrl would read `https://host?/r6/fhir/...`.
"""

import pytest

from r6 import checkin


@pytest.mark.parametrize('base', [
    'https://app.healthclaw.io?', 'https://app.healthclaw.io/?',
    'https://app.healthclaw.io#', 'https://app.healthclaw.io/#',
])
@pytest.mark.parametrize('production', [True, False])
def test_a_bare_query_or_fragment_marker_is_unusable(
        app, monkeypatch, base, production):
    monkeypatch.setenv('PUBLIC_BASE_URL', base)
    monkeypatch.setattr(checkin, 'resolve_app_env',
                        lambda: 'production' if production else 'development')
    with app.test_request_context('/', base_url='http://engine.internal/'):
        got = checkin.public_base()
    assert got == (None if production else 'http://engine.internal/')
