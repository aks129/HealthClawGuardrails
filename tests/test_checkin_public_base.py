"""Check-in fullUrls name the public engine, not the host that asked (FHI-61).

The CareAgents relay calls the engine server to server, so
`request.host_url` is the internal Railway hostname. Every fullUrl a clinic
received named it, and the wallet rewrites references to those fullUrls
too. The origin now comes from PUBLIC_BASE_URL, the setting the action
rails and SDC delivery links already use. In production an unset or
unusable value refuses the export (503, audited, token not spent) instead
of publishing an internal host. Outside production the request host is
still the fallback, for local development.

Each test names the mutation that turns it red.
"""

import pytest

from r6 import checkin
from tests import checkin_fixtures as f


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', f.MINT_SECRET)
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)


def _production(monkeypatch):
    monkeypatch.setattr(checkin, 'resolve_app_env', lambda: 'production')


def _export(app, client, headers=None):
    f.seed(app, f.patient(), f.condition(), f.coverage(), f.organization())
    types = ['Condition', 'Coverage', 'Patient']
    tok = f.token(client, types)
    return tok, types, f.export(client, tok, types, headers=headers)


def test_the_fullurl_origin_is_the_public_base(app, client, monkeypatch):
    """The internal host the relay used never appears.

    MUTATION: build fullUrl from request.host_url again -> red."""
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://app.healthclaw.io')
    _, _, resp = _export(app, client,
                         headers={'Host': 'engine.railway.internal'})
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert 'railway.internal' not in text
    entries = resp.get_json()['entry']
    assert len(entries) == 4
    for e in entries:
        res = e['resource']
        assert e['fullUrl'] == ('https://app.healthclaw.io/r6/fhir/'
                                f"{res['resourceType']}/{res['id']}")


@pytest.mark.parametrize('base', [
    'https://app.healthclaw.io/', 'https://app.healthclaw.io//',
    ' https://app.healthclaw.io ',
])
def test_a_trailing_slash_or_padding_is_normalised(app, client, monkeypatch,
                                                   base):
    monkeypatch.setenv('PUBLIC_BASE_URL', base)
    _, _, resp = _export(app, client)
    url = resp.get_json()['entry'][0]['fullUrl']
    assert url.startswith('https://app.healthclaw.io/r6/fhir/')


def test_a_base_with_a_path_prefix_keeps_it(app, client, monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.org/engine')
    _, _, resp = _export(app, client)
    assert resp.get_json()['entry'][0]['fullUrl'].startswith(
        'https://example.org/engine/r6/fhir/')


@pytest.mark.parametrize('base', [
    None, '', '   ', 'app.healthclaw.io', 'ftp://app.healthclaw.io',
    'https://', 'https://app.healthclaw.io/?x=1',
    'https://app.healthclaw.io/#frag', 'javascript:alert(1)',
    # Production publishes https only.
    'http://app.healthclaw.io',
    'https://user:pw@app.healthclaw.io',
])
def test_production_refuses_without_a_usable_public_base(
        app, client, monkeypatch, base):
    """Fail closed: no bundle naming an internal host, a 503, audited, and
    the token is not spent.

    MUTATION: fall back to request.host_url in production -> red."""
    _production(monkeypatch)
    if base is not None:
        monkeypatch.setenv('PUBLIC_BASE_URL', base)
    tok, types, resp = _export(app, client)
    assert resp.status_code == 503
    assert 'entry' not in resp.get_json()
    audits = f.export_audits(app)
    assert audits[-1] == (
        'careagents-checkin',
        'share-bundle export (profile=checkin) refused: '
        'PUBLIC_BASE_URL is not configured')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://app.healthclaw.io')
    assert f.export(client, tok, types).status_code == 200


def test_production_uses_a_configured_base(app, client, monkeypatch):
    _production(monkeypatch)
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://app.healthclaw.io')
    _, _, resp = _export(app, client, headers={'Host': 'evil.example'})
    assert resp.status_code == 200
    assert 'evil.example' not in resp.get_data(as_text=True)


def test_the_refusal_comes_before_the_grant_is_checked(app, client,
                                                       monkeypatch):
    """A misconfigured engine answers 503 to any caller, token or not, and
    a bad request still answers 400 first."""
    _production(monkeypatch)
    f.seed(app, f.patient())
    assert f.export(client, None, ['Patient']).status_code == 503
    assert f.export(client, None, ['Organization']).status_code == 400


@pytest.mark.parametrize('base', [None, 'not a url'])
def test_outside_production_the_request_host_is_the_fallback(
        app, client, monkeypatch, base):
    """Local development has no public base to name."""
    if base is not None:
        monkeypatch.setenv('PUBLIC_BASE_URL', base)
    _, _, resp = _export(app, client)
    assert resp.get_json()['entry'][0]['fullUrl'].startswith(
        'http://localhost/r6/fhir/')


def test_public_base_is_the_shared_helper(monkeypatch, app):
    """One helper for every export that names our host (ktc reuses it)."""
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://app.healthclaw.io/')
    with app.test_request_context('/'):
        assert checkin.public_base() == 'https://app.healthclaw.io/'
    monkeypatch.delenv('PUBLIC_BASE_URL')
    with app.test_request_context('/'):
        assert checkin.public_base() == 'http://localhost/'
    _production(monkeypatch)
    with app.test_request_context('/'):
        assert checkin.public_base() is None
