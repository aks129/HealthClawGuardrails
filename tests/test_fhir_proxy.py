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
    sanitize_upstream_error, upstream_unreachable_outcome,
    _SAFE_SEVERITIES,
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

        result, status = self.proxy.read('Patient', '123')
        assert status == 200
        assert result is not None
        assert result['resourceType'] == 'Patient'
        assert result['id'] == '123'

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_read_not_found(self, mock_client):
        """Read returns (None, 404) only for a true upstream 404."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        self.proxy._client.get = MagicMock(return_value=mock_resp)

        result, status = self.proxy.read('Patient', 'nonexistent')
        assert result is None
        assert status == 404

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_read_network_error(self, mock_client):
        """Network failure surfaces as (OperationOutcome, 502) — not a fake 404.

        Regression for #74: this used to return None, which the route turned
        into "Patient/123 not found".
        """
        self.proxy._client.get = MagicMock(side_effect=Exception('Connection refused'))

        result, status = self.proxy.read('Patient', '123')
        assert status == 502
        assert result['resourceType'] == 'OperationOutcome'
        assert result['issue'][0]['code'] == 'transient'

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_read_auth_failure_is_not_a_404(self, mock_client):
        """Upstream 401 surfaces as a security outcome, never as not-found.

        Regression for #74: an expired proxy token used to be reported to the
        caller as "this resource does not exist".
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {
            'resourceType': 'OperationOutcome',
            'issue': [{'severity': 'error', 'code': 'login',
                       'details': {'text': 'Invalid access token'}}],
        }
        self.proxy._client.get = MagicMock(return_value=mock_resp)

        result, status = self.proxy.read('Patient', '123')
        assert status == 502  # proxy-credential failure, not the caller's 401
        assert result['resourceType'] == 'OperationOutcome'
        assert result['issue'][0]['code'] == 'security'
        # The upstream's auth diagnostics describe OUR credentials — dropped
        assert 'Invalid access token' not in json.dumps(result)

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_malformed_200_body_surfaces_as_502(self, mock_client):
        """A 2xx response whose body fails to parse must not escape as an
        unhandled exception — it becomes a processing outcome."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError('not json')
        self.proxy._client.get = MagicMock(return_value=mock_resp)

        result, status = self.proxy.read('Patient', '1')
        assert status == 502
        assert result['issue'][0]['code'] == 'processing'
        result, status = self.proxy.search('Patient', {})
        assert status == 502
        assert result['issue'][0]['code'] == 'processing'

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_wrong_shape_200_body_surfaces_as_502(self, mock_client):
        """A parseable-but-non-object 200 (array/scalar) must not reach the
        route, which assumes a dict resource/Bundle — it maps to 502."""
        for shape in ([], 'a string', 42, None):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = shape
            self.proxy._client.get = MagicMock(return_value=mock_resp)
            result, status = self.proxy.read('Patient', '1')
            assert status == 502, f'read shape {shape!r}'
            assert result['issue'][0]['code'] == 'processing'
            result, status = self.proxy.search('Patient', {})
            assert status == 502, f'search shape {shape!r}'

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_search_non_bundle_dict_surfaces_as_502(self, mock_client):
        """A dict 200 that isn't a Bundle (e.g. a bare Patient) must not reach
        the route, which would synthesize a misleading empty searchset."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'resourceType': 'Patient', 'id': 'p1'}
        self.proxy._client.get = MagicMock(return_value=mock_resp)

        result, status = self.proxy.search('Patient', {})
        assert status == 502
        assert result['resourceType'] == 'OperationOutcome'
        assert result['issue'][0]['code'] == 'processing'

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

        result, status = self.proxy.search('Patient', {'name': 'Smith'})
        assert status == 200
        assert result['total'] == 1
        assert len(result['entry']) == 1

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_search_network_error_returns_outcome(self, mock_client):
        """Network failure surfaces as (OperationOutcome, 502) — never as an
        empty result set (#74: a failed search used to come back as total=0)."""
        self.proxy._client.get = MagicMock(side_effect=Exception('Timeout'))

        result, status = self.proxy.search('Patient', {})
        assert status == 502
        assert result['resourceType'] == 'OperationOutcome'
        assert result['issue'][0]['code'] == 'transient'

    @patch.object(FHIRUpstreamProxy, '_client', create=True)
    def test_search_upstream_rejection_passes_through_status(self, mock_client):
        """An upstream 400 OperationOutcome surfaces as a sanitized 400 with
        the real status and machine-readable code — but the upstream's own
        free text (which could carry PHI) is NOT forwarded (#74)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {
            'resourceType': 'OperationOutcome',
            'issue': [{'severity': 'error', 'code': 'invalid',
                       'details': {'text': 'Patient Rosa Hernandez already exists'},
                       'diagnostics': 'stack trace with https://internal:5432/db'}],
        }
        self.proxy._client.get = MagicMock(return_value=mock_resp)

        result, status = self.proxy.search('Observation', {'datetime': 'x'})
        assert status == 400
        assert result['resourceType'] == 'OperationOutcome'
        assert result['issue'][0]['code'] == 'invalid'  # code (safe enum) survives
        # Synthesized message, NOT the upstream text
        assert result['issue'][0]['details']['text'] == \
            'The upstream FHIR server rejected the request as invalid.'
        # Nothing from the upstream body transits: not the name, not the trace
        blob = json.dumps(result)
        assert 'Rosa Hernandez' not in blob
        assert 'internal:5432' not in blob
        assert 'diagnostics' not in result['issue'][0]

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

class TestUpstreamErrorSanitization:
    """Unit tests for the allowlist OperationOutcome sanitizer (#74).

    Only the FHIR value-set tokens severity/code survive from upstream; the
    human-readable text is synthesized from the code. Upstream free text is
    never forwarded (it can carry patient names or internal hostnames).
    """

    @staticmethod
    def _resp(status, body=None, json_error=False):
        resp = MagicMock()
        resp.status_code = status
        # A real, small bytes .content so the size guard measures a length
        # (a bare MagicMock has no len() and would read as unbounded).
        resp.content = json.dumps(body).encode() if body is not None else b'{}'
        if json_error:
            resp.json.side_effect = ValueError('not json')
        else:
            resp.json.return_value = body
        return resp

    def test_unknown_severity_and_code_are_mapped(self):
        oo = {'resourceType': 'OperationOutcome',
              'issue': [{'severity': 'catastrophic', 'code': 'made-up-code',
                         'details': {'text': 'bad thing'}}]}
        result, status = sanitize_upstream_error(self._resp(400, oo))
        assert status == 400
        assert result['issue'][0]['severity'] == 'error'
        assert result['issue'][0]['code'] == 'invalid'  # mapped from HTTP 400

    def test_only_severity_code_and_synthesized_text_survive(self):
        oo = {'resourceType': 'OperationOutcome',
              'issue': [{'severity': 'error', 'code': 'invalid',
                         'details': {'text': 'Rosa Hernandez at db.internal', 'coding': [{'code': 'x'}]},
                         'diagnostics': 'trace', 'expression': ['Patient.name'],
                         'extension': [{'url': 'x'}]}]}
        result, _ = sanitize_upstream_error(self._resp(400, oo))
        issue = result['issue'][0]
        assert set(issue.keys()) == {'severity', 'code', 'details'}
        assert set(issue['details'].keys()) == {'text'}
        # text is synthesized from the code, not copied from upstream
        assert issue['details']['text'] == 'The upstream FHIR server rejected the request as invalid.'
        assert 'Rosa Hernandez' not in json.dumps(result)
        assert 'db.internal' not in json.dumps(result)

    def test_upstream_free_text_never_forwarded_for_any_form(self):
        # None of these upstream free-text payloads may reach the caller —
        # names, hosts, IPs, encoded URLs are all replaced by synthesized text.
        for leak in ('patient Rosa Hernandez was seen', 'host clinic.internal',
                     '10.0.0.7', 'db.internal/path', 'https%3A%2F%2Fx.internal'):
            oo = {'resourceType': 'OperationOutcome',
                  'issue': [{'severity': 'error', 'code': 'invalid',
                             'details': {'text': f'error: {leak}'}}]}
            result, _ = sanitize_upstream_error(self._resp(400, oo))
            assert leak not in json.dumps(result), f'leaked: {leak!r}'

    def test_issue_count_is_bounded(self):
        oo = {'resourceType': 'OperationOutcome',
              'issue': [{'severity': 'error', 'code': 'invalid'}] * 20}
        result, _ = sanitize_upstream_error(self._resp(400, oo))
        assert len(result['issue']) == 5

    def test_non_json_body_is_synthesized(self):
        result, status = sanitize_upstream_error(self._resp(500, json_error=True))
        assert status == 502
        assert result['issue'][0]['code'] == 'transient'
        assert result['issue'][0]['details']['text'] == 'The upstream FHIR server had a transient error.'

    def test_non_operationoutcome_json_is_synthesized(self):
        result, status = sanitize_upstream_error(
            self._resp(429, {'error': 'rate limited', 'internal': 'stuff'}))
        assert status == 429
        assert result['issue'][0]['code'] == 'throttled'
        assert 'stuff' not in json.dumps(result)

    def test_401_maps_to_502_and_drops_upstream_text(self):
        oo = {'resourceType': 'OperationOutcome',
              'issue': [{'severity': 'error', 'code': 'login',
                         'details': {'text': 'client_id cid-12345 token expired'}}]}
        result, status = sanitize_upstream_error(self._resp(401, oo))
        assert status == 502
        assert result['issue'][0]['code'] == 'security'
        assert 'cid-12345' not in json.dumps(result)

    def test_caller_attributable_statuses_pass_through(self):
        for upstream, expected in ((400, 400), (404, 404), (422, 422),
                                   (429, 429), (500, 502), (503, 502)):
            _, status = sanitize_upstream_error(self._resp(upstream, json_error=True))
            assert status == expected, f'HTTP {upstream} should map to {expected}'

    def test_unreachable_outcome_discloses_type_only(self):
        exc = ConnectionError('https://user:pass@internal:5432 refused')
        result, status = upstream_unreachable_outcome(exc)
        assert status == 502
        assert result['issue'][0]['code'] == 'transient'
        assert 'ConnectionError' in result['issue'][0]['details']['text']
        assert 'internal:5432' not in json.dumps(result)

    def test_malformed_issue_shapes_do_not_crash(self):
        # Whole-array malformations AND per-field malformations (unhashable
        # severity/code would raise inside a membership test).
        bad_arrays = (None, 'a string', {'severity': 'error'}, 42,
                      [None, 'x', 42, {'severity': 'error', 'code': 'invalid'}])
        bad_fields = (
            [{'severity': [], 'code': {}}],
            [{'severity': {'x': 1}, 'code': ['a']}],
            [{'severity': 5, 'code': 5, 'details': {'text': ['not', 'a', 'string']}}],
        )
        for bad_issue in (*bad_arrays, *bad_fields):
            oo = {'resourceType': 'OperationOutcome', 'issue': bad_issue}
            result, status = sanitize_upstream_error(self._resp(400, oo))
            assert status == 400
            assert result['resourceType'] == 'OperationOutcome'
            assert len(result['issue']) >= 1
            # non-string severity/code fall back to safe defaults
            for issue in result['issue']:
                assert issue['severity'] in _SAFE_SEVERITIES
                assert isinstance(issue['code'], str)

    def test_oversized_error_body_is_not_parsed(self):
        resp = self._resp(400, {'resourceType': 'OperationOutcome',
                                'issue': [{'severity': 'error', 'code': 'invalid',
                                           'details': {'text': 'should not appear'}}]})
        resp.content = b'x' * 2_000_000
        result, status = sanitize_upstream_error(resp)
        assert status == 400
        assert 'should not appear' not in json.dumps(result)
        resp.json.assert_not_called()

    def test_5xx_body_text_is_never_forwarded(self):
        oo = {'resourceType': 'OperationOutcome',
              'issue': [{'severity': 'error', 'code': 'exception',
                         'details': {'text': 'NullPointerException at PatientDao.java:88'}}]}
        resp = self._resp(500, oo)
        result, status = sanitize_upstream_error(resp)
        assert status == 502
        assert 'PatientDao' not in json.dumps(result)
        assert result['issue'][0]['details']['text'] == 'The upstream FHIR server had a transient error.'
        resp.json.assert_not_called()  # 5xx bodies are never parsed at all

    def test_caller_auth_401_passes_through_for_sharp(self):
        """SHARP mode forwards the CALLER's SMART token — an upstream 401
        belongs to the caller, who must see the status to re-authenticate.
        The code passes through; the upstream text still does not."""
        oo = {'resourceType': 'OperationOutcome',
              'issue': [{'severity': 'error', 'code': 'expired',
                         'details': {'text': 'Token expired for user jsmith@clinic.internal'}}]}
        result, status = sanitize_upstream_error(self._resp(401, oo), caller_auth=True)
        assert status == 401  # caller can see the 401 and re-auth
        assert result['issue'][0]['code'] == 'expired'
        assert 'jsmith@clinic.internal' not in json.dumps(result)  # text still not forwarded


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

        with patch('r6.routes.get_proxy_for_request') as mock_get:
            mock_proxy = MagicMock()
            mock_proxy.read.return_value = (upstream_patient, 200)
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

        with patch('r6.routes.get_proxy_for_request') as mock_get:
            mock_proxy = MagicMock()
            mock_proxy.read.return_value = (None, 404)
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

        with patch('r6.routes.get_proxy_for_request') as mock_get:
            mock_proxy = MagicMock()
            mock_proxy.search.return_value = (upstream_bundle, 200)
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

        with patch('r6.routes.get_proxy_for_request') as mock_get:
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

    def test_update_via_proxy_does_not_require_local_shadow_row(self, auth_headers):
        """An upstream-only resource can be updated without a local DB copy."""
        patient = {
            'resourceType': 'Patient',
            'id': 'upstream-only-id',
            'name': [{'family': 'Updated'}],
        }

        with patch('r6.routes.get_proxy_for_request') as mock_get:
            mock_proxy = MagicMock()
            mock_proxy.update.return_value = (patient, 200)
            mock_get.return_value = mock_proxy

            resp = self.client.put(
                '/r6/fhir/Patient/upstream-only-id',
                json=patient,
                headers={
                    **auth_headers,
                    'X-Human-Confirmed': 'true',
                    'If-Match': 'W/"7"',
                },
            )

        assert resp.status_code == 200
        assert resp.get_json()['_source'] == 'upstream'
        mock_proxy.update.assert_called_once_with(
            'Patient', 'upstream-only-id', {
                'resourceType': 'Patient',
                'id': 'upstream-only-id',
                'name': [{'family': 'Updated'}],
            }, 'W/"7"'
        )

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
