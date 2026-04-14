"""
Tests for FHIR upstream proxy mode.

Tests the proxy client, URL rewriting, and route integration when
FHIR_UPSTREAM_URL is configured. Uses unittest.mock to simulate
upstream FHIR server responses without network calls.
"""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

from r6.fhir_proxy import (
    FHIRUpstreamProxy, MedplumProxy, get_proxy, reset_proxy, is_proxy_enabled,
    _fetch_medplum_token, _medplum_cache,
)


# --- Unit tests for FHIRUpstreamProxy ---

class TestFHIRUpstreamProxy:
    """Tests for the proxy client class."""

    def setup_method(self):
        self.proxy = FHIRUpstreamProxy(
            upstream_url='https://hapi.fhir.org/baseR4',
            local_base_url='http://localhost:5000/r6/fhir',
        )

    def teardown_method(self):
        self.proxy.close()

    def test_url_rewriting(self):
        """Upstream URLs in responses are rewritten to local proxy."""
        data = {
            'resourceType': 'Bundle',
            'link': [{'url': 'https://hapi.fhir.org/baseR4/Patient?_count=10'}],
            'entry': [{
                'fullUrl': 'https://hapi.fhir.org/baseR4/Patient/123',
                'resource': {'resourceType': 'Patient', 'id': '123'},
            }],
        }
        rewritten = self.proxy._rewrite_urls(data)
        assert 'hapi.fhir.org' not in json.dumps(rewritten)
        assert 'localhost:5000/r6/fhir/Patient?_count=10' in json.dumps(rewritten)
        assert 'localhost:5000/r6/fhir/Patient/123' in json.dumps(rewritten)

    def test_url_rewriting_preserves_non_upstream(self):
        """Non-upstream URLs are not rewritten."""
        data = {'url': 'https://other-server.example.com/Patient/1'}
        rewritten = self.proxy._rewrite_urls(data)
        assert rewritten['url'] == 'https://other-server.example.com/Patient/1'

    def test_url_rewriting_nested(self):
        """URL rewriting handles nested lists and dicts."""
        data = {
            'entry': [
                {'resource': {'reference': 'https://hapi.fhir.org/baseR4/Patient/1'}},
                {'resource': {'reference': 'https://hapi.fhir.org/baseR4/Observation/2'}},
            ]
        }
        rewritten = self.proxy._rewrite_urls(data)
        assert rewritten['entry'][0]['resource']['reference'] == 'http://localhost:5000/r6/fhir/Patient/1'
        assert rewritten['entry'][1]['resource']['reference'] == 'http://localhost:5000/r6/fhir/Observation/2'

    def test_empty_bundle(self):
        """_empty_bundle returns a valid searchset."""
        bundle = FHIRUpstreamProxy._empty_bundle()
        assert bundle['resourceType'] == 'Bundle'
        assert bundle['type'] == 'searchset'
        assert bundle['total'] == 0
        assert bundle['entry'] == []

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_read_success(self, mock_client):
        """Successful read returns parsed and rewritten JSON."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'resourceType': 'Patient',
            'id': '123',
            'name': [{'family': 'Smith'}],
        }
        self.proxy._client.get = MagicMock(return_value=mock_resp)

        result = self.proxy.read('Patient', '123')
        assert result is not None
        assert result['resourceType'] == 'Patient'
        assert result['id'] == '123'

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_read_not_found(self, mock_client):
        """Read returns None for 404."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        self.proxy._client.get = MagicMock(return_value=mock_resp)

        result = self.proxy.read('Patient', 'nonexistent')
        assert result is None

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_read_network_error(self, mock_client):
        """Read returns None on network error."""
        self.proxy._client.get = MagicMock(side_effect=Exception('Connection refused'))

        result = self.proxy.read('Patient', '123')
        assert result is None

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_search_success(self, mock_client):
        """Successful search returns rewritten bundle."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'resourceType': 'Bundle',
            'type': 'searchset',
            'total': 1,
            'entry': [{'resource': {'resourceType': 'Patient', 'id': '1'}}],
        }
        self.proxy._client.get = MagicMock(return_value=mock_resp)

        result = self.proxy.search('Patient', {'name': 'Smith'})
        assert result['total'] == 1
        assert len(result['entry']) == 1

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_search_error_returns_empty_bundle(self, mock_client):
        """Search error returns an empty bundle, not an exception."""
        self.proxy._client.get = MagicMock(side_effect=Exception('Timeout'))

        result = self.proxy.search('Patient', {})
        assert result['total'] == 0
        assert result['entry'] == []

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_healthy_connected(self, mock_client):
        """Health check returns connected status."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'fhirVersion': '4.0.1',
            'software': {'name': 'HAPI FHIR'},
        }
        self.proxy._client.get = MagicMock(return_value=mock_resp)

        result = self.proxy.healthy()
        assert result['status'] == 'connected'
        assert result['fhir_version'] == '4.0.1'

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_healthy_unreachable(self, mock_client):
        """Health check returns unreachable on error."""
        self.proxy._client.get = MagicMock(side_effect=Exception('DNS failure'))

        result = self.proxy.healthy()
        assert result['status'] == 'unreachable'

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_create_success(self, mock_client):
        """Create forwards to upstream and returns result."""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {'resourceType': 'Patient', 'id': 'new-1'}
        mock_resp.headers = {'content-type': 'application/fhir+json'}
        self.proxy._client.post = MagicMock(return_value=mock_resp)

        result, status = self.proxy.create('Patient', {'resourceType': 'Patient'})
        assert status == 201
        assert result['id'] == 'new-1'

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_update_with_if_match(self, mock_client):
        """Update passes If-Match header to upstream."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'resourceType': 'Patient', 'id': '123'}
        mock_resp.headers = {'content-type': 'application/fhir+json'}
        self.proxy._client.put = MagicMock(return_value=mock_resp)

        result, status = self.proxy.update('Patient', '123',
                                            {'resourceType': 'Patient', 'id': '123'},
                                            if_match='W/"2"')
        assert status == 200
        # Verify If-Match was passed
        call_kwargs = self.proxy._client.put.call_args
        assert call_kwargs[1]['headers']['If-Match'] == 'W/"2"'


# --- Module-level singleton tests ---

class TestProxySingleton:
    """Tests for the module-level proxy singleton."""

    def setup_method(self):
        reset_proxy()

    def teardown_method(self):
        reset_proxy()
        os.environ.pop('FHIR_UPSTREAM_URL', None)

    def test_no_proxy_when_not_configured(self):
        """get_proxy() returns None when FHIR_UPSTREAM_URL is not set."""
        os.environ.pop('FHIR_UPSTREAM_URL', None)
        assert get_proxy() is None
        assert not is_proxy_enabled()

    def test_proxy_when_configured(self):
        """get_proxy() returns a proxy when FHIR_UPSTREAM_URL is set."""
        os.environ['FHIR_UPSTREAM_URL'] = 'https://hapi.fhir.org/baseR4'
        proxy = get_proxy()
        assert proxy is not None
        assert proxy.upstream_url == 'https://hapi.fhir.org/baseR4'
        assert is_proxy_enabled()

    def test_proxy_singleton_reuse(self):
        """get_proxy() returns the same instance on repeated calls."""
        os.environ['FHIR_UPSTREAM_URL'] = 'https://hapi.fhir.org/baseR4'
        p1 = get_proxy()
        p2 = get_proxy()
        assert p1 is p2

    def test_empty_string_not_enabled(self):
        """Empty FHIR_UPSTREAM_URL is treated as not configured."""
        os.environ['FHIR_UPSTREAM_URL'] = '  '
        assert get_proxy() is None
        assert not is_proxy_enabled()


# --- Route integration tests (proxy mode) ---

class TestProxyRouteIntegration:
    """Test that routes use proxy when configured, with guardrails applied."""

    @pytest.fixture(autouse=True)
    def setup(self, app, client, tenant_headers):
        self.app = app
        self.client = client
        self.tenant_headers = tenant_headers
        reset_proxy()
        yield
        reset_proxy()
        os.environ.pop('FHIR_UPSTREAM_URL', None)

    def test_read_via_proxy(self):
        """Read route fetches from upstream when proxy is enabled."""
        os.environ['FHIR_UPSTREAM_URL'] = 'https://hapi.fhir.org/baseR4'

        upstream_patient = {
            'resourceType': 'Patient',
            'id': 'upstream-pt-1',
            'name': [{'family': 'Johnson', 'given': ['Robert']}],
            'identifier': [{'value': 'MRN-REAL-123456'}],
            'address': [{'line': ['456 Real St'], 'city': 'Chicago', 'state': 'IL'}],
            'telecom': [{'system': 'phone', 'value': '312-555-9999'}],
        }

        with patch('r6.routes.get_proxy') as mock_get:
            mock_proxy = MagicMock()
            mock_proxy.read.return_value = upstream_patient
            mock_get.return_value = mock_proxy

            resp = self.client.get('/r6/fhir/Patient/upstream-pt-1',
                                   headers=self.tenant_headers)
            assert resp.status_code == 200
            data = resp.get_json()
            # Guardrails applied: identifier redacted
            assert data['identifier'][0]['value'] == '***3456'
            # Address line stripped
            assert 'line' not in data['address'][0]
            # Telecom redacted
            assert data['telecom'][0]['value'] == '[Redacted]'
            # Source marker present
            assert data.get('_source') == 'upstream'

    def test_read_via_proxy_not_found(self):
        """Read returns 404 when upstream returns nothing."""
        os.environ['FHIR_UPSTREAM_URL'] = 'https://hapi.fhir.org/baseR4'

        with patch('r6.routes.get_proxy') as mock_get:
            mock_proxy = MagicMock()
            mock_proxy.read.return_value = None
            mock_get.return_value = mock_proxy

            resp = self.client.get('/r6/fhir/Patient/nonexistent',
                                   headers=self.tenant_headers)
            assert resp.status_code == 404

    def test_search_via_proxy(self):
        """Search route forwards to upstream with guardrails on results."""
        os.environ['FHIR_UPSTREAM_URL'] = 'https://hapi.fhir.org/baseR4'

        upstream_bundle = {
            'resourceType': 'Bundle',
            'type': 'searchset',
            'total': 2,
            'entry': [
                {'fullUrl': 'https://hapi.fhir.org/baseR4/Patient/1',
                 'resource': {
                     'resourceType': 'Patient', 'id': '1',
                     'name': [{'family': 'Smith'}],
                     'identifier': [{'value': 'MRN-0001'}],
                 }},
                {'fullUrl': 'https://hapi.fhir.org/baseR4/Patient/2',
                 'resource': {
                     'resourceType': 'Patient', 'id': '2',
                     'name': [{'family': 'Jones'}],
                 }},
            ],
        }

        with patch('r6.routes.get_proxy') as mock_get:
            mock_proxy = MagicMock()
            mock_proxy.search.return_value = upstream_bundle
            mock_get.return_value = mock_proxy

            resp = self.client.get('/r6/fhir/Patient?name=Smith',
                                   headers=self.tenant_headers)
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['total'] == 2
            assert len(data['entry']) == 2
            # Identifier redacted on upstream data
            assert data['entry'][0]['resource']['identifier'][0]['value'] == '***0001'
            assert data.get('_source') == 'upstream'

    def test_local_mode_when_proxy_not_configured(self):
        """Routes use local SQLite when no upstream is configured."""
        os.environ.pop('FHIR_UPSTREAM_URL', None)

        resp = self.client.get('/r6/fhir/Patient/nonexistent',
                               headers=self.tenant_headers)
        # Should get 404 from local DB, not a proxy error
        assert resp.status_code == 404
        data = resp.get_json()
        assert '_source' not in data

    def test_health_shows_upstream_status(self):
        """Health endpoint reports upstream connection status."""
        os.environ['FHIR_UPSTREAM_URL'] = 'https://hapi.fhir.org/baseR4'

        with patch('r6.routes.get_proxy') as mock_get:
            mock_proxy = MagicMock()
            mock_proxy.healthy.return_value = {
                'status': 'connected',
                'upstream_url': 'https://hapi.fhir.org/baseR4',
                'fhir_version': '4.0.1',
                'software': 'HAPI FHIR',
            }
            mock_get.return_value = mock_proxy

            with patch('r6.routes.is_proxy_enabled', return_value=True):
                resp = self.client.get('/r6/fhir/health')
                assert resp.status_code == 200
                data = resp.get_json()
                assert data['mode'] == 'upstream'
                assert data['checks']['upstream']['status'] == 'connected'

    def test_health_local_mode(self):
        """Health endpoint shows local mode when no upstream."""
        os.environ.pop('FHIR_UPSTREAM_URL', None)

        resp = self.client.get('/r6/fhir/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['mode'] == 'local'
        assert data['checks']['upstream'] == 'not_configured'

    def test_create_via_proxy(self, auth_headers):
        """Create route forwards to upstream with all guardrails."""
        os.environ['FHIR_UPSTREAM_URL'] = 'https://hapi.fhir.org/baseR4'

        patient = {
            'resourceType': 'Patient',
            'name': [{'family': 'NewPatient'}],
        }

        with patch('r6.routes.get_proxy') as mock_get:
            mock_proxy = MagicMock()
            mock_proxy.create.return_value = (
                {'resourceType': 'Patient', 'id': 'server-assigned-id',
                 'name': [{'family': 'NewPatient'}]},
                201,
            )
            mock_get.return_value = mock_proxy

            resp = self.client.post('/r6/fhir/Patient',
                                    data=json.dumps(patient),
                                    content_type='application/json',
                                    headers={**auth_headers, 'X-Human-Confirmed': 'true'})
            assert resp.status_code == 201
            data = resp.get_json()
            assert data['id'] == 'server-assigned-id'
            assert data.get('_source') == 'upstream'

    def test_metadata_shows_proxy_description(self):
        """Metadata describes proxy mode when upstream is configured."""
        with patch('r6.routes.is_proxy_enabled', return_value=True):
            resp = self.client.get('/r6/fhir/metadata')
            assert resp.status_code == 200
            data = resp.get_json()
            assert 'upstream' in data['implementation']['description'].lower()

    def test_metadata_shows_local_description(self):
        """Metadata describes local mode when no upstream."""
        with patch('r6.routes.is_proxy_enabled', return_value=False):
            resp = self.client.get('/r6/fhir/metadata')
            assert resp.status_code == 200
            data = resp.get_json()
            assert 'local' in data['implementation']['description'].lower()


# ---------------------------------------------------------------------------
# Medplum proxy tests
# ---------------------------------------------------------------------------

class TestMedplumProxy:
    """Tests for OAuth2 client-credentials token flow and MedplumProxy."""

    def setup_method(self):
        # Reset in-process token cache before each test
        _medplum_cache['token'] = None
        _medplum_cache['expires_at'] = 0.0
        reset_proxy()
        os.environ.pop('MEDPLUM_BASE_URL', None)
        os.environ.pop('MEDPLUM_CLIENT_ID', None)
        os.environ.pop('MEDPLUM_CLIENT_SECRET', None)

    teardown_method = setup_method  # same cleanup on exit

    # --- _fetch_medplum_token ---

    def test_fetch_token_calls_token_endpoint(self):
        """Token is fetched from Medplum OAuth endpoint when cache is cold."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'access_token': 'tok-abc123',
            'expires_in': 3600,
        }
        mock_resp.raise_for_status = MagicMock()

        with patch('r6.fhir_proxy.httpx.post', return_value=mock_resp) as mock_post:
            token = _fetch_medplum_token('client-id', 'client-secret')

        assert token == 'tok-abc123'
        call_kwargs = mock_post.call_args
        assert 'https://api.medplum.com/oauth2/token' in call_kwargs[0]

    def test_fetch_token_cached_in_process(self):
        """Second call reuses in-process cache; token endpoint called only once."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'access_token': 'tok-xyz', 'expires_in': 3600}
        mock_resp.raise_for_status = MagicMock()

        with patch('r6.fhir_proxy.httpx.post', return_value=mock_resp) as mock_post, \
             patch('r6.fhir_proxy._get_redis', return_value=None):
            _fetch_medplum_token('cid', 'csec')
            token2 = _fetch_medplum_token('cid', 'csec')

        assert token2 == 'tok-xyz'
        assert mock_post.call_count == 1  # only one HTTP call

    def test_fetch_token_redis_hit_skips_http(self):
        """Token served from Redis — no HTTP call made."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b'cached-redis-token'

        with patch('r6.fhir_proxy._get_redis', return_value=mock_redis), \
             patch('r6.fhir_proxy.httpx.post') as mock_post:
            token = _fetch_medplum_token('cid', 'csec')

        assert token == 'cached-redis-token'
        mock_post.assert_not_called()

    def test_fetch_token_stored_in_redis(self):
        """Fresh token is written to Redis with correct TTL."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # cache miss

        mock_resp = MagicMock()
        mock_resp.json.return_value = {'access_token': 'new-tok', 'expires_in': 1800}
        mock_resp.raise_for_status = MagicMock()

        with patch('r6.fhir_proxy._get_redis', return_value=mock_redis), \
             patch('r6.fhir_proxy.httpx.post', return_value=mock_resp):
            _fetch_medplum_token('cid', 'csec')

        mock_redis.setex.assert_called_once()
        key, ttl, value = mock_redis.setex.call_args[0]
        assert key == 'medplum:access_token'
        assert ttl == 1800 - 60  # 60-second safety buffer
        assert value == 'new-tok'

    # --- get_proxy with MEDPLUM_BASE_URL ---

    def test_get_proxy_returns_medplum_proxy(self):
        """get_proxy() returns MedplumProxy when MEDPLUM_BASE_URL is set."""
        os.environ['MEDPLUM_BASE_URL'] = 'https://api.medplum.com/fhir/R4'
        os.environ['MEDPLUM_CLIENT_ID'] = 'cid'
        os.environ['MEDPLUM_CLIENT_SECRET'] = 'csec'

        proxy = get_proxy()
        assert isinstance(proxy, MedplumProxy)
        proxy.close()

    def test_get_proxy_medplum_missing_credentials(self):
        """get_proxy() returns None when Medplum credentials are missing."""
        os.environ['MEDPLUM_BASE_URL'] = 'https://api.medplum.com/fhir/R4'
        # CLIENT_ID / CLIENT_SECRET intentionally absent

        proxy = get_proxy()
        assert proxy is None

    def test_is_proxy_enabled_with_medplum(self):
        """is_proxy_enabled() returns True when MEDPLUM_BASE_URL is set."""
        os.environ['MEDPLUM_BASE_URL'] = 'https://api.medplum.com/fhir/R4'
        assert is_proxy_enabled() is True

    def test_fhir_upstream_takes_priority_over_medplum(self):
        """FHIR_UPSTREAM_URL takes priority; MedplumProxy is NOT created."""
        os.environ['FHIR_UPSTREAM_URL'] = 'https://hapi.fhir.org/baseR4'
        os.environ['MEDPLUM_BASE_URL'] = 'https://api.medplum.com/fhir/R4'
        os.environ['MEDPLUM_CLIENT_ID'] = 'cid'
        os.environ['MEDPLUM_CLIENT_SECRET'] = 'csec'

        proxy = get_proxy()
        assert isinstance(proxy, FHIRUpstreamProxy)
        assert not isinstance(proxy, MedplumProxy)
        proxy.close()
        os.environ.pop('FHIR_UPSTREAM_URL', None)
