"""
r6.schema_sync — idempotent column reconciler for long-lived databases.

``db.create_all()`` only creates MISSING tables; it will not add new columns
to existing tables. When the ORM model gains a column (e.g. v1.2 added
``curation_state`` to R6Resource), pre-existing Postgres deployments end
up with a schema mismatch that shows as ``UndefinedColumn`` on every read.

This module inspects the live database after ``create_all()`` and issues
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` for any column declared on a
SQLAlchemy model but absent from the DB. It is a no-op for SQLite (fresh
files already match the model) and for Postgres DBs that are already in
sync.

Designed to run on every boot — fast (one introspection query per table)
and safe (IF NOT EXISTS so concurrent workers don't fight).
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def _format_server_default(column) -> str:
    """Return a SQL DEFAULT clause for the column, or empty string."""
    # Prefer server_default if set
    if column.server_default is not None:
        default = column.server_default.arg
        if hasattr(default, "text"):
            return f" DEFAULT {default.text}"
        return f" DEFAULT {default}"
    # Fall back to the ORM-side default if it's a simple scalar
    if column.default is not None and hasattr(column.default, "arg"):
        arg = column.default.arg
        # Skip callables (e.g. lambda: datetime.now) — we can't inline them
        if callable(arg):
            return ""
        if isinstance(arg, bool):
            return f" DEFAULT {'TRUE' if arg else 'FALSE'}"
        if isinstance(arg, (int, float)):
            return f" DEFAULT {arg}"
        if isinstance(arg, str):
            escaped = arg.replace("'", "''")
            return f" DEFAULT '{escaped}'"
    return ""


def reconcile_schema(engine: Engine, metadata) -> list[str]:
    """
    Add any ORM-declared columns that are missing from the live DB.

    Returns a list of SQL statements executed (for logging).
    """
    added: list[str] = []
    if engine.dialect.name != "postgresql":
        # SQLite recreates the schema from the model each boot via create_all()
        # so there's nothing to reconcile. (Attempting ALTER TABLE ADD COLUMN
        # IF NOT EXISTS on SQLite also isn't supported pre-3.35.)
        return added

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in metadata.tables.values():
        if table.name not in existing_tables:
            # create_all() should have already handled this
            continue

        db_columns = {c["name"]: c for c in inspector.get_columns(table.name)}
        db_cols = set(db_columns)

        for col in table.columns:
            if col.name in db_cols:
                # Column exists — widen it if the model's String is now longer
                # than the live varchar. Real EHR ids exceed the old
                # varchar(64) (Epic ids up to ~109); widening varchar length
                # in Postgres is safe and online (no table rewrite). Never
                # narrows. (found live 2026-07-08 — a too-narrow id column
                # truncation-errored mid-ingest and poisoned the batch.)
                model_len = getattr(col.type, "length", None)
                live_type = db_columns[col.name].get("type")
                live_len = getattr(live_type, "length", None)
                if (isinstance(model_len, int) and isinstance(live_len, int)
                        and model_len > live_len):
                    stmt = (
                        f"ALTER TABLE {table.name} "
                        f"ALTER COLUMN {col.name} TYPE VARCHAR({model_len})"
                    )
                    logger.info("schema_sync (widen): %s", stmt)
                    try:
                        with engine.begin() as conn:
                            conn.execute(text(stmt))
                        added.append(stmt)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("schema_sync widen failed for "
                                       "%s.%s: %s", table.name, col.name, e)
                continue
            col_type = col.type.compile(engine.dialect)
            default_sql = _format_server_default(col)
            # Always allow NULL for ADDed columns — enforcing NOT NULL on an
            # existing table without a default would fail.
            stmt = (
                f"ALTER TABLE {table.name} "
                f"ADD COLUMN IF NOT EXISTS {col.name} {col_type}{default_sql}"
            )
            logger.info("schema_sync: %s", stmt)
            with engine.begin() as conn:
                conn.execute(text(stmt))
            added.append(stmt)

    return added
