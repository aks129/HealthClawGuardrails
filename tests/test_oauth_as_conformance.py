"""The authorization server behaves like one (#568, spec §3.3, §3.5, §13).

Phase 2 of the MCP authorization design: a browser can finish the flow, the
token carries the audience it was asked for, a client authenticates the way
it registered, and the FHIR read surface refuses a token minted for any other
audience. Each test names the mutation that turns it red.
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
MCP_RESOURCE = 'https://mcp.healthclaw.io/mcp'


def _pkce():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


def _register(client, redirect_uris=('https://client.example/cb',),
              method=None, **extra):
    body = {'client_name': 'Conformance', 'redirect_uris': list(redirect_uris),
            **extra}
    if method:
        body['token_endpoint_auth_method'] = method
    resp = client.post('/r6/fhir/oauth/register', data=json.dumps(body),
                       content_type='application/json')
    return resp


def _authorize(client, reg, challenge, redirect_uri=None, tenant='desktop-demo',
               **query):
    params = {'client_id': reg['client_id'],
              'redirect_uri': redirect_uri or reg['redirect_uris'][0],
              'code_challenge': challenge, 'code_challenge_method': 'S256',
              'scope': 'fhir.read', **query}
    return client.get('/r6/fhir/oauth/authorize', query_string=params,
                      headers={'X-Tenant-Id': tenant})


def _redirect_params(resp):
    assert resp.status_code == 302, resp.get_data(as_text=True)
    location = resp.headers['Location']
    return location, {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def _token(client, reg, code, verifier, **extra):
    body = {'grant_type': 'authorization_code', 'code': code,
            'code_verifier': verifier, 'client_id': reg['client_id'], **extra}
    if 'client_secret' in reg and 'client_secret' not in extra:
        body['client_secret'] = reg['client_secret']
    return client.post('/r6/fhir/oauth/token', data=body)


# --- the browser can finish the flow (P2-a) ---------------------------------


def test_authorize_answers_302_to_the_registered_redirect_with_code_state_and_iss(client):
    """MUTATION: return the JSON body again -> red. A popup cannot act on JSON."""
    reg = _register(client).get_json()
    verifier, challenge = _pkce()
    resp = _authorize(client, reg, challenge, state='st ate&1')
    location, params = _redirect_params(resp)
    assert location.startswith('https://client.example/cb?')
    assert params['state'] == 'st ate&1', 'state is echoed URL-encoded, verbatim'
    assert params['iss'] == 'http://localhost', 'RFC 9207 names the issuer'
    assert params['code']
    assert resp.headers['Cache-Control'] == 'no-store'
    assert not resp.data.startswith(b'{"'), 'no JSON body carrying the code'


def test_state_is_omitted_when_the_client_sent_none(client):
    reg = _register(client).get_json()
    _, challenge = _pkce()
    _, params = _redirect_params(_authorize(client, reg, challenge))
    assert 'state' not in params and 'iss' in params


def test_the_code_from_the_redirect_buys_a_token(client):
    reg = _register(client).get_json()
    verifier, challenge = _pkce()
    _, params = _redirect_params(_authorize(client, reg, challenge))
    resp = _token(client, reg, params['code'], verifier)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['token_type'] == 'Bearer'


# --- the audience (RFC 8707, P2-b, P2-c) ------------------------------------


def test_a_token_carries_the_fhir_audience_when_no_resource_was_asked_for(client):
    reg = _register(client).get_json()
    verifier, challenge = _pkce()
    _, params = _redirect_params(_authorize(client, reg, challenge))
    token = _token(client, reg, params['code'], verifier).get_json()['access_token']
    ok, info = oauth.validate_bearer_token(token)
    assert ok, info
    assert info['aud'] == FHIR_RESOURCE


def test_an_unknown_resource_is_invalid_target_back_at_the_client(client):
    """MUTATION: record `resource` as sent instead of refusing -> red."""
    reg = _register(client).get_json()
    _, challenge = _pkce()
    resp = _authorize(client, reg, challenge, state='s1',
                      resource='https://somebody-else.example/api')
    location, params = _redirect_params(resp)
    assert params['error'] == 'invalid_target'
    assert params['state'] == 's1' and params['iss'] == 'http://localhost'


def test_the_token_endpoint_refuses_a_resource_other_than_the_code_was_issued_for(client):
    """MUTATION: drop the aud comparison at /oauth/token -> red."""
    reg = _register(client).get_json()
    verifier, challenge = _pkce()
    _, params = _redirect_params(_authorize(client, reg, challenge,
                                            resource=FHIR_RESOURCE))
    resp = _token(client, reg, params['code'], verifier, resource=MCP_RESOURCE)
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'invalid_target'


def test_the_token_endpoint_accepts_the_same_resource_and_inherits_when_absent(client):
    reg = _register(client).get_json()
    for sent in (FHIR_RESOURCE, None):
        verifier, challenge = _pkce()
        _, params = _redirect_params(_authorize(client, reg, challenge,
                                                resource=FHIR_RESOURCE))
        extra = {'resource': sent} if sent else {}
        resp = _token(client, reg, params['code'], verifier, **extra)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        ok, info = oauth.validate_bearer_token(resp.get_json()['access_token'])
        assert ok and info['aud'] == FHIR_RESOURCE


def test_the_mcp_resource_binds_the_configured_demo_tenant_and_ignores_the_header(
        client, monkeypatch):
    """P2-b: a browser flow has no trusted place to put a tenant.

    MUTATION: bind `request.headers.get('X-Tenant-Id')` on the demo policy
    -> red (the token lands on victim-tenant).
    """
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    monkeypatch.setenv('MCP_CANONICAL_RESOURCE', MCP_RESOURCE)
    monkeypatch.setenv('MCP_OAUTH_DEMO_TENANT', 'desktop-demo')
    reg = _register(client).get_json()
    verifier, challenge = _pkce()
    _, params = _redirect_params(_authorize(client, reg, challenge,
                                            tenant='victim-tenant',
                                            resource=MCP_RESOURCE))
    token = _token(client, reg, params['code'], verifier).get_json()['access_token']
    ok, info = oauth.validate_bearer_token(token)
    assert ok, info
    assert info['tenant_id'] == 'desktop-demo'
    assert info['aud'] == MCP_RESOURCE


def test_the_mcp_resource_fails_closed_when_the_demo_tenant_is_not_public(
        client, monkeypatch):
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    monkeypatch.setenv('MCP_CANONICAL_RESOURCE', MCP_RESOURCE)
    monkeypatch.setenv('MCP_OAUTH_DEMO_TENANT', 'private-tenant')
    reg = _register(client).get_json()
    _, challenge = _pkce()
    resp = _authorize(client, reg, challenge, resource=MCP_RESOURCE)
    assert resp.status_code == 403
    assert resp.get_json()['error'] == 'access_denied'


def test_without_the_mcp_resource_configured_that_audience_is_unknown(client):
    reg = _register(client).get_json()
    _, challenge = _pkce()
    _, params = _redirect_params(_authorize(client, reg, challenge,
                                            resource=MCP_RESOURCE))
    assert params['error'] == 'invalid_target'


# --- R3 in reverse: the FHIR surface refuses a token for another audience ---


def _store_token(tenant_id, aud, scopes=('fhir.read',)):
    token = secrets.token_urlsafe(48)
    oauth._oauth_store_set('access-token', token, {
        'client_id': 'test', 'scopes': list(scopes), 'tenant_id': tenant_id,
        'aud': aud, 'exp': time.time() + 300}, ttl=300)
    return token


@pytest.mark.parametrize('aud', [MCP_RESOURCE, None, FHIR_RESOURCE + '/',
                                 'https://localhost/r6/fhir'])
def test_a_read_bearer_minted_for_another_audience_is_refused(
        client, monkeypatch, aud):
    """Spec §9.2 / §8.2 R3, at the surface that would otherwise accept it.

    MUTATION: delete the `aud` comparison in r6/read_auth._oauth_authorizes
    -> red for every row.
    """
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    token = _store_token('private-tenant', aud)
    resp = client.get('/r6/fhir/Patient?_summary=count', headers={
        'X-Tenant-Id': 'private-tenant', 'Authorization': f'Bearer {token}'})
    assert resp.status_code == 401, resp.get_data(as_text=True)


def test_a_read_bearer_minted_for_this_surface_is_accepted(client, monkeypatch):
    """Non-vacuity for the row above: same record, right audience, 200."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    token = _store_token('private-tenant', FHIR_RESOURCE)
    resp = client.get('/r6/fhir/Patient?_summary=count', headers={
        'X-Tenant-Id': 'private-tenant', 'Authorization': f'Bearer {token}'})
    assert resp.status_code == 200, resp.get_data(as_text=True)


