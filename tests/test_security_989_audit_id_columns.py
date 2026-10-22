"""Security re-check of #989 (FHI-60): caller ids against the audit columns.

`AuditEventRecord.agent_id` is String(128), `context_id` and `tenant_id`
String(64). Postgres enforces the widths, SQLite does not, so each pin here
holds on both: an id either lands in the row or the request fails closed. A
read is never served without its AuditEvent.
"""

import pytest

from models import db
from r6.audit import AuditWriteError, record_audit_event
from r6.log_safety import id_hash
from r6.models import AuditEventRecord


def test_agent_id_column_boundary_128_raw_129_hashed(client, tenant_id):
    at_limit = "a" * 128
    over = "a" * 129
    record_audit_event("read", "Patient", agent_id=at_limit,
                       tenant_id=tenant_id)
    record_audit_event("read", "Patient", agent_id=over, tenant_id=tenant_id)

    assert AuditEventRecord.query.filter_by(
        tenant_id=tenant_id, agent_id=at_limit).count() == 1
    assert AuditEventRecord.query.filter_by(
        tenant_id=tenant_id, agent_id=id_hash(over)).count() == 1
    assert not AuditEventRecord.query.filter_by(agent_id=over).count()


def test_an_unencodable_agent_id_fails_closed(client, tenant_id):
    """A lone surrogate cannot be hashed. The write must raise, not skip."""
    before = AuditEventRecord.query.filter_by(tenant_id=tenant_id).count()
    with pytest.raises(AuditWriteError):
        record_audit_event("read", "Patient", agent_id="\ud800",
                           tenant_id=tenant_id)
    db.session.rollback()
    assert AuditEventRecord.query.filter_by(
        tenant_id=tenant_id).count() == before


def test_an_overlong_context_id_is_never_served_unaudited(
        client, auth_headers, tenant_id):
    """X-Context-Id goes to a String(64) column unchanged. On Postgres a
    65-char value fails the audit write; the read must then fail with it."""
    created = client.post(
        "/r6/fhir/Patient",
        json={"resourceType": "Patient", "gender": "female"},
        headers={**auth_headers, "X-Human-Confirmed": "true"})
    assert created.status_code == 201
    pid = created.get_json()["id"]

    def audited_reads():
        return AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, event_type="read", resource_type="Patient",
            resource_id=pid).count()

    try:
        response = client.get(
            f"/r6/fhir/Patient/{pid}",
            headers={"X-Tenant-Id": tenant_id, "X-Context-Id": "c" * 65})
    except AuditWriteError:
        db.session.rollback()
        assert audited_reads() == 0
        return

    db.session.rollback()
    if response.status_code == 200:
        assert audited_reads() == 1
    else:
        assert response.status_code >= 500
        assert pid not in response.get_data(as_text=True)
        assert audited_reads() == 0
