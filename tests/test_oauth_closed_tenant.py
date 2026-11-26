"""A closed tenant refuses OAuth reads.

Disconnect writes the tenant tombstone (r6.fasten.models.TenantClosure). A
patient connect token stops reading then (SEC940-F2), but a SMART OAuth
bearer the account holder approved earlier was still accepted by the read
gate until it expired: the bearer branch of authorize_tenant_read never
asked whether the tenant was closed. It asks now, after the token is
validated (so an unauthenticated caller learns nothing about which tenants
are closed), and the refusal writes one PHI-free AuditEvent.

Synthetic tenant ids only. Each test names the mutation that turns it red.
"""

from __future__ import annotations

import secrets
import time

import pytest
from sqlalchemy.exc import OperationalError

from r6 import oauth

FHIR_RESOURCE = 'http://localhost/r6/fhir'
TENANT = 'oauth-closed-t1'
OTHER = 'oauth-open-t2'
REFUSAL_DETAIL = 'OAuth read refused at {endpoint}: tenant disconnected'


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_TENANTS', 'desktop-demo')


def _store_token(tenant_id, aud=FHIR_RESOURCE, scopes=('fhir.read',)):
    token = secrets.token_urlsafe(24)
    oauth._oauth_store_set('access-token', token, {
        'client_id': 'closed-tenant-test', 'scopes': list(scopes),
        'tenant_id': tenant_id, 'aud': aud, 'exp': time.time() + 300},
        ttl=300)
    return token


def _read(client, tenant_id, token):
    return client.get('/r6/fhir/Patient?_summary=count', headers={
        'X-Tenant-Id': tenant_id, 'Authorization': f'Bearer {token}'})


def _close(app, tenant_id):
    from models import db
    from r6.fasten.models import TenantClosure
    with app.app_context():
        db.session.add(TenantClosure(tenant_id=tenant_id))
        db.session.commit()


def _failure_rows(app, tenant_id):
    from r6.models import AuditEventRecord
    with app.app_context():
        return [(r.event_type, r.detail) for r in AuditEventRecord.query
                .filter_by(tenant_id=tenant_id, outcome='failure').all()]


def test_an_oauth_bearer_stops_reading_once_its_tenant_is_closed(
        client, app, env):
    """MUTATION: drop the closed-tenant check on the OAuth branch of
    r6/read_auth.authorize_tenant_read -> the bearer keeps answering 200."""
    token = _store_token(TENANT)
    assert _read(client, TENANT, token).status_code == 200

    _close(app, TENANT)
    resp = _read(client, TENANT, token)
    assert resp.status_code == 401, resp.get_data(as_text=True)
    assert token not in resp.get_data(as_text=True)


def test_the_refusal_writes_one_phi_free_audit_row(client, app, env):
    """MUTATION: drop the audit call from the refusal -> no failure row."""
    token = _store_token(TENANT)
    _close(app, TENANT)
    assert _read(client, TENANT, token).status_code == 401

    rows = _failure_rows(app, TENANT)
    assert rows == [('read', REFUSAL_DETAIL.format(
        endpoint='r6.search_resources'))], rows
    # No token, no path, no query string, no header in the detail.
    assert token not in rows[0][1]
    assert 'Patient' not in rows[0][1]


def test_an_open_tenant_keeps_reading(client, app, env):
    """Closure is per tenant: closing one leaves another's bearer working."""
    _close(app, TENANT)
    assert _read(client, OTHER, _store_token(OTHER)).status_code == 200
    assert _failure_rows(app, OTHER) == []