# --- registration (RFC 7591, P2-d) ------------------------------------------


def test_a_public_client_gets_no_secret_and_its_method_is_honoured(client):
    """MUTATION: always answer client_secret_post -> red."""
    reg = _register(client, method='none').get_json()
    assert reg['token_endpoint_auth_method'] == 'none'
    assert 'client_secret' not in reg
    assert 'client_secret_expires_at' not in reg


def test_a_confidential_client_is_told_when_its_secret_expires(client):
    reg = _register(client).get_json()
    assert reg['token_endpoint_auth_method'] == 'client_secret_post'
    assert reg['client_secret_expires_at'] == (
        reg['client_id_issued_at'] + oauth.CLIENT_TTL_SECONDS)


@pytest.mark.parametrize('uri', [
    'http://client.example/cb',            # plain http off the loopback
    'https://client.example/cb#frag',      # fragment
    'javascript:alert(1)',
    'client.example/cb',
    '',
])
def test_a_redirect_uri_that_is_neither_https_nor_loopback_is_refused(client, uri):
    """MUTATION: store redirect_uris unvalidated -> red."""
    resp = _register(client, redirect_uris=(uri,))
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()['error'] == 'invalid_redirect_uri'


@pytest.mark.parametrize('uri', [
    'https://claude.ai/api/mcp/auth_callback',
    'http://localhost/callback',
    'http://127.0.0.1:53211/callback',
    'http://[::1]:8080/cb',
])
def test_https_and_loopback_redirect_uris_register(client, uri):
    resp = _register(client, redirect_uris=(uri,))
    assert resp.status_code == 201, resp.get_data(as_text=True)


