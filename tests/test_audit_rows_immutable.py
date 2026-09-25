"""The audit trail is append-only at the database, not only through the ORM.

The ORM listeners on AuditEventRecord fire on Session.delete() and on a dirty
flush. A bulk ``Query.update``/``Query.delete`` or a raw SQL statement never
passes through them, so before 0009 each of the four statements below changed
or removed a row without an error — the "immutable" in "Immutable Audit Trail"
held only for the code path that was already polite.

These tests run on whatever schema the ``app`` fixture builds: ``create_all``
on SQLite locally, the same on the Postgres CI lane. The migration path is
pinned separately in tests/test_database_migrations.py.

MUTATION: remove the after_create DDL hook in r6/models.py -> every refusal
test here is red, and the insert test stays green.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from models import db
from r6.models import AuditEventRecord

_TENANT = "audit-immutable-tenant"


def _seed(app) -> str:
    with app.app_context():
        event = AuditEventRecord(event_type="read", resource_type="Patient",
                                 resource_id="p-imm-1", tenant_id=_TENANT,
                                 agent_id="original-agent")
        db.session.add(event)
        db.session.commit()
        return event.id


def _row(app, event_id):
    with app.app_context():
        db.session.expire_all()
        return db.session.execute(
            text("SELECT agent_id, outcome FROM audit_events WHERE id = :i"),
            {"i": event_id},
        ).first()


def test_an_insert_still_works(app):
    event_id = _seed(app)
    assert _row(app, event_id) == ("original-agent", "success")


def test_a_bulk_query_update_is_refused(app):
    event_id = _seed(app)
    with app.app_context():
        with pytest.raises(DBAPIError):
            AuditEventRecord.query.filter_by(id=event_id).update(
                {"outcome": "failure"})
            db.session.commit()
        db.session.rollback()
    assert _row(app, event_id) == ("original-agent", "success")


def test_a_bulk_query_delete_is_refused(app):
    event_id = _seed(app)
    with app.app_context():
        with pytest.raises(DBAPIError):
            AuditEventRecord.query.filter_by(id=event_id).delete()
            db.session.commit()
        db.session.rollback()
    assert _row(app, event_id) is not None


def test_a_raw_sql_update_is_refused(app):
    event_id = _seed(app)
    with app.app_context():
        with pytest.raises(DBAPIError):
            db.session.execute(
                text("UPDATE audit_events SET agent_id = 'someone-else' "
                     "WHERE id = :i"), {"i": event_id})
            db.session.commit()
        db.session.rollback()
    assert _row(app, event_id) == ("original-agent", "success")


def test_a_raw_sql_delete_is_refused(app):
    event_id = _seed(app)
    with app.app_context():
        with pytest.raises(DBAPIError):
            db.session.execute(text("DELETE FROM audit_events"))
            db.session.commit()
        db.session.rollback()
    assert _row(app, event_id) is not None
