"""The consent handoff: HealthClaw parks, CareAgents decides, HealthClaw binds.

Spec §13.3 and §13.4. Flask issues nothing for a real tenant until a signed,
single-use, expiring decision comes back from the consent surface; the
approval is audited under the tenant before a code exists; a consent can be
taken back and every token under it dies with it. The grant builder below is
the reference CareAgents implements. Each test names its mutation.
"""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from r6 import oauth

MCP_RESOURCE = 'https://mcp.healthclaw.io/mcp'
CONSENT_URL = 'https://careagents.example/authorize'
SECRET = 'mint-secret-shared-with-careagents'


@pytest.fixture
def handoff(monkeypatch):
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    monkeypatch.setenv('MCP_CANONICAL_RESOURCE', MCP_RESOURCE)
    monkeypatch.setenv('CAREAGENTS_CONSENT_URL', CONSENT_URL)
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    monkeypatch.setenv('MCP_INTROSPECTION_CLIENT_ID', 'mcp-server')
    monkeypatch.setenv('MCP_INTROSPECTION_CLIENT_SECRET', 'introspection-secret')
    from r6.stepup import clear_nonce_cache
    clear_nonce_cache()


def _pkce():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


def _register(client):
    return client.post('/r6/fhir/oauth/register', data=json.dumps({
        'client_name': 'Claude', 'token_endpoint_auth_method': 'none',
        'redirect_uris': ['https://claude.ai/api/mcp/auth_callback']}),
        content_type='application/json').get_json()


def _authorize(client, reg, challenge, state='s-1', tenant='victim-tenant'):
    return client.get('/r6/fhir/oauth/authorize', query_string={
        'client_id': reg['client_id'], 'redirect_uri': reg['redirect_uris'][0],
        'code_challenge': challenge, 'code_challenge_method': 'S256',
        'scope': 'fhir.read context.read', 'state': state,
        'resource': MCP_RESOURCE}, headers={'X-Tenant-Id': tenant})


def _params(resp):
    assert resp.status_code == 302, resp.get_data(as_text=True)
    location = resp.headers['Location']
    return location, {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def _key(secret=SECRET):
    return hashlib.sha256(b'healthclaw-consent-handoff:' + secret.encode()).digest()


def _grant(request_id, tenant_id='private-tenant', decision='approved',
           secret=SECRET, exp=None, nonce=None, consent_id=None, **overrides):
    """What CareAgents sends back. Built here from the shared secret alone."""
    import hmac
    payload = {'request_id': request_id, 'tenant_id': tenant_id,
               'consent_id': (f'consent_{secrets.token_hex(8)}'
                              if consent_id is None else consent_id),
               'nonce': secrets.token_hex(16) if nonce is None else nonce,
               'exp': exp or int(time.time()) + 300, 'decision': decision}
    payload.update(overrides)
    body = base64.urlsafe_b64encode(json.dumps(
        payload, sort_keys=True, separators=(',', ':')).encode()).rstrip(b'=').decode()
    tag = hmac.new(_key(secret), body.encode(), hashlib.sha256).hexdigest()
    return f'{body}.{tag}', payload


def _park(client, reg, challenge, state='s-1'):
    """Authorize up to the consent redirect; return the parked request id."""
    location, params = _params(_authorize(client, reg, challenge, state))
    assert location.startswith(CONSENT_URL + '?')
    request_id, exp, tag = params['req'].split('.')
    return request_id, int(exp), tag


def _return(client, grant):
    # The browser carries no tenant, but a hostile caller could add one; the
    # header is sent on every return so a binding that reads it turns red.
    return client.get('/r6/fhir/oauth/consent/return', query_string={'grant': grant},
                      headers={'X-Tenant-Id': 'victim-tenant'})


def _service(headers=None):
    return {'X-Internal-Secret': SECRET, **(headers or {})}


# --- outbound ---------------------------------------------------------------


def test_the_mcp_audience_sends_the_browser_to_the_consent_surface(client, handoff):
    """MUTATION: keep the `demo` policy when CAREAGENTS_CONSENT_URL is set -> red."""
    import hmac
    reg = _register(client)
    _, challenge = _pkce()
    request_id, exp, tag = _park(client, reg, challenge)
    assert exp > time.time()
    assert tag == hmac.new(_key(), f'{request_id}.{exp}'.encode(), hashlib.sha256).hexdigest()


def test_the_consent_page_reads_the_parked_request_with_the_service_secret_only(
        client, handoff):
    """MUTATION: drop internal_secret_authorized from consent_request -> red."""
    reg = _register(client)
    _, challenge = _pkce()
    request_id, _, _ = _park(client, reg, challenge)
    bare = client.get(f'/r6/fhir/oauth/consent/{request_id}')
    assert bare.status_code == 403
    wrong = client.get(f'/r6/fhir/oauth/consent/{request_id}',
                       headers={'X-Internal-Secret': 'nope'})
    assert wrong.status_code == 403
    shown = client.get(f'/r6/fhir/oauth/consent/{request_id}', headers=_service())
    assert shown.status_code == 200
    body = shown.get_json()
    assert body['client_name'] == 'Claude'
    assert body['scopes'] == ['fhir.read', 'context.read']
    assert 'redirect_uri' not in body and 'code_challenge' not in body
    missing = client.get('/r6/fhir/oauth/consent/no-such-request', headers=_service())
    assert missing.status_code == 404


def test_without_the_shared_secret_the_handoff_fails_closed_at_the_client(
        client, handoff, monkeypatch):
    monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET')
    reg = _register(client)
    _, challenge = _pkce()
    location, params = _params(_authorize(client, reg, challenge))
    assert location.startswith('https://claude.ai/api/mcp/auth_callback?')
    assert params['error'] == 'temporarily_unavailable' and 'code' not in params


# --- inbound: the approval ---------------------------------------------------


def test_an_approved_grant_binds_the_code_to_the_tenant_the_person_chose(
        client, handoff):
    """The whole path: park, decide, return, exchange, introspect.

    MUTATION: bind `request.headers.get('X-Tenant-Id')` instead of the
    grant's tenant at consent_return -> red (the header said victim-tenant).
    """
    reg = _register(client)
    verifier, challenge = _pkce()
    request_id, _, _ = _park(client, reg, challenge, state='st-9')
    grant, payload = _grant(request_id, tenant_id='private-tenant')
    location, params = _params(_return(client, grant))
    assert location.startswith('https://claude.ai/api/mcp/auth_callback?')
    assert params['state'] == 'st-9' and params['iss'] == 'http://localhost'

    token_resp = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'authorization_code', 'code': params['code'],
        'code_verifier': verifier, 'client_id': reg['client_id'],
        'resource': MCP_RESOURCE})
    assert token_resp.status_code == 200, token_resp.get_data(as_text=True)
    token = token_resp.get_json()['access_token']
    ok, info = oauth.validate_bearer_token(token)
    assert ok, info
    assert info['tenant_id'] == 'private-tenant'
    assert info['aud'] == MCP_RESOURCE
    assert info['consent_id'] == payload['consent_id']

    introspected = client.post('/r6/fhir/oauth/introspect', data={
        'token': token, 'client_id': 'mcp-server',
        'client_secret': 'introspection-secret'}).get_json()
    assert introspected['active'] is True
    assert introspected['tenant_id'] == 'private-tenant'


