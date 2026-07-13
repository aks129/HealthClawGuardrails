"""Tests for r6/sdc/delivery.py — signed, expiring download links for a
persisted intake-PDF DocumentReference (Task 7).

The signed URL IS the authorization: the public download route is reachable
without X-Tenant-Id / X-Step-Up-Token headers, so these tests exercise the
route with a bare test client and rely on the signature in the query string.
"""

import time
from urllib.parse import urlparse, parse_qs

import pytest

from r6.sdc.delivery import (
    build_document_link,
    verify_document_link,
    _sign,
)
from r6.sdc.documents import persist_intake_document

PDF_BYTES = b"%PDF-1.4 hello"


@pytest.fixture(autouse=True)
def _delivery_env(monkeypatch):
    """Every delivery test needs a signing secret and a base URL."""
    monkeypatch.setenv('STEP_UP_SECRET', 'test-secret')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')


def _persist(app, tenant='test-tenant', pdf=PDF_BYTES):
    with app.app_context():
        resource = persist_intake_document(tenant, "Patient/123", pdf)
        return resource["id"]


def _route_path_and_query(link):
    """Split an absolute link into (path, query-dict) for the test client."""
    parsed = urlparse(link)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    return parsed.path, query


# --- build_document_link / verify_document_link unit behaviour ---

def test_build_link_contains_route_and_params():
    link = build_document_link('test-tenant', 'docref-1')
    assert '/r6/sdc/documents/docref-1' in link
    _, query = _route_path_and_query(link)
    assert query['t'] == 'test-tenant'
    assert 'exp' in query
    assert 'sig' in query


def test_verify_accepts_freshly_built_link():
    link = build_document_link('test-tenant', 'docref-1', now=1000)
    _, query = _route_path_and_query(link)
    ok, reason = verify_document_link(
        'test-tenant', 'docref-1', query['exp'], query['sig'], now=1000)
    assert ok is True
    assert reason == 'ok'


def test_link_is_absolute_and_uses_base_url():
    link = build_document_link('test-tenant', 'docref-1')
    assert link.startswith('https://example.test/r6/sdc/documents/docref-1')


def test_tenant_id_is_url_quoted():
    # urllib.parse.quote (default safe='/') percent-encodes the space.
    link = build_document_link('tenant with space', 'docref-1')
    assert 't=tenant%20with%20space' in link


def test_verify_tenant_bound_signature_rejects_other_tenant():
    link = build_document_link('tenant-A', 'docref-1', now=1000)
    _, query = _route_path_and_query(link)
    ok, reason = verify_document_link(
        'tenant-B', 'docref-1', query['exp'], query['sig'], now=1000)
    assert ok is False
    assert reason == 'bad-signature'


def test_verify_tampered_signature_is_bad_signature():
    link = build_document_link('test-tenant', 'docref-1', now=1000)
    _, query = _route_path_and_query(link)
    bad_sig = ('0' if query['sig'][0] != '0' else '1') + query['sig'][1:]
    ok, reason = verify_document_link(
        'test-tenant', 'docref-1', query['exp'], bad_sig, now=1000)
    assert ok is False
    assert reason == 'bad-signature'


def test_verify_tampered_exp_caught_as_bad_signature_before_expiry():
    # Keep the original signature but move exp far into the future. Because
    # signature is checked before expiry, this is bad-signature, not a pass.
    link = build_document_link('test-tenant', 'docref-1', now=1000)
    _, query = _route_path_and_query(link)
    forged_exp = str(int(query['exp']) + 999999)
    ok, reason = verify_document_link(
        'test-tenant', 'docref-1', forged_exp, query['sig'], now=1000)
    assert ok is False
    assert reason == 'bad-signature'


def test_verify_expired_link():
    link = build_document_link('test-tenant', 'docref-1',
                               ttl_seconds=100, now=1000)
    _, query = _route_path_and_query(link)
    # now is past exp (1100)
    ok, reason = verify_document_link(
        'test-tenant', 'docref-1', query['exp'], query['sig'], now=5000)
    assert ok is False
    assert reason == 'expired'


