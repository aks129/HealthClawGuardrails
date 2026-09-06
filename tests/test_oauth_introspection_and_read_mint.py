"""Introspection is protected and truthful; the internal mint can be read-only.

Spec §3.5 item 3 and amendment P2-e (the MCP server asks Flask whether a token
is live), and §13.6 (the MCP server's downstream credential on the OAuth path
is a read-scoped step-up token, minted with the scope it consented to and no
more). Each test names the mutation that turns it red.
"""
import base64
import json
import secrets
import time

import pytest

from r6 import oauth

FHIR_RESOURCE = 'http://localhost/r6/fhir'
INTROSPECT = '/r6/fhir/oauth/introspect'


@pytest.fixture
def introspection_client(monkeypatch):
    monkeypatch.setenv('MCP_INTROSPECTION_CLIENT_ID', 'mcp-server')
    monkeypatch.setenv('MCP_INTROSPECTION_CLIENT_SECRET', 'introspection-secret')
    return {'client_id': 'mcp-server', 'client_secret': 'introspection-secret'}


def _store_token(tenant_id='private-tenant', aud=FHIR_RESOURCE, exp_in=300,
                 scopes=('fhir.read', 'context.read')):
    token = secrets.token_urlsafe(48)
    oauth._oauth_store_set('access-token', token, {
        'client_id': 'claude', 'scopes': list(scopes), 'tenant_id': tenant_id,
        'aud': aud, 'exp': time.time() + exp_in}, ttl=max(1, exp_in))
    return token


# --- the endpoint is protected (RFC 7662 §2.1) -------------------------------


def test_unconfigured_credentials_authorize_nobody(client, monkeypatch):
    """MUTATION: return True when the variables are unset -> red.
    An open introspection endpoint is an oracle over captured tokens."""
    monkeypatch.delenv('MCP_INTROSPECTION_CLIENT_ID', raising=False)
    monkeypatch.delenv('MCP_INTROSPECTION_CLIENT_SECRET', raising=False)
    token = _store_token()
    resp = client.post(INTROSPECT, data={'token': token, 'client_id': '',
                                         'client_secret': ''})
    assert resp.status_code == 401
    assert 'active' not in (resp.get_json() or {})


@pytest.mark.parametrize('creds', [
    {},
    {'client_id': 'mcp-server'},
    {'client_id': 'mcp-server', 'client_secret': 'wrong'},
    {'client_id': 'someone-else', 'client_secret': 'introspection-secret'},
])
def test_a_caller_without_the_registered_credential_gets_401_and_a_challenge(
        client, introspection_client, creds):
    """MUTATION: skip introspection_client_authorized -> red for every row."""
    token = _store_token()
    resp = client.post(INTROSPECT, data={'token': token, **creds})
    assert resp.status_code == 401, resp.get_data(as_text=True)
    assert resp.headers['WWW-Authenticate'].startswith('Basic')
    assert resp.get_json() == {'error': 'invalid_client'}


def test_the_credential_is_accepted_in_the_body_and_as_http_basic(
        client, introspection_client):
    token = _store_token()
    in_body = client.post(INTROSPECT, data={'token': token, **introspection_client})
    assert in_body.status_code == 200 and in_body.get_json()['active'] is True

    basic = base64.b64encode(b'mcp-server:introspection-secret').decode()
    as_basic = client.post(INTROSPECT, data={'token': token},
                           headers={'Authorization': f'Basic {basic}'})
    assert as_basic.status_code == 200 and as_basic.get_json()['active'] is True


# --- the answer is truthful and says no more than it must -------------------


def test_a_live_token_answers_active_with_audience_tenant_scope_and_expiry(
        client, introspection_client):
    token = _store_token(tenant_id='private-tenant', aud='https://mcp.healthclaw.io/mcp')
    resp = client.post(INTROSPECT, data={'token': token, **introspection_client})
    body = resp.get_json()
    assert body['active'] is True
    assert body['aud'] == 'https://mcp.healthclaw.io/mcp'
    assert body['tenant_id'] == 'private-tenant'
    assert body['scope'] == 'fhir.read context.read'
    assert body['client_id'] == 'claude'
    assert body['exp'] > time.time()
    assert resp.headers['Cache-Control'] == 'no-store'
    assert token not in resp.get_data(as_text=True), 'the token is never echoed'


