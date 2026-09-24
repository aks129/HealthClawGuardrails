"""A re-seed revives a tombstoned demo row instead of skipping it.

seed_demo_data pre-checked with `is_deleted=False`, so a soft-deleted
demo-patient-rivera was invisible to the check; the insert then hit the
composite primary key held by the tombstone, logged a warning and moved
on. The demo stayed empty while the comment beside the check promised to
"seed a live one".

A tombstoned seed row now goes through the ingest path's revive
(r6/fasten/ingester.py `_ingest_one`, pinned by
tests/test_revive_on_reingest_is_audited.py): the row comes back live with
the seed content and one 'update' AuditEvent naming the lifted tombstone,
in the same transaction. A live row is still skipped untouched.

Not covered here: main.py's seed_demo_tenant gate, which on this branch
still counts tombstones as existing data and never calls seed_demo_data
(#814 changes that). /internal/seed and the seed scripts call
seed_demo_data directly, so they are covered.

MUTATION: remove the `_ingest_one(...)` revive call in r6/seed.py -> the
revive test goes red; the live-skip test stays green.
"""

from models import db
from r6.models import AuditEventRecord, R6Resource
from r6.seed import seed_demo_data

PATIENT = 'demo-patient-rivera'
REVIVE = 'tombstone lifted on re-ingest'
# Synthetic demo values from r6/seed.py; none may reach an audit row.
SENTINELS = ('Rivera', 'Maria', 'Elena', 'MRN-2026-4471', '617-555-0198',
             'Clinical Ave', '1985-03-15')


def _patient(tenant_id):
    return R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type='Patient', id=PATIENT).first()


def _revive_rows(tenant_id):
    return [r for r in AuditEventRecord.query.filter_by(
                tenant_id=tenant_id, resource_id=PATIENT).all()
            if r.detail and REVIVE in r.detail]


def test_reseed_revives_a_tombstoned_demo_patient(app, tenant_id):
    with app.app_context():
        seed_demo_data(tenant_id)
        row = _patient(tenant_id)
        row.is_deleted = True
        db.session.commit()

        seed_demo_data(tenant_id)

        db.session.expire_all()
        row = _patient(tenant_id)
        assert row.is_deleted is False
        body = row.to_fhir_json()
        assert body['name'][0]['family'] == 'Rivera'
        assert body['identifier'][0]['value'] == 'MRN-2026-4471'

        rows = _revive_rows(tenant_id)
        assert len(rows) == 1
        assert rows[0].event_type == 'update'
        assert rows[0].resource_type == 'Patient'
        assert rows[0].agent_id == 'seed'

        for r in AuditEventRecord.query.filter_by(tenant_id=tenant_id).all():
            blob = ' '.join(str(v) for v in (r.detail, r.resource_id,
                                              r.agent_id, r.outcome_detail_code))
            for s in SENTINELS:
                assert s not in blob


def test_reseed_leaves_a_live_patient_alone(app, tenant_id):
    with app.app_context():
        seed_demo_data(tenant_id)
        seed_demo_data(tenant_id)
        assert _patient(tenant_id).is_deleted is False
        assert _revive_rows(tenant_id) == []
        assert AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_id=PATIENT).count() == 1