def test_an_unknown_auth_method_is_refused(client):
    resp = _register(client, method='private_key_jwt')
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'invalid_client_metadata'


def test_a_loopback_redirect_matches_on_any_port_and_nothing_else_does(client):
    """RFC 8252 §7.3: Claude Code listens on an ephemeral port.

    MUTATION: exact-match every redirect -> the first assertion goes red;
    ignore the port everywhere -> the second does.
    """
    reg = _register(client, redirect_uris=('http://127.0.0.1/callback',
                                           'https://client.example/cb')).get_json()
    _, challenge = _pkce()
    resp = _authorize(client, reg, challenge,
                      redirect_uri='http://127.0.0.1:53211/callback')
    location, _ = _redirect_params(resp)
    assert location.startswith('http://127.0.0.1:53211/callback?')

    resp = _authorize(client, reg, challenge,
                      redirect_uri='https://client.example:8443/cb')
    assert resp.status_code == 400


# --- client authentication at the token endpoint ----------------------------


def test_a_public_client_must_send_its_client_id(client):
    """RFC 6749 §4.1.3. MUTATION: drop the requirement -> red."""
    reg = _register(client, method='none').get_json()
    verifier, challenge = _pkce()
    _, params = _redirect_params(_authorize(client, reg, challenge))
    resp = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'authorization_code', 'code': params['code'],
        'code_verifier': verifier})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'invalid_request'


def test_a_confidential_client_must_present_the_secret_it_was_issued(client):
    """MUTATION: skip the compare_digest -> the wrong-secret row goes green."""
    reg = _register(client).get_json()
    for secret, status in ((None, 401), ('not-the-secret', 401)):
        verifier, challenge = _pkce()
        _, params = _redirect_params(_authorize(client, reg, challenge))
        body = {'grant_type': 'authorization_code', 'code': params['code'],
                'code_verifier': verifier, 'client_id': reg['client_id']}
        if secret:
            body['client_secret'] = secret
        resp = client.post('/r6/fhir/oauth/token', data=body)
        assert resp.status_code == status, resp.get_data(as_text=True)
        assert resp.get_json()['error'] == 'invalid_client'


def test_a_confidential_client_may_authenticate_with_http_basic(client):
    reg = _register(client).get_json()
    verifier, challenge = _pkce()
    _, params = _redirect_params(_authorize(client, reg, challenge))
    basic = base64.b64encode(
        f"{reg['client_id']}:{reg['client_secret']}".encode()).decode()
    resp = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'authorization_code', 'code': params['code'],
        'code_verifier': verifier}, headers={'Authorization': f'Basic {basic}'})
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_a_failed_exchange_burns_the_code(client):
    reg = _register(client).get_json()
    verifier, challenge = _pkce()
    _, params = _redirect_params(_authorize(client, reg, challenge))
    assert _token(client, reg, params['code'], verifier,
                  client_secret='wrong').status_code == 401
    assert _token(client, reg, params['code'], verifier).status_code == 400


# --- discovery at the issuer root (spec §3.3) -------------------------------


def test_the_metadata_is_served_at_the_issuer_root_and_matches_the_prefixed_copy(client):
    """RFC 8414 §3: a path-less issuer's clients look at the root first.

    MUTATION: remove the root rule from main._register_blueprints -> red.
    """
    root = client.get('/.well-known/oauth-authorization-server')
    prefixed = client.get('/r6/fhir/.well-known/oauth-authorization-server')
    assert root.status_code == 200 and prefixed.status_code == 200
    assert root.get_json() == prefixed.get_json()
    doc = root.get_json()
    assert doc['issuer'] == 'http://localhost'
    assert doc['authorization_response_iss_parameter_supported'] is True
    assert set(doc['token_endpoint_auth_methods_supported']) == {
        'none', 'client_secret_post', 'client_secret_basic'}


def test_oauth_issuer_pins_every_published_endpoint(client, monkeypatch):
    """Behind a proxy that loses the scheme, the configured issuer wins over
    the request (spec §3.3 change 1). The audience follows it too."""
    monkeypatch.setenv('OAUTH_ISSUER', 'https://app.healthclaw.io/')
    doc = client.get('/.well-known/oauth-authorization-server').get_json()
    assert doc['issuer'] == 'https://app.healthclaw.io'
    for key in ('authorization_endpoint', 'token_endpoint',
                'registration_endpoint', 'revocation_endpoint'):
        assert doc[key].startswith('https://app.healthclaw.io/r6/fhir/oauth/')
    smart = client.get('/r6/fhir/.well-known/smart-configuration').get_json()
    assert smart['token_endpoint'] == 'https://app.healthclaw.io/r6/fhir/oauth/token'
    with client.application.test_request_context('/'):
        assert oauth.fhir_resource() == 'https://app.healthclaw.io/r6/fhir'