@pytest.mark.parametrize('case', ['unknown', 'expired', 'revoked', 'missing'])
def test_anything_not_live_answers_exactly_active_false(
        client, introspection_client, case):
    """One shape for every failure: the caller cannot tell unknown from
    expired from revoked, and there is no error field to learn from."""
    if case == 'unknown':
        token = secrets.token_urlsafe(48)
    elif case == 'expired':
        token = _store_token(exp_in=-5)
    elif case == 'revoked':
        token = _store_token()
        client.post('/r6/fhir/oauth/revoke', data={'token': token})
    else:
        token = ''
    resp = client.post(INTROSPECT, data={'token': token, **introspection_client})
    assert resp.status_code == 200
    assert resp.get_json() == {'active': False}


def test_discovery_advertises_the_introspection_endpoint(client):
    doc = client.get('/.well-known/oauth-authorization-server').get_json()
    assert doc['introspection_endpoint'] == 'http://localhost' + INTROSPECT
    assert 'client_secret_basic' in doc['introspection_endpoint_auth_methods_supported']


# --- the read-only mint (§13.6) ---------------------------------------------


def _mint(client, scope, tenant='private-tenant'):
    body = {'tenant_id': tenant}
    if scope is not None:
        body['scope'] = scope
    return client.post('/r6/fhir/internal/step-up-token', json=body)


def test_a_read_scoped_mint_reads_and_never_writes(client, monkeypatch,
                                                   sample_observation):
    """H4 at the mint: the MCP server's OAuth path asks for `read` and gets a
    token the read gate accepts and every write path refuses.

    MUTATION: mint `generate_step_up_token(tenant_id)` ignoring scope -> the
    write below succeeds and the test goes red.
    """
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    minted = _mint(client, 'read')
    assert minted.status_code == 200, minted.get_data(as_text=True)
    assert minted.get_json()['scope'] == 'read'
    token = minted.get_json()['token']
    headers = {'X-Tenant-Id': 'private-tenant', 'X-Step-Up-Token': token}

    read = client.get('/r6/fhir/Patient?_summary=count', headers=headers)
    assert read.status_code == 200, read.get_data(as_text=True)

    write = client.post('/r6/fhir/Observation', data=json.dumps(sample_observation),
                        content_type='application/json',
                        headers={**headers, 'X-Human-Confirmed': 'true'})
    assert write.status_code in (401, 403), write.get_data(as_text=True)
    assert write.get_json()['resourceType'] == 'OperationOutcome'


def test_an_unscoped_mint_is_unchanged_and_still_writes(client, monkeypatch,
                                                        sample_observation):
    """Non-vacuity for the refusal above: the same write, a full token, lands."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    minted = _mint(client, None)
    assert minted.status_code == 200 and minted.get_json()['scope'] is None
    headers = {'X-Tenant-Id': 'private-tenant',
               'X-Step-Up-Token': minted.get_json()['token'],
               'X-Human-Confirmed': 'true'}
    write = client.post('/r6/fhir/Observation', data=json.dumps(sample_observation),
                        content_type='application/json', headers=headers)
    assert write.status_code == 201, write.get_data(as_text=True)


@pytest.mark.parametrize('scope', ['write', 'admin', 'READ', 'read write', ''])
def test_any_scope_other_than_read_is_refused_at_the_mint(client, scope):
    """MUTATION: accept any scope string -> `write` mints and the row goes red.
    The grammar is two values; a third would be a new capability nobody named."""
    resp = _mint(client, scope)
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_the_scope_check_does_not_soften_the_mint_gate(client, monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', 'top-secret')
    resp = _mint(client, 'read')
    assert resp.status_code == 403
