"""QA (PR #846): the host the consent page shows is the host the code lands on.

The page tells a person to "go by the address". That holds only if the host
HealthClaw reports as `redirect_host` is the host a browser actually follows
the final 302 to, and reads to a person as that host. Each row registers a
crafted callback and accepts either answer: refused at registration, or shown
exactly as the browser will resolve it.
"""
import json
from urllib.parse import urlsplit

import pytest

from tests.test_oauth_consent_handoff import (
    MCP_RESOURCE, _grant, _pkce, _return, _service, handoff)  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures('handoff')


def _register_uri(client, uri):
    return client.post('/r6/fhir/oauth/register', data=json.dumps({
        'client_name': 'Claude', 'token_endpoint_auth_method': 'none',
        'redirect_uris': [uri]}), content_type='application/json')


def _shown_host(client, uri):
    """(registration status, redirect_host shown, final Location) for `uri`."""
    reg = _register_uri(client, uri)
    if reg.status_code != 201:
        return reg.status_code, None, None
    _, challenge = _pkce()
    az = client.get('/r6/fhir/oauth/authorize', query_string={
        'client_id': reg.get_json()['client_id'], 'redirect_uri': uri,
        'code_challenge': challenge, 'code_challenge_method': 'S256',
        'scope': 'fhir.read', 'state': 's', 'resource': MCP_RESOURCE})
    assert az.status_code == 302, az.get_data(as_text=True)
    req = dict(p.split('=', 1) for p in urlsplit(az.headers['Location']).query.split('&'))
    request_id = req['req'].split('.')[0]
    shown = client.get(f'/r6/fhir/oauth/consent/{request_id}', headers=_service())
    grant, _ = _grant(request_id)
    back = _return(client, grant)
    assert back.status_code == 302
    return 201, shown.get_json()['redirect_host'], back.headers['Location']


def test_a_backslash_callback_lands_on_the_host_the_page_shows(client):
    """`https://evil.example\\@claude.ai/...`: Python reads host claude.ai, a
    browser reading the raw string goes to evil.example. Today Werkzeug
    percent-encodes the backslash on the way out, so the browser lands on
    claude.ai too. This pins that parity (or a refusal) against a Werkzeug
    upgrade or a Location header written by hand."""
    status, host, location = _shown_host(
        client, 'https://evil.example\\@claude.ai/api/mcp/auth_callback')
    if status != 201:
        return
    assert '\\' not in location
    assert host == urlsplit(location).hostname == 'claude.ai'


@pytest.mark.parametrize('uri, browser_host', [
    # Cyrillic U+0441 in place of the Latin c: reads as claude.ai.
    ('https://сlaude.ai/api/mcp/auth_callback', 'xn--laude-0ye.ai'),
    # Ideographic full stop: a browser maps it to '.'.
    ('https://claude.ai。evil.example/cb', 'claude.ai.evil.example'),
    # Percent-escapes in the host: a browser decodes them before resolving.
    ('https://claude.ai%2eevil.example/cb', 'claude.ai.evil.example'),
    ('https://%63laude.ai/cb', 'claude.ai'),
])
def test_a_non_ascii_host_is_refused_or_shown_as_the_browser_resolves_it(
        client, uri, browser_host):
    """FAILS on PR #846 head ee53a4a: the page is shown 'сlaude.ai' (Cyrillic),
    which a person cannot tell from claude.ai, under copy that says 'go by the
    address'. Browser hosts from the WHATWG URL parser (Node `new URL`)."""
    status, host, _ = _shown_host(client, uri)
    if status != 201:
        return
    assert host == browser_host
