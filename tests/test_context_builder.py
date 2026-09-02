"""
Tests for the Context Builder service.
"""

import json

from models import db
from r6.models import R6Resource


class TestContextBuilder:
    """Test context builder functionality."""

    def test_redaction_strips_identifiers(self, client, sample_bundle, tenant_headers):
        """Context builder should redact identifiers to last 4 chars."""
        resp = client.post('/r6/fhir/Bundle/$ingest-context',
                          data=json.dumps(sample_bundle),
                          content_type='application/json',
                          headers=tenant_headers)
        resp.get_json()['context_id']

        # Read the stored patient (same tenant)
        patient_resp = client.get('/r6/fhir/Patient/test-patient-1',
                                  headers=tenant_headers)
        data = patient_resp.get_json()

        # Check identifier redaction
        if 'identifier' in data:
            for ident in data['identifier']:
                if 'value' in ident:
                    assert ident['value'].startswith('***')

    def test_redaction_removes_address_lines(self, client, sample_bundle, tenant_headers):
        """Context builder should remove address line details."""
        client.post('/r6/fhir/Bundle/$ingest-context',
                    data=json.dumps(sample_bundle),
                    content_type='application/json',
                    headers=tenant_headers)

        # Read the stored patient
        patient_resp = client.get('/r6/fhir/Patient/test-patient-1',
                                  headers=tenant_headers)
        data = patient_resp.get_json()

        # Check address redaction
        if 'address' in data:
            for addr in data['address']:
                assert 'line' not in addr

    def test_context_includes_provenance_hashes(self, client, sample_bundle, tenant_headers):
        """Context items should include SHA-256 hashes."""
        resp = client.post('/r6/fhir/Bundle/$ingest-context',
                          data=json.dumps(sample_bundle),
                          content_type='application/json',
                          headers=tenant_headers)
        data = resp.get_json()

        for item in data['items']:
            assert 'sha256' in item
            assert len(item['sha256']) == 64  # SHA-256 hex length

    def test_context_has_slice_names(self, client, sample_bundle, tenant_headers):
        """Context items should be assigned slice names."""
        resp = client.post('/r6/fhir/Bundle/$ingest-context',
                          data=json.dumps(sample_bundle),
                          content_type='application/json',
                          headers=tenant_headers)
        data = resp.get_json()

        slices = {item['slice_name'] for item in data['items']}
        assert 'demographics' in slices  # Patient
        assert 'observations' in slices  # Observation

    def test_context_has_expiry(self, client, sample_bundle, tenant_headers):
        """Context envelope should have an expiry timestamp."""
        resp = client.post('/r6/fhir/Bundle/$ingest-context',
                          data=json.dumps(sample_bundle),
                          content_type='application/json',
                          headers=tenant_headers)
        data = resp.get_json()
        assert 'expires_at' in data
        assert data['expires_at'] is not None

    def test_reingest_revives_a_tombstoned_row(self, client, sample_bundle,
                                               sample_observation, tenant_id,
                                               tenant_headers):
        """#509 defect 2: the upsert lands on a soft-deleted row and must
        clear the flag, as the Fasten ingester does. Before the fix it wrote
        the new content into the tombstone and reported success — data that
        was there, and that no read path could see.
        """
        stale = dict(sample_observation, status='preliminary')
        row = R6Resource(
            resource_type='Observation',
            resource_json=json.dumps(stale, separators=(',', ':'), sort_keys=True),
            resource_id=sample_observation['id'],
            tenant_id=tenant_id,
        )
        row.is_deleted = True
        db.session.add(row)
        db.session.commit()

        resp = client.post('/r6/fhir/Bundle/$ingest-context',
                           data=json.dumps(sample_bundle),
                           content_type='application/json',
                           headers=tenant_headers)
        assert resp.status_code == 201, resp.get_data(as_text=True)

        row = R6Resource.query.filter_by(
            tenant_id=tenant_id, resource_type='Observation',
            id=sample_observation['id']).first()
        assert row.is_deleted is False
        assert row.version_id == 2
        assert json.loads(row.resource_json)['status'] == 'final'