def test_the_approval_is_audited_under_the_tenant_before_any_code_exists(
        client, handoff):
    """§13.4. MUTATION: delete the add_audit_event call -> red."""
    from r6.models import AuditEventRecord
    reg = _register(client)
    _, challenge = _pkce()
    request_id, _, _ = _park(client, reg, challenge)
    grant, payload = _grant(request_id, tenant_id='private-tenant')
    _params(_return(client, grant))
    with client.application.app_context():
        rows = AuditEventRecord.query.filter_by(
            tenant_id='private-tenant', resource_type='Consent').all()
    assert len(rows) == 1
    row = rows[0]
    assert row.resource_id == payload['consent_id']
    assert row.event_type == 'create'
    assert f"client_id={reg['client_id']}" in row.detail
    assert 'scopes=fhir.read context.read' in row.detail
    assert 'via=careagents' in row.detail


# --- inbound: refusals -------------------------------------------------------


def test_a_denial_goes_back_to_the_client_as_access_denied_and_spends_the_request(
        client, handoff):
    reg = _register(client)
    _, challenge = _pkce()
    request_id, _, _ = _park(client, reg, challenge, state='st-2')
    grant, _ = _grant(request_id, decision='denied')
    location, params = _params(_return(client, grant))
    assert params['error'] == 'access_denied' and params['state'] == 'st-2'
    assert 'code' not in params
    again, _ = _grant(request_id)
    assert _return(client, again).status_code == 400, 'the parked request was popped'


@pytest.mark.parametrize('case', ['forged key', 'tampered body', 'expired',
                                  'no nonce', 'no request', 'bad tenant',
                                  'no consent id'])
