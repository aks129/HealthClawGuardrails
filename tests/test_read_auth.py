"""
Tests for flag-gated read authentication and the token-mint oracle lock.

The vulnerability: FHIR reads were gated only by the client-supplied
X-Tenant-Id header — no authentication. With READ_AUTH_ENABLED on, reads of
non-public tenants must present a step-up token bound to that tenant.

Default (flag off) must be a strict no-op: header-only reads keep working,
so deploying this code changes nothing until the flag is flipped.
"""

import pytest

from r6.stepup import generate_step_up_token


# A search GET returns a 200 searchset bundle even with an empty store, so it
# exercises the read path without needing seeded data.
READ_PATH = '/r6/fhir/Patient?_summary=count'


@pytest.fixture(autouse=True)
def _clean_read_auth_env(monkeypatch):
    """Each test starts with the read-auth flag and mint secret cleared.

    conftest sets PUBLIC_TENANTS in os.environ; tests that care about it set
    it explicitly via monkeypatch.
    """
    monkeypatch.delenv('READ_AUTH_ENABLED', raising=False)
    monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET', raising=False)


# --- Flag OFF: behavior preserved (default no-op) ---

def test_read_flag_off_header_only_succeeds(client):
    """Flag off (default): header-only read returns 200 — unchanged."""
    resp = client.get(READ_PATH, headers={'X-Tenant-Id': 'private-tenant'})
    assert resp.status_code == 200


def test_read_flag_off_public_tenants_irrelevant(client, monkeypatch):
    """Flag off: even a non-public tenant reads fine without a token."""
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    resp = client.get(READ_PATH, headers={'X-Tenant-Id': 'some-private-tenant'})
    assert resp.status_code == 200


# --- Flag ON ---

def test_read_flag_on_non_public_no_token_401(client, monkeypatch):
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    resp = client.get(READ_PATH, headers={'X-Tenant-Id': 'private-tenant'})
    assert resp.status_code == 401
    body = resp.get_json()
    assert body['resourceType'] == 'OperationOutcome'
    assert body['issue'][0]['code'] == 'security'
    assert 'requires authentication' in body['issue'][0]['diagnostics']


def test_read_flag_on_non_public_valid_token_200(client, monkeypatch):
    monkeypatch.setenv('READ_AUTH_ENABLED', '1')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    token = generate_step_up_token('private-tenant')
    resp = client.get(READ_PATH, headers={
        'X-Tenant-Id': 'private-tenant',
        'X-Step-Up-Token': token,
    })
    assert resp.status_code == 200


def test_read_flag_on_bearer_alias_200(client, monkeypatch):
    """Authorization: Bearer <token> is accepted as a token alias."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'yes')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    token = generate_step_up_token('private-tenant')
    resp = client.get(READ_PATH, headers={
        'X-Tenant-Id': 'private-tenant',
        'Authorization': f'Bearer {token}',
    })
    assert resp.status_code == 200


def test_read_flag_on_token_wrong_tenant_401(client, monkeypatch):
    """A valid token bound to a DIFFERENT tenant must be rejected."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    token = generate_step_up_token('other-tenant')
    resp = client.get(READ_PATH, headers={
        'X-Tenant-Id': 'private-tenant',
        'X-Step-Up-Token': token,
    })
    assert resp.status_code == 401
    assert resp.get_json()['issue'][0]['code'] == 'security'


def test_read_flag_on_public_tenant_no_token_200(client, monkeypatch):
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo,winters-demo')
    resp = client.get(READ_PATH, headers={'X-Tenant-Id': 'desktop-demo'})
    assert resp.status_code == 200


