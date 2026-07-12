"""Ingest resilience — real EHR data breaks naive assumptions.

Both bugs here only manifested on Postgres (SQLite does not enforce varchar
length), so 966 green tests missed them until a live Epic export
(2026-07-08): 65/250 resource ids exceeded varchar(64), the first over-length
id truncation-errored, and the un-rolled-back session poisoned all 250.
"""

import uuid

from r6.models import R6Resource
from models import db


def test_resource_id_column_fits_real_ehr_ids():
    # Epic ids run to ~109 chars; the FHIR 64-char limit is widely violated.
    # Assert the column is wide enough WITHOUT relying on Postgres to enforce
    # it (SQLite silently accepts any length, which is why this regressed).
    assert R6Resource.__table__.c.id.type.length >= 128


def test_long_id_resource_stores_and_round_trips(client, tenant_id):
    long_id = "e-" + uuid.uuid4().hex + uuid.uuid4().hex + "X" * 20  # ~86 chars
    assert len(long_id) > 64
    res = R6Resource(resource_type="Observation",
                     resource_json='{"resourceType":"Observation","id":"%s"}' % long_id,
                     resource_id=long_id, tenant_id=tenant_id)
    db.session.add(res)
    db.session.commit()
    got = R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type="Observation", id=long_id).first()
    assert got is not None and got.id == long_id


def test_ingest_error_rolls_back_session_so_next_resource_succeeds(client, tenant_id):
    # The core resilience contract: a failed resource must not poison the
    # session for the ones after it. Simulate by forcing one flush to fail,
    # rolling back, then confirming a clean insert still commits.
    from sqlalchemy.exc import IntegrityError

    good1 = R6Resource(resource_type="Observation",
                       resource_json="{}", resource_id="ir-good-1",
                       tenant_id=tenant_id)
    db.session.add(good1)
    db.session.commit()

    # Force a failure (duplicate composite PK — same tenant, type, AND id)
    # then the ingester's recovery: rollback.
    dup = R6Resource(resource_type="Observation",
                     resource_json="{}", resource_id="ir-good-1",
                     tenant_id=tenant_id)
    db.session.add(dup)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # this is what the ingester now does per-resource

    # After rollback the session is usable again — the "next" resource commits.
    good2 = R6Resource(resource_type="Observation",
                       resource_json="{}", resource_id="ir-good-2",
                       tenant_id=tenant_id)
    db.session.add(good2)
    db.session.commit()
    assert R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type="Observation",
        id="ir-good-2").first() is not None


def test_audit_resource_id_column_fits_real_ehr_ids():
    # The audit table stores the same resource ids; if it's narrower than
    # R6Resource.id, an audit-insert truncation rolls back the resource write
    # in the shared transaction (found live 2026-07-08 — only 2/56
    # observations survived). Must be at least as wide.
    from r6.models import AuditEventRecord, R6Resource
    assert (AuditEventRecord.__table__.c.resource_id.type.length
            >= R6Resource.__table__.c.id.type.length)


def test_curatr_engine_api_matches_ingester_usage():
    # The Fasten post-ingest scan imports CuratrEngine and calls
    # evaluate(resource) -> result.issues. Pin that contract so the import/
    # signature can't silently drift again (it did: stale 'CuratrEvaluator').
    from r6.curatr import CuratrEngine
    engine = CuratrEngine()
    result = engine.evaluate({"resourceType": "Condition", "id": "c1"})
    assert hasattr(result, "issues")
    assert isinstance(result.issues, list)
