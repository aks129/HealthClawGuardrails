"""Database triggers that make audit_events append-only.

One source for the DDL, used twice: migration 0009 installs it on every
Alembic-managed database, and an ``after_create`` hook on AuditEventRecord's
table installs it wherever ``create_all`` builds the table (the test suite,
and ``_create_missing_baseline_tables`` when adopting a legacy database).

Every statement is idempotent, because both paths can reach the same
database: a legacy schema adopted at 0007 already carries the triggers from
create_all when 0009 runs.

What this does not cover, stated so nobody reads more into it:
  * TRUNCATE on Postgres — a row-level trigger does not fire for it.
  * A role that owns the table can drop or disable the trigger. This closes
    the application's own bulk and raw-SQL paths; it is not a ledger.
  * SQLite ``batch_alter_table`` on audit_events rebuilds the table and does
    not carry triggers across. A future migration that batch-alters this
    table must call ``install_statements`` again after it.
"""

from __future__ import annotations

_PG_FUNCTION = "audit_events_append_only"
_PG_TRIGGER = "audit_events_append_only"
_SQLITE_TRIGGERS = {
    "UPDATE": "audit_events_no_update",
    "DELETE": "audit_events_no_delete",
}

# No '%' and no ':word' in any statement: SQLAlchemy's DDL() applies %-format
# substitution and text() treats ':word' as a bind parameter.
_PG_INSTALL = [
    f"""CREATE OR REPLACE FUNCTION {_PG_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = 'audit_events is append-only, ' || TG_OP || ' refused';
END;
$$""",
    f"DROP TRIGGER IF EXISTS {_PG_TRIGGER} ON audit_events",
    f"""CREATE TRIGGER {_PG_TRIGGER}
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION {_PG_FUNCTION}()""",
]

_PG_REMOVE = [
    f"DROP TRIGGER IF EXISTS {_PG_TRIGGER} ON audit_events",
    f"DROP FUNCTION IF EXISTS {_PG_FUNCTION}()",
]

_SQLITE_INSTALL = [
    f"""CREATE TRIGGER IF NOT EXISTS {name}
BEFORE {op} ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only, {op} refused');
END"""
    for op, name in _SQLITE_TRIGGERS.items()
]

_SQLITE_REMOVE = [
    f"DROP TRIGGER IF EXISTS {name}" for name in _SQLITE_TRIGGERS.values()
]


def install_statements(dialect: str) -> list[str]:
    """DDL that installs the append-only triggers for ``dialect``."""
    return {"postgresql": _PG_INSTALL, "sqlite": _SQLITE_INSTALL}.get(dialect, [])


def remove_statements(dialect: str) -> list[str]:
    """DDL that removes them again (migration downgrade only)."""
    return {"postgresql": _PG_REMOVE, "sqlite": _SQLITE_REMOVE}.get(dialect, [])
