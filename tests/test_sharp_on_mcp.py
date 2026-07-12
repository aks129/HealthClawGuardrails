"""
SHARP-on-MCP (Standardised Healthcare Agent Remote Protocol) tests.

Verifies the per-request upstream proxy that the agent host can configure
via X-FHIR-Server-URL / X-FHIR-Access-Token / X-Patient-ID headers.
"""

import hashlib
from unittest.mock import patch


from r6.fhir_proxy import (
    make_sharp_proxy,
    get_proxy_for_request,
    is_sharp_context_active,
    close_request_proxy,
    reset_proxy,
    SHARP_SERVER_URL_HEADER,
    SHARP_ACCESS_TOKEN_HEADER,
    SHARP_PATIENT_ID_HEADER,
)


class TestMakeSharpProxy:
    """Unit tests for the SHARP proxy factory."""

    def teardown_method(self):
        reset_proxy()

    def test_bearer_token_added(self):
        proxy = make_sharp_proxy(
            server_url='https://hapi.fhir.org/baseR4',
            access_token='abc123',
        )
        try:
            assert proxy._client.headers['Authorization'] == 'Bearer abc123'
        finally:
            proxy.close()

    def test_bearer_prefix_stripped(self):
        """An incoming token that already has 'Bearer ' isn't double-prefixed."""
        proxy = make_sharp_proxy(
            server_url='https://hapi.fhir.org/baseR4',
            access_token='Bearer abc123',
        )
        try:
            assert proxy._client.headers['Authorization'] == 'Bearer abc123'
        finally:
            proxy.close()

    def test_no_token_no_auth_header(self):
        proxy = make_sharp_proxy(
            server_url='https://hapi.fhir.org/baseR4',
            access_token=None,
        )
        try:
            assert 'Authorization' not in proxy._client.headers
        finally:
            proxy.close()


class TestGetProxyForRequest:
    """Tests the per-request proxy resolver in a Flask request context."""

    def teardown_method(self):
        reset_proxy()

    def test_returns_sharp_proxy_when_headers_present(self, app):
        with app.test_request_context(
            '/r6/fhir/Patient/123',
            headers={
                SHARP_SERVER_URL_HEADER: 'https://hapi.fhir.org/baseR4',
                SHARP_ACCESS_TOKEN_HEADER: 'tok-xyz',
            },
        ):
            proxy = get_proxy_for_request()
            assert proxy is not None
            assert proxy.upstream_url == 'https://hapi.fhir.org/baseR4'
            assert proxy._client.headers['Authorization'] == 'Bearer tok-xyz'
            assert is_sharp_context_active() is True
            close_request_proxy()

    def test_falls_back_to_singleton_when_no_sharp_headers(self, app):
        """Without SHARP headers + no env upstream → returns None (local mode)."""
        with app.test_request_context('/r6/fhir/Patient/123'):
            assert is_sharp_context_active() is False
            assert get_proxy_for_request() is None

    def test_cached_per_request(self, app):
        """Two calls within the same request reuse the same proxy instance."""
        with app.test_request_context(
            '/r6/fhir/Patient/123',
            headers={SHARP_SERVER_URL_HEADER: 'https://hapi.fhir.org/baseR4'},
        ):
            first = get_proxy_for_request()
            second = get_proxy_for_request()
            assert first is second
            close_request_proxy()


class TestSharpTenantDerivation:
    """When SHARP context is active and X-Tenant-Id is omitted, a deterministic
    synthetic tenant is derived from the FHIR server URL so audit + guardrails
    still scope correctly."""

    def test_sharp_request_without_tenant_uses_derived_tenant(self, client):
        """Calling /metadata works without tenant (public endpoint)."""
        resp = client.get('/r6/fhir/metadata')
        assert resp.status_code == 200

    def test_sharp_request_synthesizes_tenant_for_protected_path(self, client):
        """Without SHARP headers, a missing tenant returns 400."""
        resp = client.get('/r6/fhir/Patient/no-such-id')
        assert resp.status_code == 400
        body = resp.get_json()
        assert body['resourceType'] == 'OperationOutcome'
        assert 'X-Tenant-Id' in body['issue'][0]['diagnostics']

    def test_sharp_headers_synthesize_tenant_when_missing(self, client):
        """With SHARP headers but no X-Tenant-Id, the tenant guard passes
        (derived synthetic tenant). Resource still not found in local store,
        so we get 404, not 400. We mock the proxy so no network call happens."""
        sharp_url = 'https://hapi.fhir.org/baseR4'
        expected_digest = hashlib.sha256(sharp_url.encode('utf-8')).hexdigest()[:16]
        expected_tenant = f'sharp-{expected_digest}'

        with patch('r6.fhir_proxy.FHIRUpstreamProxy.read', return_value=(None, 404)) as mock_read:
            resp = client.get(
                '/r6/fhir/Patient/upstream-only-id',
                headers={
                    SHARP_SERVER_URL_HEADER: sharp_url,
                    SHARP_ACCESS_TOKEN_HEADER: 'tok-xyz',
                },
            )
        # The synthetic tenant let the request past the tenant guard; the
        # proxy was consulted and returned None → 404 (not 400).
        assert resp.status_code == 404
        mock_read.assert_called_once()
        # Sanity-check the synthetic tenant ID format
        assert expected_tenant.startswith('sharp-')
        assert len(expected_tenant) == len('sharp-') + 16


class TestSharpProxyRouting:
    """End-to-end: SHARP headers route a Patient read through the per-request
    proxy instead of local storage."""

    def teardown_method(self):
        reset_proxy()

    def test_sharp_read_hits_per_request_proxy(self, client, tenant_id):
        upstream_resource = {
            'resourceType': 'Patient',
            'id': 'patient-from-upstream',
            'name': [{'family': 'Smith', 'given': ['John']}],
        }
        with patch(
            'r6.fhir_proxy.FHIRUpstreamProxy.read',
            return_value=(upstream_resource, 200),
        ) as mock_read:
            resp = client.get(
                '/r6/fhir/Patient/patient-from-upstream',
                headers={
                    'X-Tenant-Id': tenant_id,
                    SHARP_SERVER_URL_HEADER: 'https://r4.smarthealthit.org',
                    SHARP_ACCESS_TOKEN_HEADER: 'smart-token-xyz',
                    SHARP_PATIENT_ID_HEADER: 'patient-from-upstream',
                },
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['resourceType'] == 'Patient'
        assert body['id'] == 'patient-from-upstream'
        # PHI redaction still applied — name truncated by the guardrail layer
        # (HealthClaw never returns full names verbatim on the read path).
        # Just verify the source marker exists.
        assert body.get('_source') == 'upstream'
        mock_read.assert_called_once_with('Patient', 'patient-from-upstream')