def test_read_flag_on_discovery_metadata_no_token_200(client, monkeypatch):
    """Discovery endpoint reads need no auth even with the flag on."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    resp = client.get('/r6/fhir/metadata')
    assert resp.status_code == 200


# --- Writes are independent of the read flag ---

def test_write_requires_step_up_flag_off(client, monkeypatch, sample_patient):
    monkeypatch.delenv('READ_AUTH_ENABLED', raising=False)
    resp = client.post('/r6/fhir/Patient',
                       json=sample_patient,
                       headers={'X-Tenant-Id': 'private-tenant'})
    assert resp.status_code == 401


def test_write_requires_step_up_flag_on(client, monkeypatch, sample_patient):
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    resp = client.post('/r6/fhir/Patient',
                       json=sample_patient,
                       headers={'X-Tenant-Id': 'private-tenant'})
    assert resp.status_code == 401


# --- Token-mint oracle lock ---

def test_mint_unset_secret_open(client, monkeypatch):
    """Unset INTERNAL_TOKEN_MINT_SECRET → open (backward-compatible)."""
    monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET', raising=False)
    resp = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': 'private-tenant'})
    assert resp.status_code == 200
    assert 'token' in resp.get_json()


def test_mint_secret_set_missing_header_403(client, monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', 'top-secret')
    resp = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': 'private-tenant'})
    assert resp.status_code == 403


def test_mint_secret_set_wrong_header_403(client, monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', 'top-secret')
    resp = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': 'private-tenant'},
                       headers={'X-Internal-Secret': 'wrong'})
    assert resp.status_code == 403


def test_mint_secret_set_correct_header_200(client, monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', 'top-secret')
    resp = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': 'private-tenant'},
                       headers={'X-Internal-Secret': 'top-secret'})
    assert resp.status_code == 200
    assert 'token' in resp.get_json()


def test_mint_public_tenant_open_even_with_secret(client, monkeypatch):
    """Public/synthetic tenants stay open with NO secret even when the mint
    secret is set — so the browser demo dashboard + telemetry (which mint
    desktop-demo tokens and can't hold a secret) keep working. A public-tenant
    token only unlocks public data, which read-auth lets through anyway."""
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', 'top-secret')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo,test-tenant')
    resp = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': 'desktop-demo'})
    assert resp.status_code == 200
    assert 'token' in resp.get_json()


def test_mint_unset_secret_prod_closed(client, monkeypatch):
    """Fail-closed: with the secret unset, a NON-public tenant mint is refused
    in production (the C2 oracle). Dev stays open (backward compatible)."""
    monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET', raising=False)
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    resp = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': 'private-tenant'})
    assert resp.status_code == 403


# --- /internal/seed: the C1 unauthenticated write-token oracle ---

def test_seed_secret_set_missing_header_403(client, monkeypatch):
    """C1: /internal/seed must NOT mint a token (or seed) for a non-public
    tenant without the internal secret when the secret is configured."""
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', 'top-secret')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    resp = client.post('/r6/fhir/internal/seed',
                       json={'tenant_id': 'victim-tenant'})
    assert resp.status_code == 403
    assert 'step_up_token' not in resp.get_json()


def test_seed_secret_set_correct_header_ok(client, monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', 'top-secret')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    resp = client.post('/r6/fhir/internal/seed',
                       json={'tenant_id': 'ops-tenant'},
                       headers={'X-Internal-Secret': 'top-secret'})
    assert resp.status_code == 201
    assert resp.get_json().get('step_up_token')


def test_seed_public_tenant_open(client, monkeypatch):
    """Public/synthetic demo tenants stay seedable (browser demo needs it)."""
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', 'top-secret')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    resp = client.post('/r6/fhir/internal/seed',
                       json={'tenant_id': 'desktop-demo'})
    assert resp.status_code == 201


def test_seed_unset_secret_prod_closed(client, monkeypatch):
    monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET', raising=False)
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')
    resp = client.post('/r6/fhir/internal/seed',
                       json={'tenant_id': 'victim-tenant'})
    assert resp.status_code == 403


# --- Suffix-exemption bypass: a FHIR read of resource id "metadata"/"health"
# must NOT be treated as a public discovery endpoint. ---

def test_read_flag_on_resource_id_metadata_no_token_401(client, monkeypatch):
    """GET /r6/fhir/Patient/metadata is a resource read, NOT discovery.

    Pre-fix, path.endswith('/metadata') exempted this and leaked the Patient
    without a token. It must now require auth like any other read.
    """
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    resp = client.get('/r6/fhir/Patient/metadata',
                      headers={'X-Tenant-Id': 'private-tenant'})
    assert resp.status_code == 401
    assert resp.get_json()['issue'][0]['code'] == 'security'


def test_read_flag_on_resource_id_health_no_token_401(client, monkeypatch):
    """GET /r6/fhir/Patient/health (resource id 'health') must require auth."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    resp = client.get('/r6/fhir/Patient/health',
                      headers={'X-Tenant-Id': 'private-tenant'})
    assert resp.status_code == 401
    assert resp.get_json()['issue'][0]['code'] == 'security'


def test_read_flag_on_real_metadata_no_token_200(client, monkeypatch):
    """The real CapabilityStatement stays exempt with the flag on."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    resp = client.get('/r6/fhir/metadata')
    assert resp.status_code == 200


def test_read_flag_on_real_health_no_token_200(client, monkeypatch):
    """The real health route stays exempt with the flag on."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    resp = client.get('/r6/fhir/health')
    assert resp.status_code == 200


def test_tenant_hook_resource_id_metadata_still_requires_tenant(client):
    """Flag OFF: /r6/fhir/Patient/metadata still needs X-Tenant-Id.

    The same suffix bug also let the tenant hook exempt this path. With the
    exact-match fix, a missing tenant header is now a 400, not a silent pass.
    """
    resp = client.get('/r6/fhir/Patient/metadata')
    assert resp.status_code == 400
    assert resp.get_json()['issue'][0]['code'] == 'security'


# --- OAuth bearer access tokens authorize reads (the advertised mechanism) ---

def _mint_oauth_token(tenant_id, scopes):
    """Inject an OAuth access token directly into the in-memory store."""
    import secrets
    import time
    from r6 import oauth
    tok = secrets.token_urlsafe(16)
    oauth._access_tokens[tok] = {
        'client_id': 'test-client',
        'scopes': scopes,
        'tenant_id': tenant_id,
        'exp': time.time() + 3600,
    }
    return tok


def test_read_flag_on_oauth_bearer_read_scope_200(client, monkeypatch):
    """A valid OAuth bearer with a read scope for the tenant → 200."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    tok = _mint_oauth_token('private-tenant', ['patient/*.read'])
    resp = client.get(READ_PATH, headers={
        'X-Tenant-Id': 'private-tenant',
        'Authorization': f'Bearer {tok}',
    })
    assert resp.status_code == 200


def test_read_flag_on_oauth_bearer_wrong_tenant_401(client, monkeypatch):
    """OAuth bearer bound to another tenant must not authorize the read."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    tok = _mint_oauth_token('other-tenant', ['patient/*.read'])
    resp = client.get(READ_PATH, headers={
        'X-Tenant-Id': 'private-tenant',
        'Authorization': f'Bearer {tok}',
    })
    assert resp.status_code == 401


def test_read_flag_on_oauth_bearer_no_read_scope_401(client, monkeypatch):
    """OAuth bearer without a read scope must not authorize the read."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', '')
    tok = _mint_oauth_token('private-tenant', ['fhir.write'])
    resp = client.get(READ_PATH, headers={
        'X-Tenant-Id': 'private-tenant',
        'Authorization': f'Bearer {tok}',
    })
    assert resp.status_code == 401
