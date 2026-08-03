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


def test_fasten_ingest_still_accepts_real_epic_shaped_long_ids(client, tenant_id):
    """Regression guard for #267's `_RESOURCE_ID_PATTERN`
    (r6/fasten/ingester.py): `^[A-Za-z0-9\\-\\.]{1,64}$`, enforced
    unconditionally inside `_ingest_one` -- the SAME function Fasten's
    `stream_ingest` calls for every real EHR export, not just the
    direct-upload path #267 was fixing.

    `test_long_id_resource_stores_and_round_trips` above proves the DB
    column is wide enough for a ~86-char Epic id, but it writes the
    R6Resource row directly and never calls `_ingest_one` -- so it could
    not have caught this. This test drives the real ingester entry point.

    #267's own docstring says the pattern is "FHIR id: ... per the spec",
    and 64 chars IS the FHIR spec limit -- but this file's docstring (and
    the 2026-07-08 live-Epic-export incident it documents: 65/250 resource
    ids over varchar(64), which is why R6Resource.id was widened to
    varchar(128) instead of truncated or rejected) is the standing record
    that real Epic exports routinely violate that limit. `_ingest_one`
    previously stored whatever id Epic sent, subject only to the DB column
    width; #267 now rejects it at the application layer as `invalid_id`
    before it ever reaches the DB -- same ceiling the 128-char column was
    widened specifically to avoid, reintroduced silently (a skipped/
    invalid_id entry, not a crash) for the live Fasten/Epic connector.
    """
    from r6.fasten.ingester import _ingest_one

    long_id = "e-" + uuid.uuid4().hex + uuid.uuid4().hex + "X" * 20  # ~86 chars
    assert len(long_id) > 64
    resource = {"resourceType": "Observation", "id": long_id, "status": "final"}

    result, rid = _ingest_one(resource, tenant_id)

    assert result == "ok" and rid == long_id, (
        f"real Epic-shaped id (len={len(long_id)}) was refused as "
        f"{result!r} by _RESOURCE_ID_PATTERN -- #267's id-shape "
        "validation silently drops real Fasten/Epic resources whose "
        "ids exceed 64 characters, exactly the case "
        "test_resource_id_column_fits_real_ehr_ids above widened the "
        "column for"
    )


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


def test_audit_public_outcome_detail_code_is_bounded():
    from r6.models import AuditEventRecord
    column = AuditEventRecord.__table__.c.outcome_detail_code
    assert column.type.length == 64
    assert column.nullable is True


def test_curatr_engine_api_matches_ingester_usage():
    # The Fasten post-ingest scan imports CuratrEngine and calls
    # evaluate(resource) -> result.issues. Pin that contract so the import/
    # signature can't silently drift again (it did: stale 'CuratrEvaluator').
    from r6.curatr import CuratrEngine
    engine = CuratrEngine()
    result = engine.evaluate({"resourceType": "Condition", "id": "c1"})
    assert hasattr(result, "issues")
    assert isinstance(result.issues, list)


def test_resource_id_pattern_is_a_charset_control_not_a_length_control():
    """#267's id validation must keep rejecting injectable ids WITHOUT
    re-imposing the 64-char FHIR ceiling this file's incident forced us off.

    The security finding it closes is that a caller-supplied id reached
    `AuditEventRecord.resource_id`, which `health_compliance.py` exports into
    the auditor-facing compliance bundle — so an id could smuggle a name, a
    date, a path, or markup into an audit artifact. The CHARSET stops that.
    The LENGTH never had anything to do with it, and capping at 64 silently
    skipped real Epic resources on the live Fasten connector (caught in
    review, not by 1772 green tests).

    Pinned in both directions so a future "tighten it to spec" edit fails
    here instead of in production.
    """
    from r6.fasten.ingester import _RESOURCE_ID_PATTERN as pat
    from r6.models import R6Resource

    # Injectable shapes stay refused, regardless of length.
    for bad in ("../../etc/passwd", "Jane Doe 1980-01-01 MRN 12345",
                "<script>alert(1)</script>", "id with spaces", "a/b", "a_b"):
        assert not pat.fullmatch(bad), bad

    # Real Epic-shaped ids (~109 chars per the 2026-07-08 export) are fine.
    assert pat.fullmatch("e-" + "a" * 107)

    # The ceiling tracks the column width, so validation and storage cannot
    # disagree: an id we accept must be an id we can store.
    width = R6Resource.__table__.c.id.type.length
    assert pat.fullmatch("a" * width)
    assert not pat.fullmatch("a" * (width + 1))
