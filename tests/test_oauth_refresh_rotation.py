"""Refresh tokens rotate, and a replayed one takes the whole chain with it.

Spec §13.5: hourly re-consent on a person's own records is not acceptable, so
the token endpoint offers `grant_type=refresh_token`; OAuth 2.1 §4.3.1 makes
rotation a MUST for public clients, and reuse of a rotated token revokes the
chain. Every token in a chain carries the audience, tenant and consent of the
authorization that started it. Each test names its mutation.
"""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from r6 import oauth

FHIR_RESOURCE = 'http://localhost/r6/fhir'


def _pkce():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


def _register(client, method='none', name='Claude'):
    body = {'client_name': name, 'redirect_uris': ['https://client.example/cb'],
            'token_endpoint_auth_method': method}
    return client.post('/r6/fhir/oauth/register', data=json.dumps(body),
                       content_type='application/json').get_json()


def _creds(reg):
    creds = {'client_id': reg['client_id']}
    if 'client_secret' in reg:
        creds['client_secret'] = reg['client_secret']
    return creds


def _grant_tokens(client, reg, scope='fhir.read context.read'):
    """A full code grant; returns the token response body."""
    verifier, challenge = _pkce()
    resp = client.get('/r6/fhir/oauth/authorize', query_string={
        'client_id': reg['client_id'], 'redirect_uri': reg['redirect_uris'][0],
        'code_challenge': challenge, 'code_challenge_method': 'S256',
        'scope': scope, 'resource': FHIR_RESOURCE},
        headers={'X-Tenant-Id': 'desktop-demo'})
    assert resp.status_code == 302, resp.get_data(as_text=True)
    code = parse_qs(urlsplit(resp.headers['Location']).query)['code'][0]
    token = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'authorization_code', 'code': code,
        'code_verifier': verifier, **_creds(reg)})
    assert token.status_code == 200, token.get_data(as_text=True)
    return token.get_json()


def _refresh(client, reg, refresh_token, **extra):
    return client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'refresh_token', 'refresh_token': refresh_token,
        **_creds(reg), **extra})


def test_a_code_grant_also_returns_a_refresh_token_and_discovery_says_so(client):
    reg = _register(client)
    body = _grant_tokens(client, reg)
    assert body['refresh_token'] and body['refresh_token'] != body['access_token']
    doc = client.get('/.well-known/oauth-authorization-server').get_json()
    assert 'refresh_token' in doc['grant_types_supported']


def test_a_refresh_returns_new_tokens_carrying_the_original_binding(client):
    reg = _register(client)
    first = _grant_tokens(client, reg)
    resp = _refresh(client, reg, first['refresh_token'])
    assert resp.status_code == 200, resp.get_data(as_text=True)
    second = resp.get_json()
    assert second['access_token'] != first['access_token']
    assert second['refresh_token'] != first['refresh_token']
    assert second['token_type'] == 'Bearer' and second['scope'] == first['scope']
    assert resp.headers['Cache-Control'] == 'no-store'
    ok, info = oauth.validate_bearer_token(second['access_token'])
    assert ok, info
    assert info['tenant_id'] == 'desktop-demo'
    assert info['aud'] == FHIR_RESOURCE


def test_the_presented_refresh_token_is_spent_by_the_rotation(client):
    """MUTATION: `_oauth_store_get` instead of `_oauth_store_pop` on the
    presented refresh token -> the old one keeps working and this goes red."""
    reg = _register(client)
    first = _grant_tokens(client, reg)
    assert _refresh(client, reg, first['refresh_token']).status_code == 200
    again = _refresh(client, reg, first['refresh_token'])
    assert again.status_code == 400
    assert again.get_json()['error'] == 'invalid_grant'


def test_replaying_a_rotated_token_revokes_the_whole_chain(client):
    """OAuth 2.1 §4.3.1. MUTATION: skip `_revoke_refresh_chain` on reuse ->
    the newest token in the chain keeps working and this goes red."""
    reg = _register(client)
    first = _grant_tokens(client, reg)
    second = _refresh(client, reg, first['refresh_token']).get_json()
    third = _refresh(client, reg, second['refresh_token']).get_json()
    # Somebody presents the first token again: a replay of a spent token.
    assert _refresh(client, reg, first['refresh_token']).status_code == 400
    # The legitimate holder's current token is dead too.
    cut = _refresh(client, reg, third['refresh_token'])
    assert cut.status_code == 400 and cut.get_json()['error'] == 'invalid_grant'
    # And so is every access token the chain issued, not only the refresh
    # side (the Mac mini session measured the first cut leaving them alive
    # for their own hour). MUTATION: drop the access_hashes loop in
    # _revoke_refresh_chain -> red.
    for body in (first, second, third):
        ok, why = oauth.validate_bearer_token(body['access_token'])
        assert not ok, f'an access token from the revoked chain still validates: {why}'


