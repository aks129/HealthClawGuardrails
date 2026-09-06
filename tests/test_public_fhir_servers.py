"""
Integration tests against public FHIR servers. Opt-in, never default.

Tests the FHIRUpstreamProxy and Flask route guardrails against real public
FHIR servers. They need network access, they are slow, and they assert
against shared sandboxes nobody here controls, so they are not part of the
default suite (#635): a default `pytest` run makes no network call, here or
anywhere (tests/test_no_network_at_import.py pins the import-time half).
The guardrail properties these exercise are pinned against a local server
by tests/test_guardrail_conformance.py; this file checks the proxy against
real upstreams and is evidence, not a gate.

Tested servers:
  - HAPI FHIR R4: https://hapi.fhir.org/baseR4
  - SMART Health IT R4: https://r4.smarthealthit.org

Run with:
  HEALTHCLAW_LIVE_FHIR_TESTS=1 python -m pytest tests/test_public_fhir_servers.py -v
  HEALTHCLAW_LIVE_FHIR_TESTS=1 python -m pytest tests/test_public_fhir_servers.py -v -k hapi
  HEALTHCLAW_LIVE_FHIR_TESTS=1 python -m pytest tests/test_public_fhir_servers.py -v -k smart
"""

import json
import os
import pytest

from r6.fhir_proxy import FHIRUpstreamProxy, reset_proxy

LIVE_FLAG = 'HEALTHCLAW_LIVE_FHIR_TESTS'

# Every test in this module is skipped at collection unless the flag is set,
# so the default suite never reaches the probes below.
pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_FLAG, '').strip().lower() not in ('1', 'true', 'yes'),
    reason=f'live public FHIR servers; set {LIVE_FLAG}=1 to run')

# --- Public FHIR servers ---
HAPI_FHIR_R4 = 'https://hapi.fhir.org/baseR4'
SMART_HEALTH_IT = 'https://r4.smarthealthit.org'

LOCAL_BASE = 'http://localhost:5000/r6/fhir'


