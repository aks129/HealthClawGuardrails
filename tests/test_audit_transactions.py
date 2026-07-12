"""Domain writes and their AuditEvent must share one transaction."""

from models import db
from r6.fasten.ingester import _ingest_one
from r6.models import AuditEventRecord, R6Resource


RESOURCE = {
    "resourceType": "Observation",
    "id": "atomic-audit-observation",
    "status": "final",
    "code": {"text": "Synthetic test value"},
}


def test_fasten_resource_and_audit_roll_back_together(app):
    with app.app_context():
        assert _ingest_one(RESOURCE, "atomic-tenant") == (
            "ok", "atomic-audit-observation"
        )
        db.session.rollback()

        assert db.session.get(
            R6Resource,
            ("atomic-tenant", "Observation", "atomic-audit-observation"),
        ) is None
        assert AuditEventRecord.query.filter_by(
            tenant_id="atomic-tenant",
            resource_id="atomic-audit-observation",
        ).count() == 0


def test_fasten_resource_and_audit_commit_together(app):
    with app.app_context():
        _ingest_one(RESOURCE, "atomic-tenant")
        db.session.commit()

        assert db.session.get(
            R6Resource,
            ("atomic-tenant", "Observation", "atomic-audit-observation"),
        ) is not None
        assert AuditEventRecord.query.filter_by(
            tenant_id="atomic-tenant",
            resource_id="atomic-audit-observation",
        ).count() == 1
