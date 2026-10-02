"""Take SQLite's write lock up front when a SAVEPOINT opens a transaction.

pysqlite (Python < 3.12 default, "legacy" transaction control) issues its
own BEGIN only before INSERT/UPDATE/DELETE, so a plain write starts its
transaction holding no lock and SQLite runs the busy handler (5s by
default) while it waits for the write lock. A SAVEPOINT is different: run
outside a transaction it opens a DEFERRED one. `begin_nested()` callers
(ingest, `add_audit_event`) then read before they write, and SQLite never
runs the busy handler for a read-to-write upgrade: with another connection
writing, the upgrade fails at once with `database is locked`. Under the
CareAgents worker that failed whole uploads on the self-hosted SQLite stack.

So when a SAVEPOINT is about to open a transaction, open it first with
BEGIN IMMEDIATE: the write lock is taken through the busy handler, and
the read that follows happens under it. Nothing else changes. Transactions
pysqlite begins itself are untouched, and read-only work never takes the
write lock (the SSE audit stream holds one session open indefinitely, so a
global BEGIN IMMEDIATE would block every writer while it runs). Postgres
and every other dialect are skipped. The hook is registered on the Engine
class, so it covers every SQLite engine in the process, not only this app's.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import Engine


def _begin_immediate_before_savepoint(conn, name) -> None:
    if conn.dialect.name != "sqlite":
        return
    dbapi_conn = conn.connection.dbapi_connection
    if dbapi_conn is None or dbapi_conn.in_transaction:
        return
    # A pysqlite connection in autocommit mode (isolation_level None) has
    # its BEGIN emitted by whoever configured it; leave that alone.
    if dbapi_conn.isolation_level is None:
        return
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def install() -> None:
    """Register the hook on every Engine. Idempotent."""
    if not event.contains(Engine, "savepoint",
                          _begin_immediate_before_savepoint):
        event.listen(Engine, "savepoint", _begin_immediate_before_savepoint)