def test_verify_malformed_exp():
    ok, reason = verify_document_link(
        'test-tenant', 'docref-1', 'not-a-number', 'deadbeef', now=1000)
    assert ok is False
    assert reason == 'malformed'


def test_build_link_raises_without_public_base_url(monkeypatch):
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    with pytest.raises(ValueError, match='PUBLIC_BASE_URL is required'):
        build_document_link('test-tenant', 'docref-1')


def test_sign_raises_without_secret(monkeypatch):
    monkeypatch.delenv('STEP_UP_SECRET', raising=False)
    with pytest.raises(ValueError, match='STEP_UP_SECRET is required'):
        _sign('test-tenant', 'docref-1', 1234567890)


# --- Full round-trip through the public route ---

def test_route_round_trip_returns_pdf_bytes(app, client):
    docref_id = _persist(app)
    link = build_document_link('test-tenant', docref_id)
    path, query = _route_path_and_query(link)

    resp = client.get(path, query_string=query)

    assert resp.status_code == 200
    assert resp.mimetype == 'application/pdf'
    assert resp.data == PDF_BYTES


def test_route_sets_attachment_disposition(app, client):
    docref_id = _persist(app)
    link = build_document_link('test-tenant', docref_id)
    path, query = _route_path_and_query(link)

    resp = client.get(path, query_string=query)
    assert 'attachment' in resp.headers.get('Content-Disposition', '')
    assert 'intake.pdf' in resp.headers.get('Content-Disposition', '')


def test_route_tampered_signature_is_403(app, client):
    docref_id = _persist(app)
    link = build_document_link('test-tenant', docref_id)
    path, query = _route_path_and_query(link)
    query['sig'] = ('0' if query['sig'][0] != '0' else '1') + query['sig'][1:]

    resp = client.get(path, query_string=query)
    assert resp.status_code == 403


def test_route_tampered_exp_is_403(app, client):
    docref_id = _persist(app)
    link = build_document_link('test-tenant', docref_id)
    path, query = _route_path_and_query(link)
    query['exp'] = str(int(query['exp']) + 999999)

    resp = client.get(path, query_string=query)
    assert resp.status_code == 403


def test_route_expired_link_is_410(app, client):
    docref_id = _persist(app)
    # Build a link that already expired well before "now".
    link = build_document_link('test-tenant', docref_id,
                               ttl_seconds=1, now=1000)
    path, query = _route_path_and_query(link)

    resp = client.get(path, query_string=query)
    assert resp.status_code == 410


def test_route_unknown_docref_is_404(app, client):
    # Valid signature for a docref that does not exist.
    link = build_document_link('test-tenant', 'does-not-exist')
    path, query = _route_path_and_query(link)

    resp = client.get(path, query_string=query)
    assert resp.status_code == 404


def test_route_missing_query_params_is_400(app, client):
    resp = client.get('/r6/sdc/documents/docref-1')
    assert resp.status_code == 400


def test_route_reachable_without_tenant_headers(app, client):
    """The signed URL is the credential — no X-Tenant-Id header supplied."""
    docref_id = _persist(app)
    link = build_document_link('test-tenant', docref_id)
    path, query = _route_path_and_query(link)

    resp = client.get(path, query_string=query)  # no headers at all
    assert resp.status_code == 200
    assert resp.data == PDF_BYTES


def test_route_current_time_expiry(app, client):
    """A link built with a real (short) TTL still works right now, proving
    the default now=time.time() path (not just injected now=) is wired."""
    docref_id = _persist(app)
    link = build_document_link('test-tenant', docref_id,
                               ttl_seconds=3600, now=int(time.time()))
    path, query = _route_path_and_query(link)

    resp = client.get(path, query_string=query)
    assert resp.status_code == 200
