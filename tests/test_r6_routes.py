"""
Tests for R6 FHIR REST endpoints.
"""

import json
import pytest


class TestR6Metadata:
    """Test /r6/fhir/metadata endpoint (exempt from tenant requirement)."""

    def test_metadata_returns_capability_statement(self, client):
        resp = client.get('/r6/fhir/metadata')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'CapabilityStatement'

    def test_metadata_has_r6_fhir_version(self, client):
        resp = client.get('/r6/fhir/metadata')
        data = resp.get_json()
        assert data['fhirVersion'] == '6.0.0-ballot3'

    def test_metadata_lists_supported_resources(self, client):
        resp = client.get('/r6/fhir/metadata')
        data = resp.get_json()
        rest = data['rest'][0]
        resource_types = [r['type'] for r in rest['resource']]
        assert 'Patient' in resource_types
        assert 'Observation' in resource_types
        assert 'AuditEvent' in resource_types

    def test_metadata_lists_operations(self, client):
        resp = client.get('/r6/fhir/metadata')
        data = resp.get_json()
        ops = data['rest'][0]['operation']
        op_names = [o['name'] for o in ops]
        assert 'validate' in op_names
        assert 'ingest-context' in op_names

    def test_metadata_version_is_current(self, client):
        """CapabilityStatement.software.version must match pyproject.toml."""
        resp = client.get('/r6/fhir/metadata')
        data = resp.get_json()
        assert data['software']['version'] == '1.3.0'


class TestTenantEnforcement:
    """Test mandatory tenant isolation."""

    def test_read_without_tenant_returns_400(self, client):
        resp = client.get('/r6/fhir/Patient/test-1')
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'X-Tenant-Id' in data['issue'][0]['diagnostics']

    def test_create_without_tenant_returns_400(self, client, sample_patient):
        resp = client.post('/r6/fhir/Patient',
                          data=json.dumps(sample_patient),
                          content_type='application/json')
        assert resp.status_code == 400

    def test_metadata_exempt_from_tenant(self, client):
        resp = client.get('/r6/fhir/metadata')
        assert resp.status_code == 200

    def test_invalid_tenant_id_format_rejected(self, client):
        """Tenant IDs with special characters should be rejected."""
        resp = client.get('/r6/fhir/Patient/test-1',
                         headers={'X-Tenant-Id': 'bad tenant!@#'})
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'X-Tenant-Id must match' in data['issue'][0]['diagnostics']

    def test_valid_tenant_id_formats(self, client):
        """Valid tenant IDs with hyphens and underscores should work."""
        resp = client.get('/r6/fhir/Patient/nonexistent',
                         headers={'X-Tenant-Id': 'my-tenant_123'})
        assert resp.status_code == 404  # Accepted but resource not found


class TestStepUpToken:
    """Test HMAC step-up token validation."""

    def test_create_with_invalid_token_rejected(self, client, sample_patient, tenant_headers):
        headers = {**tenant_headers, 'X-Step-Up-Token': 'bogus-token'}
        resp = client.post('/r6/fhir/Patient',
                          data=json.dumps(sample_patient),
                          content_type='application/json',
                          headers=headers)
        assert resp.status_code == 401
        data = resp.get_json()
        assert 'token' in data['issue'][0]['diagnostics'].lower()

    def test_create_with_valid_token_succeeds(self, client, sample_patient, auth_headers):
        resp = client.post('/r6/fhir/Patient',
                          data=json.dumps(sample_patient),
                          content_type='application/json',
                          headers=auth_headers)
        assert resp.status_code == 201


