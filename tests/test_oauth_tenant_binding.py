"""H3: OAuth auto-approve must not mint a read bearer for an arbitrary
(non-public) tenant when read-auth is enabled."""
import base64
import hashlib
import json
import secrets


def _register(client, tenant_headers):
    resp = client.post('/r6/fhir/oauth/register',
                       data=json.dumps({
                           'client_name': 'Binding Test',
                           'redirect_uris': ['http://localhost/cb'],
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
    assert resp.status_code == 200
    assert 'code' in resp.get_json()
