"""Ingest waits for a concurrent SQLite writer instead of failing.

On SQLite an upload failed every entry with `database is locked` whenever
another process (the CareAgents worker writing run events) held the write
lock. Each entry opens a SAVEPOINT, which starts a deferred transaction;
the entry then reads (does this id exist?) and writes. SQLite never runs
the busy handler for a read-to-write upgrade, so the write failed at once
instead of waiting out the busy timeout.

The fix starts a SAVEPOINT-opened transaction with BEGIN IMMEDIATE, so the
write lock is taken before the read, through the busy handler.

Needs a file database: the in-memory test default shares one connection,
which cannot exhibit cross-connection locking. Postgres lanes skip.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("SQLALCHEMY_DATABASE_URI", "").startswith("sqlite"),
    reason="SQLite-only locking behaviour",
)

_ENDPOINT = "/r6/fhir/internal/ingest-bundle"
_HOLD_SECONDS = 1.0


@pytest.fixture
def file_app(tmp_path):
    from main import create_app
    from models import db

    path = tmp_path / "lock.db"
    flask_app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}",
        "LEGACY_BOOT_ON_CREATE": False,
    })
    with flask_app.app_context():
        db.create_all()
        from r6.rate_limit import _rate_limits
        _rate_limits.clear()
        yield flask_app, path
        db.session.remove()
        db.engine.dispose()


def _hold_write_lock(path, locked: threading.Event):
    other = sqlite3.connect(path, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        locked.set()
        threading.Event().wait(_HOLD_SECONDS)
        other.execute("COMMIT")
    finally:
        other.close()


def test_ingest_waits_for_a_concurrent_writer(file_app):
    flask_app, path = file_app
    from r6.models import R6Resource

    resources = [{"resourceType": "Patient", "id": f"p-{i}",
                  "name": [{"family": "Lock", "given": [str(i)]}]}
                 for i in range(3)]
    body = {"bundle": {"resourceType": "Bundle", "type": "collection",
                       "entry": [{"resource": r} for r in resources]}}

    locked = threading.Event()
    holder = threading.Thread(target=_hold_write_lock,
                              args=(path, locked))
    holder.start()
    assert locked.wait(5)
    try:
        r = flask_app.test_client().post(
            _ENDPOINT, data=json.dumps(body),
            headers={"X-Tenant-Id": "test-tenant",
                     "Content-Type": "application/json"})
    finally:
        holder.join()

    payload = r.get_json()
    assert r.status_code == 200, payload
    assert payload["failed"] == 0, payload["errors"]
    assert payload["ingested"] == len(resources)
    assert R6Resource.query.filter_by(
        tenant_id="test-tenant", resource_type="Patient").count() == 3


def test_postgres_engines_get_no_sqlite_begin(monkeypatch):
    """The hook only ever touches SQLite connections."""
    from r6.sqlite_locking import _begin_immediate_before_savepoint

    class _Dialect:
        name = "postgresql"

    class _Dbapi:
        in_transaction = False
        isolation_level = ""

    class _Pooled:
        dbapi_connection = _Dbapi()

    class _Conn:
        # Shaped like an idle pysqlite connection, so only the dialect
        # guard stands between the hook and the SQL below.
        dialect = _Dialect()
        connection = _Pooled()

        def exec_driver_sql(self, *_a, **_k):
            raise AssertionError("must not emit SQL on Postgres")

    _begin_immediate_before_savepoint(_Conn(), "sa_savepoint_1")
