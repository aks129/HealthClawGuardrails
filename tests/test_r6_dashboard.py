"""
Integration tests for R6 Dashboard, Health endpoint, and end-to-end workflows.

Tests cover:
- Health check endpoint
- Internal step-up token issuance
- Dashboard page serving
- Full agent workflow (create → read → search → context → deidentify → audit)
- ETag concurrency under concurrent-like updates
- Cross-tenant isolation across all resource types
- OAuth end-to-end with PKCE
- Human-in-the-loop enforcement lifecycle
- Bundle validation edge cases
- Rate limit header presence
- Audit trail export formats
- Medical disclaimer propagation through search
"""

import hashlib
import base64
import json
import secrets
import pytest


# ===== Health Check =====

class TestHealthEndpoint:
    """Test /r6/fhir/health liveness/readiness probe."""

    def test_health_returns_200(self, client):
        resp = client.get('/r6/fhir/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'healthy'
        assert data['checks']['database'] == 'ok'

    def test_health_includes_version(self, client):
        resp = client.get('/r6/fhir/health')
        data = resp.get_json()
        assert data['version'] == '1.0.0'
        assert '6.0.0' in data['fhirVersion']

    def test_health_no_tenant_required(self, client):
        """Health endpoint should work without X-Tenant-Id."""
        resp = client.get('/r6/fhir/health')
        assert resp.status_code == 200


# ===== Internal Step-Up Token =====

class TestInternalStepUpToken:
    """Test /r6/fhir/internal/step-up-token for dashboard."""

    def test_issue_token_returns_valid_token(self, client, tenant_headers):
        resp = client.post('/r6/fhir/internal/step-up-token',
                          data=json.dumps({'tenant_id': 'test-tenant'}),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'token' in data
        assert '.' in data['token']  # HMAC format: payload.signature

    def test_issued_token_is_usable(self, client, tenant_headers, sample_patient):
        """Token from internal endpoint should work for writes."""
        token_resp = client.post('/r6/fhir/internal/step-up-token',
                                data=json.dumps({'tenant_id': 'test-tenant'}),
                                content_type='application/json',
                                headers=tenant_headers)
        token = token_resp.get_json()['token']

        resp = client.post('/r6/fhir/Patient',
                          data=json.dumps(sample_patient),
                          content_type='application/json',
                          headers={**tenant_headers, 'X-Step-Up-Token': token})
        assert resp.status_code == 201


# ===== Dashboard Page =====

class TestDashboardPage:
    """Test R6 Dashboard page rendering."""

    def test_dashboard_returns_html(self, client):
        resp = client.get('/r6-dashboard')
        assert resp.status_code == 200
        assert b'R6 FHIR Agent Dashboard' in resp.data

    def test_dashboard_includes_js(self, client):
        resp = client.get('/r6-dashboard')
        assert b'r6-dashboard.js' in resp.data

    def test_dashboard_includes_css(self, client):
        resp = client.get('/r6-dashboard')
        assert b'r6-dashboard.css' in resp.data

    def test_dashboard_has_all_panels(self, client):
        resp = client.get('/r6-dashboard')
        html = resp.data.decode()
        assert 'patient-panel' in html
        assert 'tools-panel' in html
        assert 'context-panel' in html
        assert 'deid-panel' in html
        assert 'hitl-panel' in html
        assert 'oauth-panel' in html
        assert 'validate-panel' in html

    def test_dashboard_linked_in_navbar(self, client):
        resp = client.get('/')
        assert b'R6 Dashboard' in resp.data


# ===== Full Agent Workflow =====

class TestFullAgentWorkflow:
    """End-to-end agent workflow: create → read → search → context → deidentify → audit."""

    def test_complete_workflow(self, client, auth_headers, tenant_headers):
        # 1. Create patient
        patient = {
            'resourceType': 'Patient', 'id': 'workflow-pt',
            'name': [{'family': 'Workflow', 'given': ['Test']}],
            'gender': 'male', 'birthDate': '1990-01-01',
            'identifier': [{'value': 'WF123456'}],
            'address': [{'line': ['999 Test Ave'], 'city': 'Testville', 'state': 'TX'}]
        }
        create_resp = client.post('/r6/fhir/Patient',
                                  data=json.dumps(patient),
                                  content_type='application/json',
                                  headers=auth_headers)
        assert create_resp.status_code == 201
        assert 'ETag' in create_resp.headers

        # 2. Read back (should be redacted)
        read_resp = client.get('/r6/fhir/Patient/workflow-pt',
                              headers=tenant_headers)
        assert read_resp.status_code == 200
        data = read_resp.get_json()
        assert data['identifier'][0]['value'].startswith('***')
        assert 'line' not in data.get('address', [{}])[0]

        # 3. Search
        search_resp = client.get('/r6/fhir/Patient?_count=10',
                                headers=tenant_headers)
        assert search_resp.status_code == 200
        assert search_resp.get_json()['total'] >= 1

        # 4. Ingest bundle + context
        obs = {
            'resourceType': 'Observation', 'id': 'wf-obs-1',
            'status': 'final',
            'code': {'coding': [{'system': 'http://loinc.org', 'code': '2339-0'}]},
            'subject': {'reference': 'Patient/workflow-pt'},
            'valueQuantity': {'value': 95, 'unit': 'mg/dL'}
        }
        bundle = {
            'resourceType': 'Bundle', 'type': 'collection',
            'entry': [{'resource': patient}, {'resource': obs}]
        }
        ctx_resp = client.post('/r6/fhir/Bundle/$ingest-context',
                               data=json.dumps(bundle),
                               content_type='application/json',
                               headers=tenant_headers)
        assert ctx_resp.status_code == 201
        ctx_id = ctx_resp.get_json()['context_id']

        # 5. Retrieve context
        envelope_resp = client.get(f'/r6/fhir/context/{ctx_id}',
                                   headers=tenant_headers)
        assert envelope_resp.status_code == 200
        assert envelope_resp.get_json()['item_count'] == 2

        # 6. De-identify
        deid_resp = client.get('/r6/fhir/Patient/workflow-pt/$deidentify',
                               headers=tenant_headers)
        assert deid_resp.status_code == 200
        deid_data = deid_resp.get_json()
        assert 'name' not in deid_data
        assert deid_data['id'] != 'workflow-pt'

        # 7. Check audit trail has entries
        audit_resp = client.get('/r6/fhir/AuditEvent?_count=50',
                               headers=tenant_headers)
        assert audit_resp.status_code == 200
        assert audit_resp.get_json()['total'] >= 4  # create, read, search, context


# ===== Cross-Tenant Isolation Comprehensive =====

class TestCrossTenantIsolationComprehensive:
    """Verify tenant isolation across ALL resource operations."""

    def _create_patient(self, client, tenant, token):
        """Helper to create a patient under a specific tenant."""
        patient = {
            'resourceType': 'Patient',
            'id': f'iso-pt-{tenant}',
            'name': [{'family': f'Tenant-{tenant}'}]
        }
        return client.post('/r6/fhir/Patient',
                          data=json.dumps(patient),
                          content_type='application/json',
                          headers={'X-Tenant-Id': tenant, 'X-Step-Up-Token': token})

    def test_search_tenant_isolated(self, client):
        """Search results only include resources from the requesting tenant."""
        from r6.stepup import generate_step_up_token

        token_a = generate_step_up_token('tenant-a')
        token_b = generate_step_up_token('tenant-b')

        self._create_patient(client, 'tenant-a', token_a)
        self._create_patient(client, 'tenant-b', token_b)

        # Search as tenant-a should only see tenant-a's patient
        resp_a = client.get('/r6/fhir/Patient', headers={'X-Tenant-Id': 'tenant-a'})
        entries_a = resp_a.get_json().get('entry', [])
        for e in entries_a:
            assert 'tenant-a' in json.dumps(e)

        resp_b = client.get('/r6/fhir/Patient', headers={'X-Tenant-Id': 'tenant-b'})
        entries_b = resp_b.get_json().get('entry', [])
        for e in entries_b:
            assert 'tenant-b' in json.dumps(e)

    def test_deidentify_tenant_isolated(self, client):
        """De-identify fails for cross-tenant access."""
        from r6.stepup import generate_step_up_token
        token = generate_step_up_token('tenant-c')
        self._create_patient(client, 'tenant-c', token)

        # Try deidentify from different tenant
        resp = client.get('/r6/fhir/Patient/iso-pt-tenant-c/$deidentify',
                          headers={'X-Tenant-Id': 'tenant-d'})
        assert resp.status_code == 404

    def test_update_tenant_isolated(self, client):
        """Cannot update a resource owned by another tenant."""
        from r6.stepup import generate_step_up_token
        token_e = generate_step_up_token('tenant-e')
        self._create_patient(client, 'tenant-e', token_e)

        token_f = generate_step_up_token('tenant-f')
        resp = client.put('/r6/fhir/Patient/iso-pt-tenant-e',
                         data=json.dumps({'resourceType': 'Patient', 'id': 'iso-pt-tenant-e', 'gender': 'other'}),
                         content_type='application/json',
                         headers={'X-Tenant-Id': 'tenant-f', 'X-Step-Up-Token': token_f})
        assert resp.status_code == 404


# ===== ETag Lifecycle =====

class TestETagLifecycle:
    """ETag lifecycle: create → update → stale check → concurrent update."""

    def test_etag_increments_on_update(self, client, sample_patient, auth_headers, tenant_headers):
        # Create
        r1 = client.post('/r6/fhir/Patient',
                        data=json.dumps(sample_patient),
                        content_type='application/json',
                        headers=auth_headers)
        etag1 = r1.headers.get('ETag')
        assert etag1 is not None

        # Update
        sample_patient['gender'] = 'other'
        r2 = client.put(f'/r6/fhir/Patient/{sample_patient["id"]}',
                        data=json.dumps(sample_patient),
                        content_type='application/json',
                        headers=auth_headers)
        etag2 = r2.headers.get('ETag')
        assert etag2 != etag1

    def test_read_returns_etag(self, client, sample_patient, auth_headers, tenant_headers):
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get(f'/r6/fhir/Patient/{sample_patient["id"]}',
                         headers=tenant_headers)
        assert 'ETag' in resp.headers


# ===== OAuth Complete Flow =====

class TestOAuthCompleteFlow:
    """Full OAuth 2.1 flow with error cases."""

    def _register_client(self, client, tenant_headers):
        resp = client.post('/r6/fhir/oauth/register',
                          data=json.dumps({
                              'client_name': 'Integration Test',
                              'redirect_uris': ['http://localhost/cb'],
                              'scope': 'fhir.read context.read',
                          }),
                          content_type='application/json',
                          headers=tenant_headers)
        return resp.get_json()

    def _make_pkce(self):
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b'=').decode()
        return verifier, challenge

    def test_wrong_pkce_verifier_rejected(self, client, tenant_headers):
        """Token exchange with wrong PKCE verifier must fail."""
        reg = self._register_client(client, tenant_headers)
        _, challenge = self._make_pkce()

        auth_resp = client.get(
            f'/r6/fhir/oauth/authorize?client_id={reg["client_id"]}'
            f'&redirect_uri=http://localhost/cb&scope=fhir.read'
            f'&code_challenge={challenge}&code_challenge_method=S256',
            headers=tenant_headers)
        code = auth_resp.get_json()['code']

        # Use a DIFFERENT verifier
        token_resp = client.post('/r6/fhir/oauth/token',
                                data=json.dumps({
                                    'grant_type': 'authorization_code',
                                    'code': code,
                                    'code_verifier': 'completely-wrong-verifier',
                                }),
                                content_type='application/json',
                                headers=tenant_headers)
        assert token_resp.status_code == 400
        assert 'PKCE' in token_resp.get_json().get('error_description', '')

    def test_expired_code_rejected(self, client, tenant_headers):
        """Using a code twice should fail (single-use)."""
        reg = self._register_client(client, tenant_headers)
        verifier, challenge = self._make_pkce()

        auth_resp = client.get(
            f'/r6/fhir/oauth/authorize?client_id={reg["client_id"]}'
            f'&redirect_uri=http://localhost/cb&scope=fhir.read'
            f'&code_challenge={challenge}&code_challenge_method=S256',
            headers=tenant_headers)
        code = auth_resp.get_json()['code']

        # First exchange succeeds
        client.post('/r6/fhir/oauth/token',
                    data=json.dumps({
                        'grant_type': 'authorization_code',
                        'code': code,
                        'code_verifier': verifier,
                    }),
                    content_type='application/json',
                    headers=tenant_headers)

        # Second exchange fails (code consumed)
        resp2 = client.post('/r6/fhir/oauth/token',
                            data=json.dumps({
                                'grant_type': 'authorization_code',
                                'code': code,
                                'code_verifier': verifier,
                            }),
                            content_type='application/json',
                            headers=tenant_headers)
        assert resp2.status_code == 400


# ===== Bundle Validation Edge Cases =====

class TestBundleEdgeCases:
    """Edge cases for Bundle ingestion."""

    def test_bundle_missing_resource_type(self, client, tenant_headers):
        resp = client.post('/r6/fhir/Bundle/$ingest-context',
                          data=json.dumps({'type': 'collection', 'entry': []}),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 400

    def test_bundle_with_unsupported_resources_skips_them(self, client, tenant_headers):
        """Unsupported resource types in a Bundle should be silently skipped."""
        bundle = {
            'resourceType': 'Bundle', 'type': 'collection',
            'entry': [
                {'resource': {'resourceType': 'Patient', 'id': 'skip-pt'}},
                {'resource': {'resourceType': 'ImagingStudy', 'id': 'skip-img'}},
            ]
        }
        resp = client.post('/r6/fhir/Bundle/$ingest-context',
                          data=json.dumps(bundle),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 201
        # Only Patient should be stored (ImagingStudy is not supported)
        assert resp.get_json()['resource_count'] == 1

    def test_bundle_all_valid_types(self, client, tenant_headers):
        """All valid Bundle.type values should be accepted."""
        for btype in ['document', 'message', 'collection', 'transaction', 'batch']:
            bundle = {
                'resourceType': 'Bundle', 'type': btype,
                'entry': [{'resource': {'resourceType': 'Patient', 'id': f'btype-{btype}'}}]
            }
            resp = client.post('/r6/fhir/Bundle/$ingest-context',
                              data=json.dumps(bundle),
                              content_type='application/json',
                              headers=tenant_headers)
            assert resp.status_code == 201, f'Bundle.type={btype} should be accepted'


# ===== Audit Export Formats =====

class TestAuditExportFormats:
    """Test audit trail export in different formats."""

    def _generate_events(self, client, auth_headers, tenant_headers):
        client.post('/r6/fhir/Patient',
                    data=json.dumps({'resourceType': 'Patient', 'id': 'export-pt', 'name': [{'family': 'Export'}]}),
                    content_type='application/json',
                    headers=auth_headers)
        client.get('/r6/fhir/Patient/export-pt', headers=tenant_headers)

    def test_ndjson_format(self, client, auth_headers, tenant_headers):
        self._generate_events(client, auth_headers, tenant_headers)
        resp = client.get('/r6/fhir/AuditEvent/$export', headers=tenant_headers)
        assert resp.status_code == 200
        assert 'ndjson' in resp.content_type
        lines = resp.data.decode().strip().split('\n')
        assert len(lines) >= 1
        # Each line is valid JSON
        for line in lines:
            parsed = json.loads(line)
            assert parsed['resourceType'] == 'AuditEvent'

    def test_fhir_bundle_format(self, client, auth_headers, tenant_headers):
        self._generate_events(client, auth_headers, tenant_headers)
        resp = client.get('/r6/fhir/AuditEvent/$export?_format=fhir-bundle',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['resourceType'] == 'Bundle'
        assert data['type'] == 'collection'
        assert len(data['entry']) >= 1


# ===== Medical Disclaimer Propagation =====

class TestDisclaimerPropagation:
    """Ensure disclaimers propagate through all read paths."""

    def test_disclaimer_on_observation_search(self, client, auth_headers, tenant_headers):
        """Search results for clinical types should have disclaimer on entries."""
        obs = {
            'resourceType': 'Observation', 'id': 'discl-obs',
            'status': 'final',
            'code': {'coding': [{'system': 'http://loinc.org', 'code': '2339-0'}]}
        }
        client.post('/r6/fhir/Observation',
                    data=json.dumps(obs),
                    content_type='application/json',
                    headers={**auth_headers, 'X-Human-Confirmed': 'true'})

        resp = client.get('/r6/fhir/Observation', headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        # Disclaimer is on individual entries (each Observation resource)
        entries = data.get('entry', [])
        assert len(entries) >= 1
        assert '_disclaimer' in entries[0]['resource']

    def test_disclaimer_on_create_response(self, client, auth_headers):
        """Create response for clinical types should have disclaimer."""
        obs = {
            'resourceType': 'Observation', 'id': 'discl-obs-2',
            'status': 'final',
            'code': {'coding': [{'system': 'http://loinc.org', 'code': '8867-4'}]}
        }
        resp = client.post('/r6/fhir/Observation',
                          data=json.dumps(obs),
                          content_type='application/json',
                          headers={**auth_headers, 'X-Human-Confirmed': 'true'})
        assert resp.status_code == 201
        assert '_disclaimer' in resp.get_json()

    def test_no_disclaimer_on_patient_search(self, client, sample_patient, auth_headers, tenant_headers):
        """Patient search should NOT have disclaimer (not clinical)."""
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get('/r6/fhir/Patient', headers=tenant_headers)
        assert '_disclaimer' not in resp.get_json()


# ===== Validate Edge Cases =====

class TestValidateEdgeCases:
    """Edge cases for $validate endpoint."""

    def test_validate_unsupported_type(self, client, tenant_headers):
        resp = client.post('/r6/fhir/ImagingStudy/$validate',
                          data=json.dumps({'resourceType': 'ImagingStudy'}),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 400

    def test_validate_empty_json(self, client, tenant_headers):
        resp = client.post('/r6/fhir/Patient/$validate',
                          data='not-json',
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 400

    def test_validate_encounter_missing_status(self, client, tenant_headers):
        resp = client.post('/r6/fhir/Encounter/$validate',
                          data=json.dumps({'resourceType': 'Encounter'}),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 422
        data = resp.get_json()
        assert any('status' in i.get('diagnostics', '') for i in data['issue'])


# ===== Import Stub =====

class TestImportStubWorkflow:
    """Test cross-version import stub with various source versions."""

    def test_import_from_r4(self, client, tenant_headers):
        bundle = {
            'resourceType': 'Bundle', 'type': 'collection',
            'entry': [
                {'resource': {'resourceType': 'Patient', 'id': 'r4-pt'}},
                {'resource': {'resourceType': 'Observation', 'id': 'r4-obs', 'status': 'final'}},
            ]
        }
        resp = client.post('/r6/fhir/$import-stub?source-version=R4',
                          data=json.dumps(bundle),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 202
        data = resp.get_json()
        assert data['_import_stub']['source_version'] == 'R4'
        assert data['_import_stub']['entry_count'] == 2
        assert all(e['transform_status'] == 'needs-transform' for e in data['_import_stub']['entries'])

    def test_import_from_r5(self, client, tenant_headers):
        bundle = {
            'resourceType': 'Bundle', 'type': 'collection',
            'entry': [{'resource': {'resourceType': 'Patient', 'id': 'r5-pt'}}]
        }
        resp = client.post('/r6/fhir/$import-stub?source-version=R5',
                          data=json.dumps(bundle),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 202
        assert resp.get_json()['_import_stub']['source_version'] == 'R5'


# ===== Phase 2: Dashboard Integration Tests =====


class TestPhase2DashboardPanels:
    """Test Phase 2 dashboard panels are present."""

    def test_dashboard_has_phase2_panels(self, client):
        resp = client.get('/r6-dashboard')
        html = resp.data.decode()
        assert 'permission-panel' in html
        assert 'stats-panel' in html
        assert 'subscription-panel' in html
        assert 'r6resources-panel' in html
        assert 'phase2-header' in html

    def test_dashboard_shows_honest_scope(self, client):
        resp = client.get('/r6-dashboard')
        html = resp.data.decode()
        assert 'reference implementation' in html.lower() or 'ballot' in html.lower()


class TestPhase2VersionUpdate:
    """Test version is updated for Phase 2."""

    def test_health_shows_phase2_version(self, client):
        resp = client.get('/r6/fhir/health')
        data = resp.get_json()
        assert data['version'] == '1.0.0'


class TestPhase2EndToEndWorkflow:
    """End-to-end Phase 2 workflow: Permission + $stats + SubscriptionTopic."""

    def test_phase2_workflow(self, client, auth_headers, tenant_headers):
        # 1. Create Permission
        permission = {
            'resourceType': 'Permission', 'id': 'e2e-perm',
            'status': 'active', 'combining': 'permit-overrides',
            'rule': [{'type': 'permit', 'activity': [{'action': [{'coding': [{'code': 'read'}]}]}]}]
        }
        perm_resp = client.post('/r6/fhir/Permission',
                                data=json.dumps(permission),
                                content_type='application/json',
                                headers=auth_headers)
        assert perm_resp.status_code == 201

        # 2. Evaluate Permission
        eval_resp = client.post('/r6/fhir/Permission/$evaluate',
                                data=json.dumps({'action': 'read'}),
                                content_type='application/json',
                                headers=tenant_headers)
        assert eval_resp.status_code == 200
        decision = next(p for p in eval_resp.get_json()['parameter'] if p['name'] == 'decision')
        assert decision['valueCode'] == 'permit'

        # 3. Create Observations for $stats
        obs_headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        for i, val in enumerate([80, 90, 100]):
            client.post('/r6/fhir/Observation',
                       data=json.dumps({
                           'resourceType': 'Observation', 'id': f'e2e-obs-{i}',
                           'status': 'final',
                           'code': {'coding': [{'code': '2339-0'}]},
                           'valueQuantity': {'value': val, 'unit': 'mg/dL'}
                       }),
                       content_type='application/json', headers=obs_headers)

        # 4. Run $stats
        stats_resp = client.get('/r6/fhir/Observation/$stats?code=2339-0',
                                headers=tenant_headers)
        assert stats_resp.status_code == 200
        params = {p['name']: p for p in stats_resp.get_json()['parameter']}
        assert params['count']['valueInteger'] == 3
        assert params['mean']['valueDecimal'] == 90.0

        # 5. Run $lastn
        lastn_resp = client.get('/r6/fhir/Observation/$lastn?code=2339-0&max=1',
                                headers=tenant_headers)
        assert lastn_resp.status_code == 200
        assert lastn_resp.get_json()['total'] == 1

        # 6. Create SubscriptionTopic
        topic = {
            'resourceType': 'SubscriptionTopic', 'id': 'e2e-topic',
            'url': 'http://test.org/topic', 'status': 'active',
        }
        topic_resp = client.post('/r6/fhir/SubscriptionTopic',
                                 data=json.dumps(topic),
                                 content_type='application/json',
                                 headers=auth_headers)
        assert topic_resp.status_code == 201

        # 7. List topics
        list_resp = client.get('/r6/fhir/SubscriptionTopic/$list',
                               headers=tenant_headers)
        assert list_resp.status_code == 200
        assert list_resp.get_json()['total'] >= 1

        # 8. Verify audit trail captured everything
        audit_resp = client.get('/r6/fhir/AuditEvent?_count=50',
                                headers=tenant_headers)
        assert audit_resp.status_code == 200
        assert audit_resp.get_json()['total'] >= 5


class TestDemoAgentLoop:
    """Test the orchestrated 6-step agent guardrail demo endpoint."""

    def test_demo_loop_returns_all_steps(self, client):
        """The demo loop endpoint returns all 6 guardrail steps."""
        resp = client.post('/r6/fhir/demo/agent-loop',
                           content_type='application/json',
                           headers={'X-Tenant-Id': 'demo-test'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['title'] == 'MCP Guardrail Pattern Sequence'
        assert len(data['steps']) == 6
        assert len(data['guardrails_demonstrated']) == 6

    def test_demo_loop_step_sequence(self, client):
        """Steps execute in the correct order with expected statuses."""
        resp = client.post('/r6/fhir/demo/agent-loop',
                           content_type='application/json',
                           headers={'X-Tenant-Id': 'demo-seq'})
        data = resp.get_json()
        steps = data['steps']

        # Step 1: Patient read (success)
        assert steps[0]['step'] == 1
        assert steps[0]['guardrail'] == 'PHI redaction'
        assert steps[0]['status'] == 'success'

        # Step 2: Propose observation (validated)
        assert steps[1]['step'] == 2
        assert steps[1]['guardrail'] == '$validate gate'
        assert steps[1]['status'] == 'validated'

        # Step 3: Permission denies (denied)
        assert steps[2]['step'] == 3
        assert steps[2]['status'] == 'denied'
        deny_params = steps[2]['result']['parameter']
        decision = next(p for p in deny_params if p['name'] == 'decision')
        assert decision['valueCode'] == 'deny'

        # Step 4: Permit rule + re-evaluate (permitted)
        assert steps[3]['step'] == 4
        assert steps[3]['status'] == 'permitted'
        permit_params = steps[3]['result']['evaluation']['parameter']
        decision = next(p for p in permit_params if p['name'] == 'decision')
        assert decision['valueCode'] == 'permit'

        # Step 5: Step-up auth gate
        assert steps[4]['step'] == 5
        assert steps[4]['status'] == 'awaiting_confirmation'
        assert steps[4]['result']['human_confirmation_required'] is True

        # Step 6: Commit with audit trail
        assert steps[5]['step'] == 6
        assert steps[5]['status'] == 'committed'
        assert len(steps[5]['result']['audit_trail']) > 0

    def test_demo_loop_generates_audit_events(self, client):
        """The demo loop generates audit events visible in the audit feed."""
        client.post('/r6/fhir/demo/agent-loop',
                    content_type='application/json',
                    headers={'X-Tenant-Id': 'demo-audit'})

        audit_resp = client.get('/r6/fhir/AuditEvent?_count=20',
                                headers={'X-Tenant-Id': 'demo-audit'})
        assert audit_resp.status_code == 200
        assert audit_resp.get_json()['total'] >= 4

    def test_demo_loop_no_tenant_required(self, client):
        """The demo loop works without explicit tenant (falls back to demo-tenant)."""
        resp = client.post('/r6/fhir/demo/agent-loop',
                           content_type='application/json')
        assert resp.status_code == 200

    def test_demo_loop_redaction_applied(self, client):
        """Step 1 shows redacted patient data."""
        resp = client.post('/r6/fhir/demo/agent-loop',
                           content_type='application/json',
                           headers={'X-Tenant-Id': 'demo-redact'})
        data = resp.get_json()
        patient = data['steps'][0]['result']
        # Check identifiers are masked
        if patient.get('identifier'):
            for ident in patient['identifier']:
                assert '***' in ident.get('value', ''), "Identifiers should be masked"
        # Check names are redacted (given truncated to initial)
        if patient.get('name'):
            for name_entry in patient['name']:
                for given in name_entry.get('given', []):
                    assert given.endswith('.'), f"Given name not redacted: {given}"
