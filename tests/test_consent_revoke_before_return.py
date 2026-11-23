"""A consent revoked before its return stays revoked.

The consent surface records the grant, then hands the browser the signed
return. A revoke that lands first finds no consent at the engine and gets
404, which the caller reads as "already gone". The engine used to remember
nothing, so the return then stored the consent live while the person's own
list said it was revoked.

A revoke of an unknown id now leaves a revoked tombstone (it still answers
404), and the return refuses a consent id that already has a record.

From the security review's exploit test. Synthetic ids only.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_oauth_consent_handoff import (  # noqa: F401  (fixture)
    MCP_RESOURCE, _grant, _params, _park, _pkce, _register, _return,
    _service, handoff)

pytestmark = pytest.mark.usefixtures('handoff')

TENANT = 'consent-race-t1'


def _exchange(client, reg, code, verifier):
    resp = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'authorization_code', 'code': code,
        'code_verifier': verifier, 'client_id': reg['client_id'],
        'resource': MCP_RESOURCE})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def _introspect(client, token):
    return client.post('/r6/fhir/oauth/introspect', data={
        'token': token, 'client_id': 'mcp-server',
        'client_secret': 'introspection-secret'}).get_json()


def _revoke(client, consent_id):
    return client.post(f'/r6/fhir/oauth/consent/{consent_id}/revoke',
                       headers=_service())


def test_a_consent_revoked_before_its_return_stays_revoked(client):
    """MUTATION: drop the tombstone write in consent_revoke, or the
    existing-record check in consent_return -> a code is issued and its
    token introspects active."""
    reg = _register(client)
    verifier, challenge = _pkce()
    request_id, _, _ = _park(client, reg, challenge)
    grant, payload = _grant(request_id, tenant_id=TENANT)

    assert _revoke(client, payload['consent_id']).status_code == 404

    resp = _return(client, grant)
    location = resp.headers.get('Location', '')
    if 'code=' in location:
        code = parse_qs(urlsplit(location).query)['code'][0]
        tokens = _exchange(client, reg, code, verifier)
        assert _introspect(client, tokens['access_token']) == {'active': False}, (
            'a consent revoked before its return is live')
    assert 'code=' not in location
    assert 'error=access_denied' in location


def test_an_unrevoked_consent_still_returns_a_code(client):
    """Non-vacuity: the same flow without the early revoke issues a code
    whose token introspects active."""
    reg = _register(client)
    verifier, challenge = _pkce()
    request_id, _, _ = _park(client, reg, challenge)
    grant, _payload = _grant(request_id, tenant_id=TENANT)

    _, params = _params(_return(client, grant))
    tokens = _exchange(client, reg, params['code'], verifier)
    assert _introspect(client, tokens['access_token'])['active'] is True


def test_revoking_after_the_return_still_revokes(client):
    reg = _register(client)
    verifier, challenge = _pkce()
    request_id, _, _ = _park(client, reg, challenge)
    grant, payload = _grant(request_id, tenant_id=TENANT)
    _, params = _params(_return(client, grant))
    tokens = _exchange(client, reg, params['code'], verifier)

    resp = _revoke(client, payload['consent_id'])
    assert resp.status_code == 200 and resp.get_json()['revoked'] is True
    assert _introspect(client, tokens['access_token']) == {'active': False}
