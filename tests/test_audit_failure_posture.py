"""Audit-write failure must never become silent data loss (issue #182).

The original handler opened a SAVEPOINT but rolled back the WHOLE session on
failure, discarding the caller's uncommitted work — and swallowed the error, so
`POST /r6/fhir/internal/seed` answered 201/created-7 while persisting zero rows.

Two guarantees are asserted here:
  1. a failed audit rolls back only its savepoint, leaving the caller's
     pending work intact, and
  2. the failure is raised, so an un-audited access can never be reported as
     success ("every access emits an AuditEvent" is a graded conformance
     property — proceeding unrecorded is the worse failure).
"""

import pytest

from models import db
from r6.audit import AuditWriteError, record_audit_event
from r6.models import R6Resource


def _break_audit_inserts(monkeypatch):
    """Make every AuditEvent construction fail, like a stale/broken schema."""
    def boom(*args, **kwargs):
        raise RuntimeError("simulated audit insert failure")
    monkeypatch.setattr("r6.audit._new_audit_event", boom)


def test_audit_failure_raises_instead_of_reporting_success(client, tenant_id,
                                                           monkeypatch):
    _break_audit_inserts(monkeypatch)
    with pytest.raises(AuditWriteError):
        record_audit_event("read", resource_type="Patient",
                           resource_id="p-1", tenant_id=tenant_id)


def test_audit_failure_does_not_discard_the_callers_pending_work(
        client, tenant_id, monkeypatch):
    # The caller has staged a resource but not committed it yet — exactly the
    # seed path's shape, where record_audit_event used to perform the commit.
    pending = R6Resource(resource_type="Observation", resource_json="{}",
                         resource_id="audit-posture-1", tenant_id=tenant_id)
    db.session.add(pending)

    _break_audit_inserts(monkeypatch)
    with pytest.raises(AuditWriteError):
        record_audit_event("create", resource_type="Observation",
                           resource_id="audit-posture-1", tenant_id=tenant_id)

    # The savepoint rollback must NOT have taken the caller's work with it:
    # the staged resource is still in the session and can still be committed.
    db.session.commit()
    got = R6Resource.query.filter_by(tenant_id=tenant_id,
                                     resource_type="Observation",
                                     id="audit-posture-1").first()
    assert got is not None, "audit failure wiped the caller's uncommitted work"


def test_seed_endpoint_fails_loudly_rather_than_persisting_nothing(
        client, tenant_id, monkeypatch):
    # The exact #182 repro: with audit inserts broken, seeding must not answer
    # 201/created-N while the tenant ends up empty. Under TESTING the error
    # propagates; in production Flask turns the same failure into a 500 —
    # either way the caller is never told "created" when nothing was.
    _break_audit_inserts(monkeypatch)
    try:
        resp = client.post("/r6/fhir/internal/seed",
                           json={"tenant_id": tenant_id})
    except AuditWriteError:
        return  # failed loudly, which is the contract
    assert resp.status_code >= 500, (
        f"seed answered {resp.status_code} while audit was failing "
        "— the #182 regression (success with nothing persisted)")
    assert R6Resource.query.filter_by(tenant_id=tenant_id).count() == 0


def test_healthy_audit_path_still_records(client, tenant_id):
    # Guard the guard: the fail-loud change must not break normal auditing.
    record_audit_event("read", resource_type="Patient", resource_id="p-ok",
                       tenant_id=tenant_id)
    from r6.models import AuditEventRecord
    assert AuditEventRecord.query.filter_by(
        tenant_id=tenant_id, resource_id="p-ok").first() is not None
