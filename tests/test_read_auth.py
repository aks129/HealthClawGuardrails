"""
Tenant read authentication: the X-Tenant-Id header must be *authenticated*,
not merely present, on FHIR reads.

Public/synthetic tenants (PUBLIC_TENANTS) stay open for the demo. Every
other tenant must prove the claim with a tenant-bound step-up token or a
SMART bearer whose tenant_id matches. The CapabilityStatement must also
advertise the SMART OAuth security service.
"""
import time

import pytest

from r6.stepup import generate_step_up_token

READ_PATH = '/r6/fhir/Patient?_summary=count'
PRIVATE_TENANT = 'private-clinic'  # deliberately NOT in PUBLIC_TENANTS


def _is_auth_error(resp):
    return resp.status_code == 401


# --- Read authentication on non-public tenants ---

def test_private_tenant_read_without_auth_is_rejected(client):
    resp = client.get(READ_PATH, headers={'X-Tenant-Id': PRIVATE_TENANT})
    assert resp.status_code == 401
    body = resp.get_json()
    assert body['resourceType'] == 'OperationOutcome'
    assert body['issue'][0]['code'] == 'security'


def test_private_tenant_read_with_valid_stepup_is_allowed(client):
    token = generate_step_up_token(PRIVATE_TENANT)
    resp = client.get(READ_PATH, headers={
        'X-Tenant-Id': PRIVATE_TENANT,
        'X-Step-Up-Token': token,
    })
    assert not _is_auth_error(resp)
    assert resp.status_code == 200


def test_private_tenant_read_with_invalid_stepup_is_rejected(client):
    resp = client.get(READ_PATH, headers={
        'X-Tenant-Id': PRIVATE_TENANT,
        'X-Step-Up-Token': 'garbage.not-a-real-signature',
    })
    assert resp.status_code == 401


def test_stepup_for_other_tenant_is_rejected(client):
    """A token bound to tenant A must not unlock tenant B's data."""
    token = generate_step_up_token('some-other-tenant')
    resp = client.get(READ_PATH, headers={
        'X-Tenant-Id': PRIVATE_TENANT,
        'X-Step-Up-Token': token,
    })
    assert resp.status_code == 401


def test_private_tenant_read_with_matching_bearer_is_allowed(client):
    from r6 import oauth
    tok = 'test-bearer-private'
    oauth._access_tokens[tok] = {
        'client_id': 'c1', 'scopes': ['patient/*.read'],
        'tenant_id': PRIVATE_TENANT, 'exp': time.time() + 3600,
    }
    try:
        resp = client.get(READ_PATH, headers={
            'X-Tenant-Id': PRIVATE_TENANT,
            'Authorization': f'Bearer {tok}',
        })
        assert not _is_auth_error(resp)
        assert resp.status_code == 200
    finally:
        oauth._access_tokens.pop(tok, None)


def test_bearer_for_other_tenant_is_rejected(client):
    from r6 import oauth
    tok = 'test-bearer-mismatch'
    oauth._access_tokens[tok] = {
        'client_id': 'c1', 'scopes': ['patient/*.read'],
        'tenant_id': 'some-other-tenant', 'exp': time.time() + 3600,
    }
    try:
        resp = client.get(READ_PATH, headers={
            'X-Tenant-Id': PRIVATE_TENANT,
            'Authorization': f'Bearer {tok}',
        })
        assert resp.status_code == 401
    finally:
        oauth._access_tokens.pop(tok, None)


# --- Regressions: public tenants and discovery stay open ---

def test_public_tenant_read_without_auth_still_works(client):
    """test-tenant is in PUBLIC_TENANTS (conftest) — must not require a token."""
    resp = client.get(READ_PATH, headers={'X-Tenant-Id': 'test-tenant'})
    assert not _is_auth_error(resp)
    assert resp.status_code == 200


def test_metadata_without_tenant_still_public(client):
    resp = client.get('/r6/fhir/metadata')
    assert resp.status_code == 200


def test_internal_step_up_token_endpoint_still_reachable(client):
    """Token minting must stay open or there's no way to obtain read auth."""
    resp = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': PRIVATE_TENANT})
    assert resp.status_code == 200
    assert resp.get_json()['token']


# --- Phase 2: CapabilityStatement advertises SMART OAuth security ---

def test_capability_statement_declares_smart_security(client):
    resp = client.get('/r6/fhir/metadata')
    rest = resp.get_json()['rest'][0]
    assert 'security' in rest, 'rest.security must advertise SMART OAuth'
    security = rest['security']
    codes = [c['code']
             for svc in security.get('service', [])
             for c in svc.get('coding', [])]
    assert 'SMART-on-FHIR' in codes
    oauth_ext = next(e for e in security['extension']
                     if e['url'].endswith('/oauth-uris'))
    sub = {x['url']: x['valueUri'] for x in oauth_ext['extension']}
    assert sub['authorize'].endswith('/r6/fhir/oauth/authorize')
    assert sub['token'].endswith('/r6/fhir/oauth/token')