def test_an_invalid_bearer_on_a_closed_tenant_is_not_told_it_is_closed(
        client, app, env):
    """The closure is asked only after the token is validated, so an
    unauthenticated caller cannot tell a closed tenant from an open one: the
    same status, the same body, the same audit rows, and no closure row.

    MUTATION: ask _oauth_tenant_closed before _oauth_authorizes in
    r6/read_auth.authorize_tenant_read -> the closed tenant gains a
    "tenant disconnected" row the open one does not have."""
    _close(app, TENANT)
    closed = _read(client, TENANT, 'not-a-real-token')
    opened = _read(client, OTHER, 'not-a-real-token')
    assert closed.status_code == opened.status_code == 401
    # The body names the tenant asked for and nothing else differs.
    assert (closed.get_data(as_text=True).replace(TENANT, 'T')
            == opened.get_data(as_text=True).replace(OTHER, 'T'))
    assert _failure_rows(app, TENANT) == _failure_rows(app, OTHER)
    assert not [d for _e, d in _failure_rows(app, TENANT)
                if 'disconnected' in (d or '')]


def test_an_audit_store_failure_still_refuses(client, app, env, monkeypatch):
    """The fail-closed state of a refusal is the refusal: an audit write
    that fails is logged, and the bearer still gets no data."""
    import r6.access as access_mod

    def _broken_audit(**_kwargs):
        raise OperationalError('INSERT', {}, Exception('store down'))

    token = _store_token(TENANT)
    _close(app, TENANT)
    monkeypatch.setattr(access_mod, 'audit', _broken_audit)
    assert _read(client, TENANT, token).status_code == 401


def test_a_closure_lookup_failure_answers_500_with_no_data(
        client, app, env, monkeypatch):
    """Fail closed: when the tombstone cannot be read, the bearer is not
    treated as reading an open tenant. The gate does not catch the failure,
    so the caller gets the engine's 500 and no Bundle. The lookup was asked,
    once, about this tenant: an earlier, unrelated failure cannot pass for
    this one."""
    from r6.fasten import models as fasten_models
    asked = []

    def _broken_lookup(tenant_id):
        asked.append(tenant_id)
        raise OperationalError('SELECT', {}, Exception('store down'))

    token = _store_token(TENANT)
    monkeypatch.setattr(fasten_models, 'tenant_closed', _broken_lookup)
    # Answer as production does instead of re-raising into the test.
    monkeypatch.setitem(app.config, 'PROPAGATE_EXCEPTIONS', False)
    resp = _read(client, TENANT, token)
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert asked == [TENANT]
    assert 'Bundle' not in resp.get_data(as_text=True)


@pytest.mark.parametrize('token_tenant', [None, ''])
def test_a_token_that_names_no_tenant_reads_no_tenant(
        client, app, env, token_tenant):
    """tenant_closed answers False for an empty tenant id, so a token with
    none is never refused as closed. It needs no such refusal: the read gate
    matches the token's tenant to the request's, so the token reads nothing.
    A token built by the same helper that does name the tenant reads 200
    (the control), so the missing tenant is what refuses.

    MUTATION: drop the tenant match from r6/read_auth._oauth_authorizes ->
    the token with no tenant reads 200."""
    assert _read(client, TENANT, _store_token(TENANT)).status_code == 200
    token = _store_token(token_tenant)
    assert _read(client, TENANT, token).status_code == 401
    _close(app, TENANT)
    assert _read(client, TENANT, token).status_code == 401


@pytest.mark.parametrize('token_tenant', [None, ''])
def test_a_token_that_names_no_tenant_is_inactive_at_introspection(
        client, app, monkeypatch, token_tenant):
    """Introspection is what a resource server trusts, and it has no tenant
    to serve such a token under. A token built by the same helper that does
    name a tenant answers active (the control).

    MUTATION: drop the empty-tenant check from r6/oauth.py introspect() ->
    the token answers active."""
    monkeypatch.setenv('MCP_INTROSPECTION_CLIENT_ID', 'mcp-server')
    monkeypatch.setenv('MCP_INTROSPECTION_CLIENT_SECRET', 'introspection-secret')

    def _introspect(token):
        return client.post('/r6/fhir/oauth/introspect', data={
            'token': token, 'client_id': 'mcp-server',
            'client_secret': 'introspection-secret'}).get_json()

    assert _introspect(_store_token(OTHER))['active'] is True
    assert _introspect(_store_token(token_tenant)) == {'active': False}
