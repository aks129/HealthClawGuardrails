"""H3: OAuth auto-approve must not mint a read bearer for an arbitrary
(non-public) tenant when read-auth is enabled."""
import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit


def _code_of(resp):
    """The code rides in the 302 to the registered redirect URI (#568)."""
    assert resp.status_code == 302, resp.get_data(as_text=True)
    return parse_qs(urlsplit(resp.headers['Location']).query)['code'][0]


def _register(client, tenant_headers):
    resp = client.post('/r6/fhir/oauth/register',
                       data=json.dumps({
                           'client_name': 'Binding Test',
                           'redirect_uris': ['http://localhost/cb'],
                           'token_endpoint_auth_method': 'none',
                       }),
                       content_type='application/json',
                       headers=tenant_headers)
    return resp.get_json()['client_id']


def _challenge():
    verifier = secrets.token_urlsafe(32)
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()


def _authorize(client, client_id, tenant_id):
    return client.get(
        f'/r6/fhir/oauth/authorize?client_id={client_id}'
        f'&redirect_uri=http://localhost/cb'
        f'&code_challenge={_challenge()}&code_challenge_method=S256',
        headers={'X-Tenant-Id': tenant_id})


def test_authorize_refuses_nonpublic_tenant_when_read_auth_on(client, monkeypatch):
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    client_id = _register(client, {'X-Tenant-Id': 'desktop-demo'})
    resp = _authorize(client, client_id, 'victim-tenant')
    assert resp.status_code == 403


def test_authorize_allows_public_tenant_when_read_auth_on(client, monkeypatch):
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    client_id = _register(client, {'X-Tenant-Id': 'desktop-demo'})
    resp = _authorize(client, client_id, 'desktop-demo')
    assert _code_of(resp)


# --- What the code is BOUND to, not merely that one was issued ---------------
#
# The two tests above stop at the status code, and the third at `'code' in
# body`. A code exists either way, so the tenant a granted code carries was
# never read back by anything. These do read it back — once out of the token
# the code buys, and once out of the behaviour that token then has.


def _challenge_pair():
    """(verifier, challenge). The helper above discards the verifier, which
    is exactly what a token exchange needs."""
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


def _authorize_claiming(client, client_id, header_tenant, challenge,
                        **extra_query):
    """Authorize as `header_tenant`, smuggling `extra_query` into the URL."""
    query = ''.join(f'&{k}={v}' for k, v in extra_query.items())
    return client.get(
        f'/r6/fhir/oauth/authorize?client_id={client_id}'
        f'&redirect_uri=http://localhost/cb'
        f'&code_challenge={challenge}&code_challenge_method=S256{query}',
        headers={'X-Tenant-Id': header_tenant})


def _exchange(client, client_id, code, verifier):
    resp = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'authorization_code',
        'code': code,
        'code_verifier': verifier,
        'client_id': client_id,
        'redirect_uri': 'http://localhost/cb',
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()['access_token']


def test_the_granted_code_is_bound_to_the_header_tenant_not_a_query_param(
        client, monkeypatch):
    """H3, read back: the tenant on the minted grant is the one the gate saw.

    The gate above checks `X-Tenant-Id` against PUBLIC_TENANTS. If the code
    is then bound from anywhere else — a query parameter, a body field — the
    403 is decorative: pass the gate as the public tenant, receive a grant
    for someone else.

    MUTATION: r6/oauth.py, `'tenant_id': requested_tenant` ->
    `'tenant_id': request.args.get('tenant_id', requested_tenant)` -> red.
    Executed 2026-09-04.
    """
    from r6.oauth import validate_bearer_token
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    client_id = _register(client, {'X-Tenant-Id': 'desktop-demo'})
    verifier, challenge = _challenge_pair()

    resp = _authorize_claiming(client, client_id, 'desktop-demo', challenge,
                               tenant_id='victim-tenant')
    token = _exchange(client, client_id, _code_of(resp), verifier)

    # Destructured, never truthiness-tested — validate_bearer_token returns
    # a tuple whose first element is the answer.
    ok, info = validate_bearer_token(token)
    assert ok, info
    assert info['tenant_id'] == 'desktop-demo', (
        f"the grant is bound to {info['tenant_id']!r}, which is not the "
        "tenant the auto-approve gate authorized")


def test_a_code_granted_to_a_public_tenant_cannot_read_a_private_one(
        client, monkeypatch):
    """The same property stated as behaviour rather than as a stored field.

    Structure can be right and still not matter; this is the read the H3
    escape was worth making. Same mutation, same direction.

    MUTATION: r6/oauth.py, `'tenant_id': requested_tenant` ->
    `'tenant_id': request.args.get('tenant_id', requested_tenant)` -> red.
    Executed 2026-09-04.
    """
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    client_id = _register(client, {'X-Tenant-Id': 'desktop-demo'})
    verifier, challenge = _challenge_pair()

    resp = _authorize_claiming(client, client_id, 'desktop-demo', challenge,
                               tenant_id='victim-tenant')
    token = _exchange(client, client_id, _code_of(resp), verifier)

    read = client.get('/r6/fhir/Patient?_summary=count', headers={
        'X-Tenant-Id': 'victim-tenant',
        'Authorization': f'Bearer {token}',
    })
    assert read.status_code == 401, read.get_data(as_text=True)
    assert read.get_json()['issue'][0]['code'] == 'security'

    # Non-vacuity, and only that: the route answers 200 for the tenant the
    # grant was authorized against, so the 401 above is not a route that
    # refuses everybody. It does NOT show the bearer is live — desktop-demo
    # is in PUBLIC_TENANTS, so authorize_tenant_read returns before it ever
    # looks at the Authorization header. The read-back in the sibling test
    # is what establishes the token is real and bound.
    own = client.get('/r6/fhir/Patient?_summary=count', headers={
        'X-Tenant-Id': 'desktop-demo',
        'Authorization': f'Bearer {token}',
    })
    assert own.status_code == 200