class TestR6CRUD:
    """Test R6 FHIR CRUD operations."""

    def test_create_requires_step_up_token(self, client, sample_patient, tenant_headers):
        resp = client.post('/r6/fhir/Patient',
                          data=json.dumps(sample_patient),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 401
        data = resp.get_json()
        assert data['resourceType'] == 'OperationOutcome'

    def test_create_with_step_up_token(self, client, sample_patient, auth_headers):
        resp = client.post('/r6/fhir/Patient',
                          data=json.dumps(sample_patient),
                          content_type='application/json',
                          headers=auth_headers)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['resourceType'] == 'Patient'
        assert 'meta' in data
        assert data['meta']['versionId'] == '1'

    def test_read_resource(self, client, sample_patient, auth_headers, tenant_headers):
        # Create first
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        # Read
        resp = client.get(f'/r6/fhir/Patient/{sample_patient["id"]}',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Patient'
        assert data['id'] == sample_patient['id']

    def test_read_applies_redaction(self, client, sample_patient, auth_headers, tenant_headers):
        """Direct reads must also apply redaction (not just context envelope)."""
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get(f'/r6/fhir/Patient/{sample_patient["id"]}',
                         headers=tenant_headers)
        data = resp.get_json()

        # Identifiers should be redacted
        for ident in data.get('identifier', []):
            if 'value' in ident:
                assert ident['value'].startswith('***')

        # Address lines should be removed
        for addr in data.get('address', []):
            assert 'line' not in addr

        # Names should be redacted (given names truncated to initial)
        for name_entry in data.get('name', []):
            for given in name_entry.get('given', []):
                assert len(given) <= 2 and given.endswith('.'), \
                    f'Given name not redacted to initial: {given}'

        # Birth date should be truncated to year
        if 'birthDate' in data:
            assert len(data['birthDate']) == 4, \
                f'BirthDate not truncated: {data["birthDate"]}'

    def test_read_nonexistent_returns_404(self, client, tenant_headers):
        resp = client.get('/r6/fhir/Patient/nonexistent',
                         headers=tenant_headers)
        assert resp.status_code == 404

    def test_update_resource(self, client, sample_patient, auth_headers):
        # Create
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        # Update
        sample_patient['gender'] = 'female'
        resp = client.put(f'/r6/fhir/Patient/{sample_patient["id"]}',
                         data=json.dumps(sample_patient),
                         content_type='application/json',
                         headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['meta']['versionId'] == '2'

    def test_unsupported_resource_type(self, client, tenant_headers):
        resp = client.get('/r6/fhir/ImagingStudy/123',
                         headers=tenant_headers)
        assert resp.status_code == 400

    def test_tenant_isolation_prevents_cross_tenant_read(self, client, sample_patient, auth_headers):
        """Resources created by one tenant should not be visible to another."""
        # Create with test tenant
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        # Read with a different tenant should fail
        resp = client.get(f'/r6/fhir/Patient/{sample_patient["id"]}',
                         headers={'X-Tenant-Id': 'other-tenant'})
        assert resp.status_code == 404


class TestETagConcurrency:
    """Test ETag/If-Match concurrency control."""

    def test_update_with_correct_etag(self, client, sample_patient, auth_headers):
        """Update with matching If-Match should succeed."""
        create_resp = client.post('/r6/fhir/Patient',
                                  data=json.dumps(sample_patient),
                                  content_type='application/json',
                                  headers=auth_headers)
        etag = create_resp.headers.get('ETag')
        assert etag is not None

        sample_patient['gender'] = 'female'
        resp = client.put(f'/r6/fhir/Patient/{sample_patient["id"]}',
                         data=json.dumps(sample_patient),
                         content_type='application/json',
                         headers={**auth_headers, 'If-Match': etag})
        assert resp.status_code == 200

    def test_update_with_stale_etag_returns_409(self, client, sample_patient, auth_headers):
        """Update with mismatched If-Match should return 409 Conflict."""
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        sample_patient['gender'] = 'female'
        resp = client.put(f'/r6/fhir/Patient/{sample_patient["id"]}',
                         data=json.dumps(sample_patient),
                         content_type='application/json',
                         headers={**auth_headers, 'If-Match': 'W/"999"'})
        assert resp.status_code == 409


class TestAuditEventImmutability:
    """Test that AuditEvent is system-managed and append-only."""

    def test_create_audit_event_via_api_blocked(self, client, auth_headers):
        audit = {
            'resourceType': 'AuditEvent',
            'id': 'fake-audit',
            'type': {'code': '110100'}
        }
        resp = client.post('/r6/fhir/AuditEvent',
                          data=json.dumps(audit),
                          content_type='application/json',
                          headers=auth_headers)
        assert resp.status_code == 403
        data = resp.get_json()
        assert 'system-managed' in data['issue'][0]['diagnostics']


class TestR6Validate:
    """Test $validate endpoint."""

    def test_validate_valid_patient(self, client, sample_patient, tenant_headers):
        resp = client.post('/r6/fhir/Patient/$validate',
                          data=json.dumps(sample_patient),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'OperationOutcome'

    def test_validate_invalid_observation(self, client, tenant_headers):
        invalid_obs = {'resourceType': 'Observation'}
        resp = client.post('/r6/fhir/Observation/$validate',
                          data=json.dumps(invalid_obs),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 422
        data = resp.get_json()
        assert data['resourceType'] == 'OperationOutcome'
        issues = data['issue']
        assert any('status' in i.get('diagnostics', '') for i in issues)

    def test_validate_missing_body(self, client, tenant_headers):
        resp = client.post('/r6/fhir/Patient/$validate',
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 400


class TestR6ContextIngestion:
    """Test Bundle ingestion and context builder."""

    def test_ingest_bundle_creates_context(self, client, sample_bundle, tenant_headers):
        resp = client.post('/r6/fhir/Bundle/$ingest-context',
                          data=json.dumps(sample_bundle),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 201
        data = resp.get_json()
        assert 'context_id' in data
        assert data['resource_count'] == 2
        assert data['patient_ref'] == 'Patient/test-patient-1'

    def test_get_context_envelope(self, client, sample_bundle, tenant_headers):
        ingest_resp = client.post('/r6/fhir/Bundle/$ingest-context',
                                  data=json.dumps(sample_bundle),
                                  content_type='application/json',
                                  headers=tenant_headers)
        context_id = ingest_resp.get_json()['context_id']

        resp = client.get(f'/r6/fhir/context/{context_id}',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['context_id'] == context_id
        assert data['item_count'] == 2

    def test_get_context_cross_tenant_blocked(self, client, sample_bundle, tenant_headers):
        """Context envelopes should be tenant-isolated."""
        ingest_resp = client.post('/r6/fhir/Bundle/$ingest-context',
                                  data=json.dumps(sample_bundle),
                                  content_type='application/json',
                                  headers=tenant_headers)
        context_id = ingest_resp.get_json()['context_id']

        resp = client.get(f'/r6/fhir/context/{context_id}',
                         headers={'X-Tenant-Id': 'other-tenant'})
        assert resp.status_code == 404

    def test_ingest_empty_bundle_fails(self, client, tenant_headers):
        empty_bundle = {'resourceType': 'Bundle', 'type': 'collection', 'entry': []}
        resp = client.post('/r6/fhir/Bundle/$ingest-context',
                          data=json.dumps(empty_bundle),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 400

    def test_ingest_non_bundle_fails(self, client, sample_patient, tenant_headers):
        resp = client.post('/r6/fhir/Bundle/$ingest-context',
                          data=json.dumps(sample_patient),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 400

    def test_ingest_invalid_bundle_type(self, client, tenant_headers):
        """Bundles with invalid type should be rejected."""
        bad_bundle = {
            'resourceType': 'Bundle',
            'type': 'invalid-type',
            'entry': [{'resource': {'resourceType': 'Patient', 'id': 'p1'}}]
        }
        resp = client.post('/r6/fhir/Bundle/$ingest-context',
                          data=json.dumps(bad_bundle),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'Bundle.type' in data['issue'][0]['diagnostics']


class TestR6AuditEvents:
    """Test AuditEvent recording and querying."""

    def test_read_generates_audit_event(self, client, sample_patient,
                                         auth_headers, tenant_headers):
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        client.get(f'/r6/fhir/Patient/{sample_patient["id"]}',
                  headers=tenant_headers)

        resp = client.get('/r6/fhir/AuditEvent',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] >= 1

    def test_audit_events_filterable_by_context(self, client, sample_bundle, tenant_headers):
        ingest_resp = client.post('/r6/fhir/Bundle/$ingest-context',
                                  data=json.dumps(sample_bundle),
                                  content_type='application/json',
                                  headers=tenant_headers)
        context_id = ingest_resp.get_json()['context_id']

        resp = client.get(f'/r6/fhir/AuditEvent?context-id={context_id}',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Bundle'

    def test_audit_events_tenant_isolated(self, client, sample_patient,
                                            auth_headers, tenant_headers):
        """Audit events from one tenant should not be visible to another."""
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get('/r6/fhir/AuditEvent',
                         headers={'X-Tenant-Id': 'other-tenant'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] == 0


class TestR6ImportStub:
    """Test cross-version import stub."""

    def test_import_stub_returns_accepted(self, client, tenant_headers):
        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [
                {'resource': {'resourceType': 'Patient', 'id': 'r4-patient'}}
            ]
        }
        resp = client.post('/r6/fhir/$import-stub?source-version=R4',
                          data=json.dumps(bundle),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 202
        data = resp.get_json()
        assert '_import_stub' in data
        assert data['_import_stub']['source_version'] == 'R4'
        assert data['_import_stub']['entry_count'] == 1
        assert data['_import_stub']['entries'][0]['transform_status'] == 'needs-transform'


class TestSearchFeatures:
    """Test FHIR search features including _summary and patient reference."""

    def test_summary_count(self, client, sample_patient, auth_headers, tenant_headers):
        """_summary=count should return total without entries."""
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get('/r6/fhir/Patient?_summary=count', headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] >= 1
        assert 'entry' not in data

    def test_patient_ref_validation(self, client, tenant_headers):
        """Invalid patient reference format should be rejected."""
        resp = client.get('/r6/fhir/Observation?patient=bad-ref',
                         headers=tenant_headers)
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'Patient/' in data['issue'][0]['diagnostics']

    def test_valid_patient_ref_search(self, client, tenant_headers):
        """Valid patient reference format should work."""
        resp = client.get('/r6/fhir/Observation?patient=Patient/test-1',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Bundle'
        assert data['type'] == 'searchset'


# ===== Phase 2-5: New Feature Tests =====


class TestOAuthDiscovery:
    """Test OAuth 2.1 and SMART-on-FHIR discovery endpoints."""

    def test_oauth_discovery_endpoint(self, client):
        resp = client.get('/r6/fhir/.well-known/oauth-authorization-server')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'authorization_endpoint' in data
        assert 'token_endpoint' in data
        assert 'registration_endpoint' in data
        assert 'revocation_endpoint' in data
        assert 'S256' in data['code_challenge_methods_supported']

    def test_smart_configuration(self, client):
        resp = client.get('/r6/fhir/.well-known/smart-configuration')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'authorization_endpoint' in data
        assert 'capabilities' in data
        assert 'launch-standalone' in data['capabilities']
        assert 'context-standalone-patient' in data['capabilities']


class TestOAuthFlow:
    """Test OAuth 2.1 authorization code flow with PKCE."""

    def test_dynamic_client_registration(self, client, tenant_headers):
        resp = client.post('/r6/fhir/oauth/register',
                          data=json.dumps({
                              'client_name': 'Test Agent',
                              'redirect_uris': ['http://localhost:3000/callback'],
                              'scope': 'fhir.read context.read',
                          }),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 201
        data = resp.get_json()
        assert 'client_id' in data
        assert 'client_secret' in data
        assert data['client_name'] == 'Test Agent'

    def test_authorize_requires_pkce(self, client, tenant_headers):
        reg_resp = client.post('/r6/fhir/oauth/register',
                              data=json.dumps({
                                  'client_name': 'PKCE Test',
                                  'redirect_uris': ['http://localhost'],
                              }),
                              content_type='application/json',
                              headers=tenant_headers)
        client_id = reg_resp.get_json()['client_id']

        resp = client.get(
            f'/r6/fhir/oauth/authorize?client_id={client_id}'
            f'&redirect_uri=http://localhost',
            headers=tenant_headers)
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'PKCE' in data.get('error_description', '')

    def test_authorize_rejects_unregistered_redirect_uri(self, client, tenant_headers):
        """Authorization should reject redirect URIs not registered for the client."""
        import hashlib, base64, secrets

        reg_resp = client.post('/r6/fhir/oauth/register',
                              data=json.dumps({
                                  'client_name': 'Redirect Test',
                                  'redirect_uris': ['http://localhost/safe-callback'],
                              }),
                              content_type='application/json',
                              headers=tenant_headers)
        client_id = reg_resp.get_json()['client_id']

        code_verifier = secrets.token_urlsafe(32)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b'=').decode()

        resp = client.get(
            f'/r6/fhir/oauth/authorize?client_id={client_id}'
            f'&redirect_uri=https://evil.com/steal'
            f'&code_challenge={code_challenge}'
            f'&code_challenge_method=S256',
            headers=tenant_headers)
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'redirect_uri' in data.get('error_description', '')

    def test_authorize_rejects_unregistered_client(self, client, tenant_headers):
        import hashlib, base64, secrets

        code_verifier = secrets.token_urlsafe(32)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b'=').decode()

        resp = client.get(
            f'/r6/fhir/oauth/authorize?client_id=nonexistent-client'
            f'&redirect_uri=http://localhost/cb'
            f'&code_challenge={code_challenge}'
            f'&code_challenge_method=S256',
            headers=tenant_headers)
        assert resp.status_code == 401

    def test_full_oauth_flow(self, client, tenant_headers):
        import hashlib, base64, secrets

        reg_resp = client.post('/r6/fhir/oauth/register',
                              data=json.dumps({
                                  'client_name': 'Flow Test',
                                  'redirect_uris': ['http://localhost/cb'],
                              }),
                              content_type='application/json',
                              headers=tenant_headers)
        client_id = reg_resp.get_json()['client_id']

        code_verifier = secrets.token_urlsafe(32)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b'=').decode()

        auth_resp = client.get(
            f'/r6/fhir/oauth/authorize?client_id={client_id}'
            f'&redirect_uri=http://localhost/cb'
            f'&scope=fhir.read'
            f'&code_challenge={code_challenge}'
            f'&code_challenge_method=S256'
            f'&state=test-state',
            headers=tenant_headers)
        assert auth_resp.status_code == 200
        auth_data = auth_resp.get_json()
        code = auth_data['code']
        assert auth_data['state'] == 'test-state'

        token_resp = client.post('/r6/fhir/oauth/token',
                                data=json.dumps({
                                    'grant_type': 'authorization_code',
                                    'code': code,
                                    'code_verifier': code_verifier,
                                    'client_id': client_id,
                                }),
                                content_type='application/json',
                                headers=tenant_headers)
        assert token_resp.status_code == 200
        token_data = token_resp.get_json()
        assert 'access_token' in token_data
        assert token_data['token_type'] == 'Bearer'
        assert token_data['scope'] == 'fhir.read'

    def test_token_revocation(self, client, tenant_headers):
        import hashlib, base64, secrets

        reg_resp = client.post('/r6/fhir/oauth/register',
                              data=json.dumps({
                                  'client_name': 'Revoke Test',
                                  'redirect_uris': ['http://localhost/cb'],
                              }),
                              content_type='application/json',
                              headers=tenant_headers)
        client_id = reg_resp.get_json()['client_id']

        code_verifier = secrets.token_urlsafe(32)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b'=').decode()

        auth_resp = client.get(
            f'/r6/fhir/oauth/authorize?client_id={client_id}'
            f'&redirect_uri=http://localhost/cb&scope=fhir.read'
            f'&code_challenge={code_challenge}&code_challenge_method=S256',
            headers=tenant_headers)
        code = auth_resp.get_json()['code']

        token_resp = client.post('/r6/fhir/oauth/token',
                                data=json.dumps({
                                    'grant_type': 'authorization_code',
                                    'code': code,
                                    'code_verifier': code_verifier,
                                }),
                                content_type='application/json',
                                headers=tenant_headers)
        access_token = token_resp.get_json()['access_token']

        revoke_resp = client.post('/r6/fhir/oauth/revoke',
                                  data=json.dumps({'token': access_token}),
                                  content_type='application/json',
                                  headers=tenant_headers)
        assert revoke_resp.status_code == 200


class TestDeidentification:
    """Test HIPAA Safe Harbor de-identification endpoint."""

    def test_deidentify_strips_identifiers(self, client, sample_patient,
                                            auth_headers, tenant_headers):
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get(f'/r6/fhir/Patient/{sample_patient["id"]}/$deidentify',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()

        assert 'name' not in data
        if 'birthDate' in data:
            assert len(data['birthDate']) == 4
        assert 'identifier' not in data
        assert 'address' not in data
        security = data.get('meta', {}).get('security', [])
        codes = [s.get('code') for s in security]
        assert 'ANONYED' in codes

    def test_deidentify_nonexistent_returns_404(self, client, tenant_headers):
        resp = client.get('/r6/fhir/Patient/nonexistent/$deidentify',
                         headers=tenant_headers)
        assert resp.status_code == 404

    def test_deidentify_patient_controlled_removes_name_telecom(
        self, client, sample_patient, auth_headers, tenant_headers
    ):
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        pid = sample_patient['id']
        resp = client.get(
            f'/r6/fhir/Patient/{pid}/$deidentify'
            '?mode=patient-controlled&patient_id=hc-123',
            headers=tenant_headers
        )
        assert resp.status_code == 200
        data = resp.get_json()

        # Direct identifiers removed
        assert 'name' not in data
        assert 'telecom' not in data
        assert 'address' not in data
        assert 'photo' not in data

    def test_deidentify_patient_controlled_preserves_birthdate(
        self, client, sample_patient, auth_headers, tenant_headers
    ):
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        pid = sample_patient['id']
        resp = client.get(
            f'/r6/fhir/Patient/{pid}/$deidentify'
            '?mode=patient-controlled&patient_id=hc-123',
            headers=tenant_headers
        )
        assert resp.status_code == 200
        data = resp.get_json()

        # birthDate PRESERVED (differs from HIPAA Safe Harbor mode)
        assert 'birthDate' in data
        assert len(data['birthDate']) > 4  # full date, not year-only

    def test_deidentify_patient_controlled_injects_healthclaw_id(
        self, client, sample_patient, auth_headers, tenant_headers
    ):
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        pid = sample_patient['id']
        resp = client.get(
            f'/r6/fhir/Patient/{pid}/$deidentify'
            '?mode=patient-controlled&patient_id=hc-456',
            headers=tenant_headers
        )
        assert resp.status_code == 200
        data = resp.get_json()

        identifiers = data.get('identifier', [])
        hc_ids = [
            i for i in identifiers
            if i.get('system') == 'https://healthclaw.io/patient-id'
        ]
        assert len(hc_ids) == 1
        assert hc_ids[0]['value'] == 'hc-456'

    def test_deidentify_patient_controlled_stamps_meta_tag(
        self, client, sample_patient, auth_headers, tenant_headers
    ):
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        pid = sample_patient['id']
        resp = client.get(
            f'/r6/fhir/Patient/{pid}/$deidentify'
            '?mode=patient-controlled&patient_id=hc-789',
            headers=tenant_headers
        )
        assert resp.status_code == 200
        data = resp.get_json()

        tags = data.get('meta', {}).get('tag', [])
        codes = {t.get('code') for t in tags}
        assert 'ANONYED' in codes
        assert 'patient-controlled' in codes


class TestAuditExport:
    """Test audit trail NDJSON export."""

    def test_export_ndjson(self, client, sample_patient, auth_headers, tenant_headers):
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get('/r6/fhir/AuditEvent/$export',
                         headers=tenant_headers)
        assert resp.status_code == 200
        assert 'ndjson' in resp.content_type

    def test_export_fhir_bundle(self, client, sample_patient, auth_headers, tenant_headers):
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get('/r6/fhir/AuditEvent/$export?_format=fhir-bundle',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['resourceType'] == 'Bundle'
        assert data['type'] == 'collection'

    def test_export_tenant_isolated(self, client, sample_patient, auth_headers):
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get('/r6/fhir/AuditEvent/$export?_format=fhir-bundle',
                         headers={'X-Tenant-Id': 'other-tenant'})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['total'] == 0


class TestPrivacyPolicy:
    """Test privacy policy endpoint."""

    def test_privacy_policy_accessible(self, client, tenant_headers):
        resp = client.get('/r6/fhir/docs/privacy-policy',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'medical_disclaimer' in data
        assert 'data_protection' in data
        assert 'data_sharing' in data
        assert data['data_sharing']['ai_training'] == 'Data is never used for AI model training'

    def test_privacy_policy_contains_compliance_info(self, client, tenant_headers):
        resp = client.get('/r6/fhir/docs/privacy-policy',
                         headers=tenant_headers)
        data = resp.get_json()
        assert 'hipaa' in data['compliance']
        assert 'smart_on_fhir' in data['compliance']


class TestHealthCompliance:
    """Test medical disclaimer and health compliance features."""

    def test_disclaimer_added_to_clinical_data(self):
        from r6.health_compliance import add_disclaimer
        obs = {'resourceType': 'Observation', 'status': 'final'}
        result = add_disclaimer(obs)
        assert '_disclaimer' in result

    def test_disclaimer_not_added_to_non_clinical(self):
        from r6.health_compliance import add_disclaimer
        patient = {'resourceType': 'Patient', 'name': [{'family': 'Test'}]}
        result = add_disclaimer(patient)
        assert '_disclaimer' not in result

    def test_deidentify_module(self):
        from r6.health_compliance import deidentify_resource
        resource = {
            'resourceType': 'Patient',
            'id': 'test-123',
            'name': [{'family': 'Smith'}],
            'birthDate': '1990-03-15',
            'identifier': [{'value': 'MRN12345678'}],
            'address': [{'line': ['123 Main St'], 'city': 'Springfield'}],
            'telecom': [{'value': '555-0100'}],
        }
        result = deidentify_resource(resource)
        assert 'name' not in result
        assert 'identifier' not in result
        assert 'telecom' not in result
        assert result.get('birthDate') == '1990'
        assert result['id'] != 'test-123'

    def test_deidentify_strips_codeable_concept_text(self):
        from r6.health_compliance import deidentify_resource
        resource = {
            'resourceType': 'Observation',
            'id': 'obs-1',
            'status': 'final',
            'code': {
                'coding': [{'system': 'http://loinc.org', 'code': '2339-0'}],
                'text': 'Blood Glucose from Springfield Regional'
            }
        }
        result = deidentify_resource(resource)
        assert 'text' not in result.get('code', {})


class TestHumanInTheLoop:
    """Test human-in-the-loop enforcement for clinical writes."""

    def test_clinical_write_requires_human_confirmation(self, client, auth_headers):
        observation = {
            'resourceType': 'Observation',
            'id': 'obs-hitl-test',
            'status': 'final',
            'code': {
                'coding': [{'system': 'http://loinc.org', 'code': '2339-0'}]
            }
        }
        resp = client.post('/r6/fhir/Observation',
                          data=json.dumps(observation),
                          content_type='application/json',
                          headers=auth_headers)
        assert resp.status_code == 428
        data = resp.get_json()
        assert 'X-Human-Confirmed' in data['issue'][0]['diagnostics']

    def test_clinical_write_with_confirmation_proceeds(self, client, auth_headers):
        observation = {
            'resourceType': 'Observation',
            'id': 'obs-hitl-ok',
            'status': 'final',
            'code': {
                'coding': [{'system': 'http://loinc.org', 'code': '2339-0'}]
            }
        }
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        resp = client.post('/r6/fhir/Observation',
                          data=json.dumps(observation),
                          content_type='application/json',
                          headers=headers)
        assert resp.status_code == 201

    def test_non_clinical_write_no_confirmation_needed(self, client, sample_patient, auth_headers):
        resp = client.post('/r6/fhir/Patient',
                          data=json.dumps(sample_patient),
                          content_type='application/json',
                          headers=auth_headers)
        assert resp.status_code == 201


class TestMedicalDisclaimerOnResponses:
    """Test that medical disclaimers are added to clinical read responses."""

    def test_observation_read_has_disclaimer(self, client, auth_headers, tenant_headers):
        obs = {
            'resourceType': 'Observation',
            'id': 'obs-disclaim',
            'status': 'final',
            'code': {
                'coding': [{'system': 'http://loinc.org', 'code': '2339-0'}]
            }
        }
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        client.post('/r6/fhir/Observation',
                    data=json.dumps(obs),
                    content_type='application/json',
                    headers=headers)

        resp = client.get('/r6/fhir/Observation/obs-disclaim',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert '_disclaimer' in data
        assert 'medical advice' in data['_disclaimer']['text'].lower()

    def test_patient_read_no_disclaimer(self, client, sample_patient,
                                         auth_headers, tenant_headers):
        client.post('/r6/fhir/Patient',
                    data=json.dumps(sample_patient),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get(f'/r6/fhir/Patient/{sample_patient["id"]}',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert '_disclaimer' not in data


class TestRateLimitHeaders:
    """Test rate limiting headers appear on responses."""

    def test_rate_limit_headers_present(self, client, tenant_headers):
        resp = client.get('/r6/fhir/Patient/nonexistent',
                         headers=tenant_headers)
        assert 'X-RateLimit-Limit' in resp.headers
        assert 'X-RateLimit-Remaining' in resp.headers


# ===== Enhanced Search Tests =====


class TestEnhancedSearch:
    """Test new search parameters: code, status, _lastUpdated, _sort."""

    def _seed_observations(self, client, auth_headers, data):
        """Seed observations with various codes and statuses."""
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        for obs in data:
            client.post('/r6/fhir/Observation',
                       data=json.dumps(obs),
                       content_type='application/json',
                       headers=headers)

    def test_search_by_code(self, client, auth_headers, tenant_headers):
        """Search Observation by code parameter."""
        self._seed_observations(client, auth_headers, [
            {'resourceType': 'Observation', 'id': 'search-glucose',
             'status': 'final', 'code': {'coding': [{'code': '2339-0'}]},
             'valueQuantity': {'value': 100}},
            {'resourceType': 'Observation', 'id': 'search-hr',
             'status': 'final', 'code': {'coding': [{'code': '8867-4'}]},
             'valueQuantity': {'value': 72}},
        ])
        resp = client.get('/r6/fhir/Observation?code=2339-0',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] >= 1
        # All results should have the glucose code
        for entry in data.get('entry', []):
            resource_json = json.dumps(entry['resource'])
            assert '2339-0' in resource_json

    def test_search_by_status(self, client, auth_headers, tenant_headers):
        """Search Permission by status parameter."""
        client.post('/r6/fhir/Permission',
                   data=json.dumps({
                       'resourceType': 'Permission', 'id': 'search-perm-active',
                       'status': 'active', 'combining': 'deny-overrides'
                   }),
                   content_type='application/json', headers=auth_headers)
        client.post('/r6/fhir/Permission',
                   data=json.dumps({
                       'resourceType': 'Permission', 'id': 'search-perm-draft',
                       'status': 'draft', 'combining': 'deny-overrides'
                   }),
                   content_type='application/json', headers=auth_headers)

        resp = client.get('/r6/fhir/Permission?status=active',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] >= 1
        for entry in data.get('entry', []):
            assert '"status":"active"' in json.dumps(entry['resource']).replace(' ', '')

    def test_search_combined_code_and_status(self, client, auth_headers, tenant_headers):
        """Search with both code and status filters."""
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        client.post('/r6/fhir/Observation',
                   data=json.dumps({
                       'resourceType': 'Observation', 'id': 'combined-1',
                       'status': 'final', 'code': {'coding': [{'code': '2339-0'}]}
                   }),
                   content_type='application/json', headers=headers)
        client.post('/r6/fhir/Observation',
                   data=json.dumps({
                       'resourceType': 'Observation', 'id': 'combined-2',
                       'status': 'preliminary', 'code': {'coding': [{'code': '2339-0'}]}
                   }),
                   content_type='application/json', headers=headers)

        resp = client.get('/r6/fhir/Observation?code=2339-0&status=final',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] >= 1

    def test_search_returns_self_link(self, client, tenant_headers):
        """Search results should include a self link with applied params."""
        resp = client.get('/r6/fhir/Patient?_count=5',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'link' in data
        self_link = next(l for l in data['link'] if l['relation'] == 'self')
        assert '_count=5' in self_link['url']

    def test_search_invalid_lastUpdated(self, client, tenant_headers):
        """Invalid _lastUpdated should return 400."""
        resp = client.get('/r6/fhir/Patient?_lastUpdated=not-a-date',
                         headers=tenant_headers)
        assert resp.status_code == 400


class TestPermissionReasoning:
    """Test that Permission $evaluate returns reasoning."""

    def test_evaluate_includes_reasoning(self, client, sample_permission, auth_headers, tenant_headers):
        """$evaluate should include a reasoning parameter."""
        client.post('/r6/fhir/Permission',
                    data=json.dumps(sample_permission),
                    content_type='application/json',
                    headers=auth_headers)
        resp = client.post('/r6/fhir/Permission/$evaluate',
                          data=json.dumps({'subject': 'Practitioner/dr-1', 'action': 'read'}),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        params = {p['name']: p for p in data['parameter']}
        assert 'reasoning' in params
        assert len(params['reasoning']['valueString']) > 0

    def test_evaluate_deny_reasoning(self, client, tenant_headers):
        """$evaluate with no permissions should explain default deny."""
        resp = client.post('/r6/fhir/Permission/$evaluate',
                          data=json.dumps({'subject': 'Practitioner/dr-1', 'action': 'read'}),
                          content_type='application/json',
                          headers=tenant_headers)
        data = resp.get_json()
        reasoning = next(p for p in data['parameter'] if p['name'] == 'reasoning')
        assert 'No active Permission' in reasoning['valueString']


class TestContextEnforcement:
    """Test that context envelope can include resources."""

    def test_context_with_include_resources(self, client, sample_bundle, tenant_headers):
        """?_include=resources should return actual resource data."""
        ingest_resp = client.post('/r6/fhir/Bundle/$ingest-context',
                                  data=json.dumps(sample_bundle),
                                  content_type='application/json',
                                  headers=tenant_headers)
        assert ingest_resp.status_code == 201
        context_id = ingest_resp.get_json()['context_id']

        resp = client.get(f'/r6/fhir/context/{context_id}?_include=resources',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'resources' in data
        assert len(data['resources']) >= 1
        assert '_note' in data


# ===== R6 Resource Tests =====


class TestPhase2ResourceTypes:
    """Test CRUD for new R6 resource types added in Phase 2."""

    def test_create_permission(self, client, sample_permission, auth_headers):
        resp = client.post('/r6/fhir/Permission',
                          data=json.dumps(sample_permission),
                          content_type='application/json',
                          headers=auth_headers)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['resourceType'] == 'Permission'
        assert data['meta']['versionId'] == '1'

    def test_read_permission(self, client, sample_permission, auth_headers, tenant_headers):
        client.post('/r6/fhir/Permission',
                    data=json.dumps(sample_permission),
                    content_type='application/json',
                    headers=auth_headers)
        resp = client.get(f'/r6/fhir/Permission/{sample_permission["id"]}',
                         headers=tenant_headers)
        assert resp.status_code == 200
        assert resp.get_json()['resourceType'] == 'Permission'

    def test_create_subscription_topic(self, client, sample_subscription_topic, auth_headers):
        resp = client.post('/r6/fhir/SubscriptionTopic',
                          data=json.dumps(sample_subscription_topic),
                          content_type='application/json',
                          headers=auth_headers)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['resourceType'] == 'SubscriptionTopic'

    def test_create_subscription(self, client, sample_subscription, auth_headers):
        resp = client.post('/r6/fhir/Subscription',
                          data=json.dumps(sample_subscription),
                          content_type='application/json',
                          headers=auth_headers)
        assert resp.status_code == 201
        assert resp.get_json()['resourceType'] == 'Subscription'

    def test_create_nutrition_intake(self, client, sample_nutrition_intake, auth_headers):
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        resp = client.post('/r6/fhir/NutritionIntake',
                          data=json.dumps(sample_nutrition_intake),
                          content_type='application/json',
                          headers=headers)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['resourceType'] == 'NutritionIntake'

    def test_create_device_alert(self, client, sample_device_alert, auth_headers):
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        resp = client.post('/r6/fhir/DeviceAlert',
                          data=json.dumps(sample_device_alert),
                          content_type='application/json',
                          headers=headers)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['resourceType'] == 'DeviceAlert'

    def test_search_permission(self, client, sample_permission, auth_headers, tenant_headers):
        client.post('/r6/fhir/Permission',
                    data=json.dumps(sample_permission),
                    content_type='application/json',
                    headers=auth_headers)
        resp = client.get('/r6/fhir/Permission',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['type'] == 'searchset'
        assert data['total'] >= 1


class TestPhase2PermissionValidation:
    """Test Permission resource validation rules."""

    def test_permission_missing_status_rejected(self, client, tenant_headers):
        invalid = {'resourceType': 'Permission', 'combining': 'deny-overrides'}
        resp = client.post('/r6/fhir/Permission/$validate',
                          data=json.dumps(invalid),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 422
        data = resp.get_json()
        assert any('status' in i.get('diagnostics', '') for i in data['issue'])

    def test_permission_missing_combining_rejected(self, client, tenant_headers):
        invalid = {'resourceType': 'Permission', 'status': 'active'}
        resp = client.post('/r6/fhir/Permission/$validate',
                          data=json.dumps(invalid),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 422
        data = resp.get_json()
        assert any('combining' in i.get('diagnostics', '') for i in data['issue'])

    def test_permission_invalid_status_rejected(self, client, tenant_headers):
        invalid = {'resourceType': 'Permission', 'status': 'bogus', 'combining': 'deny-overrides'}
        resp = client.post('/r6/fhir/Permission/$validate',
                          data=json.dumps(invalid),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 422

    def test_permission_valid_passes(self, client, sample_permission, tenant_headers):
        resp = client.post('/r6/fhir/Permission/$validate',
                          data=json.dumps(sample_permission),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 200


class TestPhase2PermissionEvaluate:
    """Test Permission $evaluate operation."""

    def test_evaluate_with_no_permissions_returns_deny(self, client, tenant_headers):
        resp = client.post('/r6/fhir/Permission/$evaluate',
                          data=json.dumps({'subject': 'Practitioner/dr-1', 'action': 'read'}),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        decision = next(p for p in data['parameter'] if p['name'] == 'decision')
        assert decision['valueCode'] == 'deny'

    def test_evaluate_with_permit_rule(self, client, sample_permission, auth_headers, tenant_headers):
        client.post('/r6/fhir/Permission',
                    data=json.dumps(sample_permission),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.post('/r6/fhir/Permission/$evaluate',
                          data=json.dumps({'subject': 'Practitioner/dr-1', 'action': 'read'}),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        decision = next(p for p in data['parameter'] if p['name'] == 'decision')
        assert decision['valueCode'] == 'permit'

    def test_evaluate_requires_json_body(self, client, tenant_headers):
        resp = client.post('/r6/fhir/Permission/$evaluate',
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 400


class TestPhase2ObservationStats:
    """Test Observation $stats operation."""

    def _seed_observations(self, client, auth_headers, values):
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        for i, val in enumerate(values):
            obs = {
                'resourceType': 'Observation',
                'id': f'stats-test-{i}',
                'status': 'final',
                'code': {'coding': [{'system': 'http://loinc.org', 'code': '2339-0'}]},
                'subject': {'reference': 'Patient/test-patient-1'},
                'valueQuantity': {'value': val, 'unit': 'mg/dL'}
            }
            client.post('/r6/fhir/Observation',
                       data=json.dumps(obs),
                       content_type='application/json',
                       headers=headers)

    def test_stats_returns_parameters(self, client, auth_headers, tenant_headers):
        self._seed_observations(client, auth_headers, [90, 100, 110])
        resp = client.get('/r6/fhir/Observation/$stats?code=2339-0',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Parameters'
        params = {p['name']: p for p in data['parameter']}
        assert params['count']['valueInteger'] == 3
        assert params['min']['valueDecimal'] == 90.0
        assert params['max']['valueDecimal'] == 110.0
        assert params['mean']['valueDecimal'] == 100.0

    def test_stats_empty_returns_zero_count(self, client, tenant_headers):
        resp = client.get('/r6/fhir/Observation/$stats?code=nonexistent',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        params = {p['name']: p for p in data['parameter']}
        assert params['count']['valueInteger'] == 0

    def test_stats_filters_by_code(self, client, auth_headers, tenant_headers):
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        client.post('/r6/fhir/Observation',
                   data=json.dumps({
                       'resourceType': 'Observation', 'id': 'stats-glucose',
                       'status': 'final',
                       'code': {'coding': [{'code': '2339-0'}]},
                       'valueQuantity': {'value': 100, 'unit': 'mg/dL'}
                   }),
                   content_type='application/json', headers=headers)
        client.post('/r6/fhir/Observation',
                   data=json.dumps({
                       'resourceType': 'Observation', 'id': 'stats-hr',
                       'status': 'final',
                       'code': {'coding': [{'code': '8867-4'}]},
                       'valueQuantity': {'value': 72, 'unit': '/min'}
                   }),
                   content_type='application/json', headers=headers)

        resp = client.get('/r6/fhir/Observation/$stats?code=2339-0',
                         headers=tenant_headers)
        data = resp.get_json()
        params = {p['name']: p for p in data['parameter']}
        assert params['count']['valueInteger'] >= 1


class TestPhase2ObservationLastN:
    """Test Observation $lastn operation."""

    def test_lastn_returns_bundle(self, client, auth_headers, tenant_headers):
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        for i in range(3):
            client.post('/r6/fhir/Observation',
                       data=json.dumps({
                           'resourceType': 'Observation', 'id': f'lastn-obs-{i}',
                           'status': 'final',
                           'code': {'coding': [{'code': '2339-0'}]},
                           'valueQuantity': {'value': 90 + i * 10, 'unit': 'mg/dL'}
                       }),
                       content_type='application/json', headers=headers)

        resp = client.get('/r6/fhir/Observation/$lastn?code=2339-0&max=2',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Bundle'
        assert data['type'] == 'searchset'
        assert data['total'] <= 2

    def test_lastn_default_max_is_1(self, client, auth_headers, tenant_headers):
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        for i in range(3):
            client.post('/r6/fhir/Observation',
                       data=json.dumps({
                           'resourceType': 'Observation', 'id': f'lastn-def-{i}',
                           'status': 'final',
                           'code': {'coding': [{'code': '8867-4'}]},
                           'valueQuantity': {'value': 72 + i}
                       }),
                       content_type='application/json', headers=headers)

        resp = client.get('/r6/fhir/Observation/$lastn?code=8867-4',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] == 1


class TestPhase2SubscriptionTopicList:
    """Test SubscriptionTopic $list operation."""

    def test_list_returns_empty_bundle(self, client, tenant_headers):
        resp = client.get('/r6/fhir/SubscriptionTopic/$list',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['resourceType'] == 'Bundle'
        assert data['type'] == 'searchset'

    def test_list_includes_created_topics(self, client, sample_subscription_topic,
                                           auth_headers, tenant_headers):
        client.post('/r6/fhir/SubscriptionTopic',
                    data=json.dumps(sample_subscription_topic),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get('/r6/fhir/SubscriptionTopic/$list',
                         headers=tenant_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] >= 1

    def test_list_tenant_isolated(self, client, sample_subscription_topic, auth_headers):
        client.post('/r6/fhir/SubscriptionTopic',
                    data=json.dumps(sample_subscription_topic),
                    content_type='application/json',
                    headers=auth_headers)

        resp = client.get('/r6/fhir/SubscriptionTopic/$list',
                         headers={'X-Tenant-Id': 'other-tenant'})
        assert resp.status_code == 200
        assert resp.get_json()['total'] == 0


class TestPhase2SubscriptionValidation:
    """Test Subscription and SubscriptionTopic validation."""

    def test_subscription_topic_missing_status(self, client, tenant_headers):
        invalid = {'resourceType': 'SubscriptionTopic', 'url': 'http://test.org/topic'}
        resp = client.post('/r6/fhir/SubscriptionTopic/$validate',
                          data=json.dumps(invalid),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 422
        data = resp.get_json()
        assert any('status' in i.get('diagnostics', '') for i in data['issue'])

    def test_subscription_missing_topic(self, client, tenant_headers):
        invalid = {'resourceType': 'Subscription', 'status': 'requested'}
        resp = client.post('/r6/fhir/Subscription/$validate',
                          data=json.dumps(invalid),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 422
        data = resp.get_json()
        assert any('topic' in i.get('diagnostics', '') for i in data['issue'])


class TestPhase2NutritionDeviceAlertValidation:
    """Test NutritionIntake and DeviceAlert validation."""

    def test_nutrition_intake_missing_status(self, client, tenant_headers):
        invalid = {'resourceType': 'NutritionIntake'}
        resp = client.post('/r6/fhir/NutritionIntake/$validate',
                          data=json.dumps(invalid),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 422
        data = resp.get_json()
        assert any('status' in i.get('diagnostics', '') for i in data['issue'])

    def test_device_alert_missing_status(self, client, tenant_headers):
        invalid = {'resourceType': 'DeviceAlert'}
        resp = client.post('/r6/fhir/DeviceAlert/$validate',
                          data=json.dumps(invalid),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 422
        data = resp.get_json()
        assert any('status' in i.get('diagnostics', '') for i in data['issue'])

    def test_nutrition_intake_valid(self, client, sample_nutrition_intake, tenant_headers):
        resp = client.post('/r6/fhir/NutritionIntake/$validate',
                          data=json.dumps(sample_nutrition_intake),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 200

    def test_device_alert_valid(self, client, sample_device_alert, tenant_headers):
        resp = client.post('/r6/fhir/DeviceAlert/$validate',
                          data=json.dumps(sample_device_alert),
                          content_type='application/json',
                          headers=tenant_headers)
        assert resp.status_code == 200


class TestPhase2CapabilityStatement:
    """Test Phase 2 additions to CapabilityStatement."""

    def test_metadata_includes_phase2_resources(self, client):
        resp = client.get('/r6/fhir/metadata')
        data = resp.get_json()
        resource_types = [r['type'] for r in data['rest'][0]['resource']]
        assert 'Permission' in resource_types
        assert 'SubscriptionTopic' in resource_types
        assert 'Subscription' in resource_types
        assert 'NutritionIntake' in resource_types
        assert 'DeviceAlert' in resource_types
        assert 'DeviceAssociation' in resource_types

    def test_metadata_includes_phase2_operations(self, client):
        resp = client.get('/r6/fhir/metadata')
        data = resp.get_json()
        op_names = [o['name'] for o in data['rest'][0]['operation']]
        assert 'stats' in op_names
        assert 'lastn' in op_names

    def test_metadata_version_is_phase2(self, client):
        resp = client.get('/r6/fhir/metadata')
        data = resp.get_json()
        assert data['software']['version'] == '1.3.0'


class TestPhase2DisclaimersOnNewResources:
    """Test clinical disclaimers on Phase 2 resources."""

    def test_nutrition_intake_has_disclaimer(self, client, sample_nutrition_intake, auth_headers, tenant_headers):
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        client.post('/r6/fhir/NutritionIntake',
                    data=json.dumps(sample_nutrition_intake),
                    content_type='application/json',
                    headers=headers)
        resp = client.get(f'/r6/fhir/NutritionIntake/{sample_nutrition_intake["id"]}',
                         headers=tenant_headers)
        assert resp.status_code == 200
        assert '_disclaimer' in resp.get_json()

    def test_device_alert_has_disclaimer(self, client, sample_device_alert, auth_headers, tenant_headers):
        headers = {**auth_headers, 'X-Human-Confirmed': 'true'}
        client.post('/r6/fhir/DeviceAlert',
                    data=json.dumps(sample_device_alert),
                    content_type='application/json',
                    headers=headers)
        resp = client.get(f'/r6/fhir/DeviceAlert/{sample_device_alert["id"]}',
                         headers=tenant_headers)
        assert resp.status_code == 200
        assert '_disclaimer' in resp.get_json()

    def test_permission_no_disclaimer(self, client, sample_permission, auth_headers, tenant_headers):
        """Permission is not a clinical resource — no disclaimer."""
        client.post('/r6/fhir/Permission',
                    data=json.dumps(sample_permission),
                    content_type='application/json',
                    headers=auth_headers)
        resp = client.get(f'/r6/fhir/Permission/{sample_permission["id"]}',
                         headers=tenant_headers)
        assert resp.status_code == 200
        assert '_disclaimer' not in resp.get_json()