def _server_reachable(url: str) -> bool:
    """Quick check if a FHIR server is reachable."""
    try:
        import httpx
        resp = httpx.get(f'{url}/metadata', params={'_summary': 'true'}, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


# Reachability is probed lazily, once per session, by the first opted-in test
# that asks; nothing runs at import. A probe that fails skips every test that
# depends on that server, with the reason naming it. (The probe is still a
# point-in-time answer: a server that goes away mid-run fails the tests that
# reach it after that, which an opt-in run is allowed to report.)

@pytest.fixture(scope='session')
def hapi_reachable():
    if not _server_reachable(HAPI_FHIR_R4):
        pytest.skip('HAPI FHIR R4 server unreachable')


@pytest.fixture(scope='session')
def smart_reachable():
    if not _server_reachable(SMART_HEALTH_IT):
        pytest.skip('SMART Health IT server unreachable')


skip_hapi = pytest.mark.usefixtures('hapi_reachable')
skip_smart = pytest.mark.usefixtures('smart_reachable')


# ============================================================================
# Direct proxy client tests — HAPI FHIR R4
# ============================================================================

class TestHAPIFHIRProxy:
    """Test FHIRUpstreamProxy directly against HAPI FHIR R4."""

    @pytest.fixture(autouse=True)
    def setup_proxy(self):
        self.proxy = FHIRUpstreamProxy(
            upstream_url=HAPI_FHIR_R4,
            local_base_url=LOCAL_BASE,
        )
        yield
        self.proxy.close()

    @skip_hapi
    def test_health_check(self):
        """HAPI server reports connected status via /metadata."""
        result = self.proxy.healthy()
        assert result['status'] == 'connected'
        assert result['upstream_url'] == HAPI_FHIR_R4
        assert result['fhir_version'] in ('4.0.1', '4.0.2')
        assert 'software' in result

    @skip_hapi
    def test_patient_search(self):
        """Search for patients returns a valid Bundle."""
        result, _ = self.proxy.search('Patient', {'_count': '3'})
        assert result['resourceType'] == 'Bundle'
        assert result['type'] == 'searchset'
        # total and entry are both optional in a searchset; public HAPI's
        # offset paging can return a first page carrying only link.
        assert 'total' in result or 'entry' in result or 'link' in result

    @skip_hapi
    def test_patient_search_with_params(self):
        """Search with specific parameters is forwarded correctly."""
        result, _ = self.proxy.search('Patient', {'_count': '2', '_summary': 'true'})
        assert result['resourceType'] == 'Bundle'
        # Summary mode returns fewer fields per resource
        if result.get('entry'):
            resource = result['entry'][0].get('resource', {})
            assert resource.get('resourceType') == 'Patient'

    @skip_hapi
    def test_observation_search(self):
        """Search for observations returns valid results."""
        result, _ = self.proxy.search('Observation', {'_count': '3'})
        assert result['resourceType'] == 'Bundle'
        assert result['type'] == 'searchset'

    @skip_hapi
    def test_url_rewriting_on_real_response(self):
        """Upstream URLs in real HAPI responses are rewritten to local proxy."""
        result, _ = self.proxy.search('Patient', {'_count': '1'})
        serialized = json.dumps(result)
        # No upstream URLs should leak
        assert 'hapi.fhir.org/baseR4' not in serialized
        # If there are links, they should point to our local proxy
        for link in result.get('link', []):
            if 'url' in link:
                assert LOCAL_BASE in link['url'] or 'hapi.fhir.org' not in link['url']

    @skip_hapi
    def test_read_nonexistent_patient(self):
        """Reading a nonexistent patient returns None."""
        result, status = self.proxy.read('Patient', 'definitely-does-not-exist-xyz-99999')
        assert result is None
        assert status == 404

    @skip_hapi
    def test_condition_search(self):
        """Search for conditions works against HAPI."""
        result, _ = self.proxy.search('Condition', {'_count': '2'})
        assert result['resourceType'] == 'Bundle'

    @skip_hapi
    def test_metadata_operation(self):
        """Fetching metadata via operation method works."""
        data, status = self.proxy.operation('/metadata', method='GET',
                                            params={'_summary': 'true'})
        assert status == 200
        assert data is not None
        assert data['resourceType'] == 'CapabilityStatement'


# ============================================================================
# Direct proxy client tests — SMART Health IT R4
# ============================================================================

class TestSMARTHealthITProxy:
    """Test FHIRUpstreamProxy directly against SMART Health IT R4."""

    @pytest.fixture(autouse=True)
    def setup_proxy(self):
        self.proxy = FHIRUpstreamProxy(
            upstream_url=SMART_HEALTH_IT,
            local_base_url=LOCAL_BASE,
        )
        yield
        self.proxy.close()

    @skip_smart
    def test_health_check(self):
        """SMART Health IT reports connected status via /metadata."""
        result = self.proxy.healthy()
        assert result['status'] == 'connected'
        assert result['upstream_url'] == SMART_HEALTH_IT
        assert 'fhir_version' in result

    @skip_smart
    def test_patient_search(self):
        """Search for patients returns a valid Bundle."""
        result, _ = self.proxy.search('Patient', {'_count': '3'})
        assert result['resourceType'] == 'Bundle'
        assert result['type'] == 'searchset'

    @skip_smart
    def test_patient_search_by_name(self):
        """Search by name parameter forwards correctly."""
        result, _ = self.proxy.search('Patient', {'name': 'Smith', '_count': '3'})
        assert result['resourceType'] == 'Bundle'

    @skip_smart
    def test_observation_search(self):
        """Search for observations on SMART Health IT."""
        result, _ = self.proxy.search('Observation', {'_count': '3'})
        assert result['resourceType'] == 'Bundle'

    @skip_smart
    def test_url_rewriting_on_real_response(self):
        """SMART Health IT URLs are rewritten to local proxy."""
        result, _ = self.proxy.search('Patient', {'_count': '1'})
        serialized = json.dumps(result)
        assert 'r4.smarthealthit.org' not in serialized

    @skip_smart
    def test_read_nonexistent_returns_none(self):
        """Reading a nonexistent resource returns None."""
        result, status = self.proxy.read('Patient', 'nonexistent-id-xyz-99999')
        assert result is None
        assert status == 404

    @skip_smart
    def test_encounter_search(self):
        """Search for encounters on SMART Health IT."""
        result, _ = self.proxy.search('Encounter', {'_count': '2'})
        assert result['resourceType'] == 'Bundle'

    @skip_smart
    def test_metadata_operation(self):
        """CapabilityStatement via operation method."""
        data, status = self.proxy.operation('/metadata', method='GET',
                                            params={'_summary': 'true'})
        assert status == 200
        assert data is not None
        assert data['resourceType'] == 'CapabilityStatement'


# ============================================================================
# Cross-server comparison tests
# ============================================================================

class TestCrossServerComparison:
    """Compare behavior across both public FHIR servers."""

    @pytest.fixture(autouse=True)
    def setup_proxies(self):
        self.hapi = FHIRUpstreamProxy(upstream_url=HAPI_FHIR_R4, local_base_url=LOCAL_BASE)
        self.smart = FHIRUpstreamProxy(upstream_url=SMART_HEALTH_IT, local_base_url=LOCAL_BASE)
        yield
        self.hapi.close()
        self.smart.close()

    @skip_hapi
    @skip_smart
    def test_both_servers_return_valid_bundles(self):
        """Both servers return structurally valid search Bundles."""
        hapi_result, _ = self.hapi.search('Patient', {'_count': '2'})
        smart_result, _ = self.smart.search('Patient', {'_count': '2'})

        for result in [hapi_result, smart_result]:
            assert result['resourceType'] == 'Bundle'
            assert result['type'] == 'searchset'
            # total and entry are both optional in a searchset; public HAPI's
            # offset paging can return a first page carrying only link.
            assert 'entry' in result or 'total' in result or 'link' in result

    @skip_hapi
    @skip_smart
    def test_both_servers_metadata_connected(self):
        """Both servers report healthy via /metadata."""
        hapi_health = self.hapi.healthy()
        smart_health = self.smart.healthy()

        assert hapi_health['status'] == 'connected'
        assert smart_health['status'] == 'connected'

    @skip_hapi
    @skip_smart
    def test_url_rewriting_prevents_upstream_leakage(self):
        """Neither server's URLs leak through the proxy."""
        hapi_result, _ = self.hapi.search('Patient', {'_count': '1'})
        smart_result, _ = self.smart.search('Patient', {'_count': '1'})

        hapi_json = json.dumps(hapi_result)
        smart_json = json.dumps(smart_result)

        assert 'hapi.fhir.org' not in hapi_json
        assert 'r4.smarthealthit.org' not in smart_json


# ============================================================================
# Flask route integration tests with real upstream servers
# ============================================================================

class TestFlaskGuardrailsWithHAPI:
    """Test Flask routes with real HAPI FHIR upstream — guardrails applied."""

    @pytest.fixture(autouse=True)
    def setup(self, app, client, tenant_headers, auth_headers):
        self.app = app
        self.client = client
        self.tenant_headers = tenant_headers
        self.auth_headers = auth_headers
        # Configure upstream
        os.environ['FHIR_UPSTREAM_URL'] = HAPI_FHIR_R4
        os.environ['FHIR_LOCAL_BASE_URL'] = LOCAL_BASE
        reset_proxy()
        yield
        reset_proxy()
        os.environ.pop('FHIR_UPSTREAM_URL', None)
        os.environ.pop('FHIR_LOCAL_BASE_URL', None)

    @skip_hapi
    def test_health_reports_upstream_connected(self):
        """Health endpoint reports HAPI upstream as connected."""
        resp = self.client.get('/r6/fhir/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['mode'] == 'upstream'
        assert data['checks']['upstream']['status'] == 'connected'
        assert 'hapi.fhir.org' in data['checks']['upstream']['upstream_url']

    @skip_hapi
    def test_patient_search_with_redaction(self):
        """Patient search via HAPI returns redacted results."""
        resp = self.client.get('/r6/fhir/Patient?_count=3',
                               headers=self.tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Bundle'
        assert data.get('_source') == 'upstream'

        # Verify redaction applied to upstream data
        for entry in data.get('entry', []):
            resource = entry.get('resource', {})
            if resource.get('resourceType') != 'Patient':
                continue
            # Identifier values are removed
            for ident in resource.get('identifier', []):
                assert 'value' not in ident
            # Address lines should be stripped
            for addr in resource.get('address', []):
                assert 'line' not in addr
            # Telecom should be redacted
            for tel in resource.get('telecom', []):
                if tel.get('value'):
                    assert tel['value'] == '[Redacted]'
            # Names should be truncated to initials
            for name_entry in resource.get('name', []):
                for given in name_entry.get('given', []):
                    assert len(given) <= 2

    @skip_hapi
    def test_no_upstream_urls_leak_through_routes(self):
        """Upstream HAPI URLs never appear in Flask responses."""
        resp = self.client.get('/r6/fhir/Patient?_count=2',
                               headers=self.tenant_headers)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'hapi.fhir.org' not in body

    @skip_hapi
    def test_audit_trail_records_upstream_access(self):
        """Reading from upstream creates audit events."""
        # Trigger an upstream read
        self.client.get('/r6/fhir/Patient?_count=1',
                        headers=self.tenant_headers)

        # Check audit trail
        resp = self.client.get('/r6/fhir/AuditEvent?_count=5',
                               headers=self.tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Bundle'
        # Should have at least one audit event
        assert len(data.get('entry', [])) >= 1

    @skip_hapi
    def test_metadata_shows_upstream_mode(self):
        """CapabilityStatement describes upstream proxy mode."""
        resp = self.client.get('/r6/fhir/metadata')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'upstream' in data['implementation']['description'].lower()

    @skip_hapi
    def test_observation_search_via_route(self):
        """Observation search through Flask route against HAPI."""
        resp = self.client.get('/r6/fhir/Observation?_count=3',
                               headers=self.tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Bundle'
        assert data.get('_source') == 'upstream'


class TestFlaskGuardrailsWithSMART:
    """Test Flask routes with real SMART Health IT upstream — guardrails applied."""

    @pytest.fixture(autouse=True)
    def setup(self, app, client, tenant_headers, auth_headers):
        self.app = app
        self.client = client
        self.tenant_headers = tenant_headers
        self.auth_headers = auth_headers
        os.environ['FHIR_UPSTREAM_URL'] = SMART_HEALTH_IT
        os.environ['FHIR_LOCAL_BASE_URL'] = LOCAL_BASE
        reset_proxy()
        yield
        reset_proxy()
        os.environ.pop('FHIR_UPSTREAM_URL', None)
        os.environ.pop('FHIR_LOCAL_BASE_URL', None)

    @skip_smart
    def test_health_reports_upstream_connected(self):
        """Health endpoint reports SMART Health IT as connected."""
        resp = self.client.get('/r6/fhir/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['mode'] == 'upstream'
        assert data['checks']['upstream']['status'] == 'connected'

    @skip_smart
    def test_patient_search_with_redaction(self):
        """Patient search via SMART returns redacted results."""
        resp = self.client.get('/r6/fhir/Patient?_count=3',
                               headers=self.tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Bundle'
        assert data.get('_source') == 'upstream'

        # Verify redaction applied
        for entry in data.get('entry', []):
            resource = entry.get('resource', {})
            if resource.get('resourceType') != 'Patient':
                continue
            # Address lines stripped
            for addr in resource.get('address', []):
                assert 'line' not in addr
            # Telecom redacted
            for tel in resource.get('telecom', []):
                if tel.get('value'):
                    assert tel['value'] == '[Redacted]'

    @skip_smart
    def test_no_upstream_urls_leak_through_routes(self):
        """SMART Health IT URLs never appear in Flask responses."""
        resp = self.client.get('/r6/fhir/Patient?_count=2',
                               headers=self.tenant_headers)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'r4.smarthealthit.org' not in body

    @skip_smart
    def test_observation_search_via_route(self):
        """Observation search through Flask route against SMART Health IT."""
        resp = self.client.get('/r6/fhir/Observation?_count=3',
                               headers=self.tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Bundle'
        assert data.get('_source') == 'upstream'

    @skip_smart
    def test_metadata_shows_upstream_mode(self):
        """CapabilityStatement describes upstream mode with SMART."""
        resp = self.client.get('/r6/fhir/metadata')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'upstream' in data['implementation']['description'].lower()

    @skip_smart
    def test_search_by_name(self):
        """Name-based patient search forwards to SMART correctly."""
        resp = self.client.get('/r6/fhir/Patient?name=Adams&_count=3',
                               headers=self.tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Bundle'