def test_a_refresh_token_is_bound_to_the_client_it_was_issued_to(client):
    """MUTATION: drop `_client_authenticates` from the refresh grant -> red."""
    owner = _register(client)
    other = _register(client, name='Not Claude')
    first = _grant_tokens(client, owner)
    stolen = _refresh(client, other, first['refresh_token'])
    assert stolen.status_code == 400 and stolen.get_json()['error'] == 'invalid_grant'
    # ...and the wrong presenter did not burn it for the right one (found by
    # the Mac mini session's live pass on the first cut, which spent the token
    # before asking who was presenting it).
    still_good = _refresh(client, owner, first['refresh_token'])
    assert still_good.status_code == 200, still_good.get_data(as_text=True)
    confidential = _register(client, method='client_secret_post')
    minted = _grant_tokens(client, confidential)
    no_secret = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'refresh_token', 'refresh_token': minted['refresh_token'],
        'client_id': confidential['client_id']})
    assert no_secret.status_code == 401
    assert no_secret.get_json()['error'] == 'invalid_client'


def test_a_public_client_must_name_itself_on_refresh(client):
    reg = _register(client)
    first = _grant_tokens(client, reg)
    resp = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'refresh_token', 'refresh_token': first['refresh_token']})
    assert resp.status_code == 400 and resp.get_json()['error'] == 'invalid_request'


def test_scope_may_narrow_on_refresh_and_never_widen(client):
    """MUTATION: drop the subset check -> the widening row goes red."""
    reg = _register(client)
    first = _grant_tokens(client, reg, scope='fhir.read context.read')
    narrowed = _refresh(client, reg, first['refresh_token'], scope='fhir.read')
    assert narrowed.status_code == 200 and narrowed.get_json()['scope'] == 'fhir.read'
    widened = _refresh(client, reg, narrowed.get_json()['refresh_token'],
                       scope='fhir.read fhir.write')
    assert widened.status_code == 400 and widened.get_json()['error'] == 'invalid_scope'


def test_a_resource_other_than_the_chain_audience_is_invalid_target(client):
    reg = _register(client)
    first = _grant_tokens(client, reg)
    resp = _refresh(client, reg, first['refresh_token'],
                    resource='https://mcp.healthclaw.io/mcp')
    assert resp.status_code == 400 and resp.get_json()['error'] == 'invalid_target'


def test_an_expired_refresh_token_is_refused(client, monkeypatch):
    reg = _register(client)
    first = _grant_tokens(client, reg)
    record = oauth._oauth_store_get('refresh-token', first['refresh_token'])
    oauth._oauth_store_set('refresh-token', first['refresh_token'],
                           {**record, 'exp': time.time() - 1}, ttl=60)
    resp = _refresh(client, reg, first['refresh_token'])
    assert resp.status_code == 400 and resp.get_json()['error'] == 'invalid_grant'


def test_a_revoked_consent_ends_the_refresh_chain(client, monkeypatch):
    """§13.4 meets §13.5. MUTATION: drop consent_is_live from the refresh
    grant -> red."""
    reg = _register(client)
    first = _grant_tokens(client, reg)
    record = oauth._oauth_store_get('refresh-token', first['refresh_token'])
    oauth._oauth_store_set('consent', 'consent_x', {
        'tenant_id': 'desktop-demo', 'client_id': reg['client_id'],
        'scopes': record['scopes'], 'granted_at': time.time(),
        'revoked_at': time.time()}, ttl=600)
    oauth._oauth_store_set('refresh-token', first['refresh_token'],
                           {**record, 'consent_id': 'consent_x'}, ttl=600)
    resp = _refresh(client, reg, first['refresh_token'])
    assert resp.status_code == 400 and resp.get_json()['error'] == 'invalid_grant'


def test_revoking_a_refresh_token_ends_its_chain(client):
    reg = _register(client)
    first = _grant_tokens(client, reg)
    second = _refresh(client, reg, first['refresh_token']).get_json()
    assert client.post('/r6/fhir/oauth/revoke',
                       data={'token': second['refresh_token']}).status_code == 200
    resp = _refresh(client, reg, second['refresh_token'])
    assert resp.status_code == 400


def test_a_missing_refresh_token_is_invalid_request(client):
    reg = _register(client)
    resp = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'refresh_token', **_creds(reg)})
    assert resp.status_code == 400 and resp.get_json()['error'] == 'invalid_request'


@pytest.mark.parametrize('kind', ['access', 'consent-request'])
def test_a_token_of_another_kind_is_not_a_refresh_token(client, kind):
    reg = _register(client)
    first = _grant_tokens(client, reg)
    value = first['access_token'] if kind == 'access' else secrets.token_urlsafe(48)
    resp = _refresh(client, reg, value)
    assert resp.status_code == 400 and resp.get_json()['error'] == 'invalid_grant'
