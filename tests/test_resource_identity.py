"""Resource identity is (tenant_id, resource_type, id) — not the bare FHIR id.

Two audits found the same live bug: R6Resource's primary key was the raw
FHIR id ALONE (global). FHIR ids are only unique per resource type per
server — Synthea exports reuse 'example', Epic reuses numeric ids across
types — so:

  1. Tenant B importing a resource whose id tenant A already stored hit a
     PK collision; the ingester's per-resource IntegrityError recovery
     caught it and the resource was SILENTLY DROPPED from tenant B's import.
  2. Patient/abc and Observation/abc collided even within a single tenant.

These tests pin the fixed identity model. They were written BEFORE the fix
(TDD) and demonstrably failed against the global-PK schema.

NOTE (W2): a `source` column + ingest Provenance are deliberately NOT part
of this migration — see the real-actions reliability design doc.
"""

import json

from models import db
from r6.models import R6Resource


def _resource(resource_type: str, resource_id: str, marker: str = 'v1') -> dict:
    return {
        'resourceType': resource_type,
        'id': resource_id,
        # Marker lives in `language`, not `meta` — to_fhir_json() rebuilds the
        # meta envelope on read, so a meta marker would be invisible via the API.
        'language': marker,
    }


def _ingest(resource: dict, tenant: str) -> tuple:
    from r6.fasten.ingester import _ingest_one
    result = _ingest_one(resource, tenant)
    db.session.commit()
    return result


def _row(tenant: str, resource_type: str, resource_id: str) -> R6Resource | None:
    return R6Resource.query.filter_by(
        tenant_id=tenant, resource_type=resource_type, id=resource_id
    ).first()


class TestCrossTenantSameId:
    """The silent-drop bug: same FHIR id in two tenants must coexist."""

    def test_second_tenant_ingest_is_not_dropped(self, app):
        # Synthea's canonical 'example' id — the exact live collision.
        status_a, _ = _ingest(_resource('Patient', 'example', 'tenant-a-copy'),
                              'test-tenant')
        status_b, _ = _ingest(_resource('Patient', 'example', 'tenant-b-copy'),
                              'other-tenant')
        assert status_a == 'ok'
        assert status_b == 'ok'  # was: IntegrityError -> silently dropped

        row_a = _row('test-tenant', 'Patient', 'example')
        row_b = _row('other-tenant', 'Patient', 'example')
        assert row_a is not None
        assert row_b is not None
        # Distinct rows, each holding its own tenant's content.
        assert json.loads(row_a.resource_json)['language'] == 'tenant-a-copy'
        assert json.loads(row_b.resource_json)['language'] == 'tenant-b-copy'

    def test_both_tenants_can_read_their_copy_via_api(
            self, client, tenant_headers, other_tenant_headers):
        _ingest(_resource('Patient', 'example', 'a'), 'test-tenant')
        _ingest(_resource('Patient', 'example', 'b'), 'other-tenant')

        resp_a = client.get('/r6/fhir/Patient/example', headers=tenant_headers)
        resp_b = client.get('/r6/fhir/Patient/example',
                            headers=other_tenant_headers)
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        assert resp_a.get_json()['language'] == 'a'
        assert resp_b.get_json()['language'] == 'b'

    def test_second_tenant_ingest_does_not_mutate_first_tenants_row(self, app):
        _ingest(_resource('Observation', '42', 'original'), 'test-tenant')
        _ingest(_resource('Observation', '42', 'other'), 'other-tenant')

        row_a = _row('test-tenant', 'Observation', '42')
        assert row_a.version_id == 1  # untouched by the other tenant's import
        assert json.loads(row_a.resource_json)['language'] == 'original'


class TestSameIdAcrossTypesOneTenant:
    """FHIR ids are only unique per TYPE: Patient/X and Observation/X coexist."""

    def test_patient_and_observation_share_an_id(self, app, tenant_id):
        status_p, _ = _ingest(_resource('Patient', 'abc'), tenant_id)
        status_o, _ = _ingest(_resource('Observation', 'abc'), tenant_id)
        assert status_p == 'ok'
        assert status_o == 'ok'

        assert _row(tenant_id, 'Patient', 'abc') is not None
        assert _row(tenant_id, 'Observation', 'abc') is not None

    def test_typed_read_returns_the_right_resource(self, client, tenant_id,
                                                   tenant_headers):
        _ingest(_resource('Patient', 'abc', 'the-patient'), tenant_id)
        _ingest(_resource('Observation', 'abc', 'the-obs'), tenant_id)

        pt = client.get('/r6/fhir/Patient/abc', headers=tenant_headers)
        obs = client.get('/r6/fhir/Observation/abc', headers=tenant_headers)
        assert pt.status_code == 200
        assert obs.status_code == 200
        assert pt.get_json()['resourceType'] == 'Patient'
        assert obs.get_json()['resourceType'] == 'Observation'


class TestReIngestIsUpdate:
    """Same (tenant, type, id) re-ingested updates in place — no duplicate."""

    def test_reingest_updates_not_duplicates(self, app, tenant_id):
        _ingest(_resource('Condition', 'c-1', 'v1'), tenant_id)
        _ingest(_resource('Condition', 'c-1', 'v2'), tenant_id)

        rows = R6Resource.query.filter_by(
            tenant_id=tenant_id, resource_type='Condition', id='c-1').all()
        assert len(rows) == 1
        assert rows[0].version_id == 2
        assert json.loads(rows[0].resource_json)['language'] == 'v2'


class TestSoftDeleteReIngest:
    """A soft-deleted resource re-ingests cleanly (revived, not PK-collided)."""

    def test_deleted_resource_reingests(self, app, tenant_id):
        _ingest(_resource('Immunization', 'imm-1', 'v1'), tenant_id)
        row = _row(tenant_id, 'Immunization', 'imm-1')
        row.is_deleted = True
        db.session.commit()

        status, _ = _ingest(_resource('Immunization', 'imm-1', 'v2'), tenant_id)
        assert status == 'ok'  # was: re-add() -> PK collision -> dropped

        rows = R6Resource.query.filter_by(
            tenant_id=tenant_id, resource_type='Immunization', id='imm-1').all()
        assert len(rows) == 1
        assert rows[0].is_deleted is False
        assert json.loads(rows[0].resource_json)['language'] == 'v2'


class TestSchemaIdentity:
    """Pin the composite PK itself so it can't silently regress."""

    def test_primary_key_is_tenant_type_id(self):
        pk_cols = [c.name for c in R6Resource.__table__.primary_key.columns]
        assert set(pk_cols) == {'tenant_id', 'resource_type', 'id'}

    def test_tenant_id_is_not_nullable(self):
        # PK membership implies NOT NULL — a NULL tenant would make the row
        # unreachable by every tenant-scoped query in the codebase.
        assert R6Resource.__table__.c.tenant_id.nullable is False