def test_a_grant_that_does_not_verify_is_a_plain_400(client, handoff, case):
    """MUTATION: skip the tag comparison in decode_grant -> the forged-key and
    tampered-body rows go green; skip the exp check -> the expired row does."""
    reg = _register(client)
    _, challenge = _pkce()
    request_id, _, _ = _park(client, reg, challenge)
    if case == 'forged key':
        grant, _ = _grant(request_id, secret='guessed')
    elif case == 'tampered body':
        good, payload = _grant(request_id, tenant_id='desktop-demo')
        body, tag = good.split('.')
        payload['tenant_id'] = 'private-tenant'
        body = base64.urlsafe_b64encode(json.dumps(
            payload, sort_keys=True, separators=(',', ':')).encode()).rstrip(b'=').decode()
        grant = f'{body}.{tag}'
    elif case == 'expired':
        grant, _ = _grant(request_id, exp=int(time.time()) - 1)
    elif case == 'no nonce':
        grant, _ = _grant(request_id, nonce='')
    elif case == 'no request':
        grant, _ = _grant('never-parked')
    elif case == 'bad tenant':
        grant, _ = _grant(request_id, tenant_id='../etc; drop')
    else:
        grant, _ = _grant(request_id, consent_id='')
    resp = _return(client, grant)
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json() == {'error': 'invalid_request'}
    if case != 'no request':
        assert resp.headers.get('Location') is None


def test_a_nonce_is_never_honoured_twice(client, handoff):
    """Two different parked requests, two grants sharing one nonce: the second
    is refused before its parked request is even looked at.

    MUTATION: drop mark_nonce_used from consent_return -> red.
    """
    reg = _register(client)
    _, c1 = _pkce()
    _, c2 = _pkce()
    first_id, _, _ = _park(client, reg, c1)
    second_id, _, _ = _park(client, reg, c2)
    nonce = secrets.token_hex(16)
    first, _ = _grant(first_id, nonce=nonce)
    assert _return(client, first).status_code == 302
    second, _ = _grant(second_id, nonce=nonce)
    assert _return(client, second).status_code == 400
    fresh, _ = _grant(second_id)
    assert _return(client, fresh).status_code == 302, 'the request itself was untouched'


def test_a_parked_request_is_spent_by_the_first_decision(client, handoff):
    """MUTATION: `_oauth_store_get` instead of `_oauth_store_pop` -> red."""
    reg = _register(client)
    _, challenge = _pkce()
    request_id, _, _ = _park(client, reg, challenge)
    first, _ = _grant(request_id)
    assert _return(client, first).status_code == 302
    second, _ = _grant(request_id)
    assert _return(client, second).status_code == 400


# --- revocation ---------------------------------------------------------------


def _consented_token(client, reg):
    verifier, challenge = _pkce()
    request_id, _, _ = _park(client, reg, challenge)
    grant, payload = _grant(request_id, tenant_id='private-tenant')
    _, params = _params(_return(client, grant))
    token = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'authorization_code', 'code': params['code'],
        'code_verifier': verifier, 'client_id': reg['client_id']}).get_json()['access_token']
    return token, payload['consent_id']


def test_revoking_the_consent_kills_every_token_under_it(client, handoff):
    """MUTATION: drop consent_is_live from validate_bearer_token -> red."""
    reg = _register(client)
    token, consent_id = _consented_token(client, reg)
    ok, _ = oauth.validate_bearer_token(token)
    assert ok
    resp = client.post(f'/r6/fhir/oauth/consent/{consent_id}/revoke', headers=_service())
    assert resp.status_code == 200 and resp.get_json()['revoked'] is True
    ok, why = oauth.validate_bearer_token(token)
    assert not ok and why == 'Consent revoked'
    introspected = client.post('/r6/fhir/oauth/introspect', data={
        'token': token, 'client_id': 'mcp-server',
        'client_secret': 'introspection-secret'}).get_json()
    assert introspected == {'active': False}
    again = client.post(f'/r6/fhir/oauth/consent/{consent_id}/revoke', headers=_service())
    assert again.status_code == 200, 'idempotent'


def test_revocation_needs_the_service_secret_and_knows_only_real_consents(
        client, handoff):
    reg = _register(client)
    _, consent_id = _consented_token(client, reg)
    assert client.post(f'/r6/fhir/oauth/consent/{consent_id}/revoke').status_code == 403
    assert client.post('/r6/fhir/oauth/consent/nope/revoke',
                       headers=_service()).status_code == 404


def test_tokens_from_the_auto_approve_paths_carry_no_consent_and_still_validate(
        client, handoff, monkeypatch):
    """The FHIR audience's header-bound path is unchanged by this feature."""
    monkeypatch.delenv('CAREAGENTS_CONSENT_URL')
    reg = _register(client)
    verifier, challenge = _pkce()
    resp = client.get('/r6/fhir/oauth/authorize', query_string={
        'client_id': reg['client_id'], 'redirect_uri': reg['redirect_uris'][0],
        'code_challenge': challenge, 'code_challenge_method': 'S256',
        'scope': 'fhir.read'}, headers={'X-Tenant-Id': 'desktop-demo'})
    _, params = _params(resp)
    token = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'authorization_code', 'code': params['code'],
        'code_verifier': verifier, 'client_id': reg['client_id']}).get_json()['access_token']
    ok, info = oauth.validate_bearer_token(token)
    assert ok and info['consent_id'] is None and info['tenant_id'] == 'desktop-demo'
