"""Explicit database migration lifecycle helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from models import db


logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]

# The revision that mirrors the schema a v1.8.0 pre-Alembic deployment built
# via db.create_all(). A database that has real tables but no alembic_version
# row is that legacy state and must be ADOPTED (stamped at this baseline),
# never re-created — running the baseline migration against it dies with
# "table ... already exists". An older create_all built a SUBSET of the
# baseline tables; _create_missing_baseline_tables fills the gap before the
# stamp so 0002 does not die on the first table it inspects.
_BASELINE_REVISION = "0001_v1_8_0"
_CREATE_ALL_SCHEMA_REVISION = "0007_agent_worker_presence"

# A table that has existed since long before v1.8.0 — its presence (without
# alembic_version) is the legacy-database fingerprint.
_LEGACY_SENTINEL_TABLE = "r6_resources"


def alembic_config() -> Config:
    """Build repository-local Alembic configuration without a Flask app."""
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    return config


def register_model_metadata() -> None:
    """Import every model module so ``db.metadata`` describes every table."""
    import r6.actions.confirmations  # noqa: F401
    import r6.actions.events  # noqa: F401
    import r6.actions.models  # noqa: F401
    import r6.agent_runs.models  # noqa: F401
    import r6.command_center.models  # noqa: F401
    import r6.fasten.models  # noqa: F401
    import r6.models  # noqa: F401
    import r6.smbp.models  # noqa: F401
    import r6.wearables.models  # noqa: F401


def _unstamped_adoption_revision(connection) -> str | None:
    """Revision to stamp when ``create_all`` built an unstamped schema.

    Older deployments have the v1.8 baseline shape. A current checkout can
    also create a full present-day schema before the migration command runs;
    stamping that at the old baseline would replay DDL for tables it already
    contains. Distinguish the two shapes using the first table introduced
    after the baseline.

    Checks the recorded REVISION, not mere table presence — an interrupted
    earlier run can leave an empty alembic_version table behind, and that
    state is still unstamped."""
    current = MigrationContext.configure(connection).get_current_revision()
    if current is not None:
        return None
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if _LEGACY_SENTINEL_TABLE not in tables:
        return None
    if "cc_conversations" in tables:
        message_columns = {
            column["name"]
            for column in inspector.get_columns("cc_conversation_messages")
        }
        resource_pk = inspector.get_pk_constraint("r6_resources").get(
            "constrained_columns", [])
        audit_columns = {
            column["name"] for column in inspector.get_columns("audit_events")
        }
        if (
            {"conversation_id", "request_id", "reply_to"} <= message_columns
            and resource_pk == ["tenant_id", "resource_type", "id"]
            and "outcome_detail_code" in audit_columns
            and "agent_runs" in tables
            and "agent_tool_calls" in tables
            and "agent_run_events" in tables
            and "agent_worker_presence" in tables
        ):
            return _CREATE_ALL_SCHEMA_REVISION
    return _BASELINE_REVISION


def _baseline_tables() -> set[str]:
    """Names of the tables migration 0001 creates.

    Read from the migration itself, by running it against a scratch
    in-memory SQLite database, so the set cannot drift from the DDL."""
    scratch = create_engine("sqlite://")
    try:
        with scratch.connect() as connection:
            config = alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, _BASELINE_REVISION)
            return set(inspect(connection).get_table_names()) - {"alembic_version"}
    finally:
        scratch.dispose()


def _create_missing_baseline_tables(connection) -> list[str]:
    """Create the v1.8.0 baseline tables an older ``create_all`` never built.

    Stamping at 0001 asserts the database has every table 0001 creates, and
    0002 inspects those tables — it raises NoSuchTableError on the first one
    that is absent. Create only the absent BASELINE tables, from current model
    metadata: that is the shape of a fully reconciled v1.8 database, which
    0002 already handles column by column. Tables introduced after the
    baseline stay absent so 0004–0007 can create them."""
    register_model_metadata()
    present = set(inspect(connection).get_table_names())
    missing = sorted(_baseline_tables() - present)
    if missing:
        db.metadata.create_all(
            bind=connection,
            tables=[db.metadata.tables[name] for name in missing],
            checkfirst=True,
        )
    return missing


def upgrade_database(engine: Engine, revision: str = "head") -> str:
    """Upgrade an existing SQLAlchemy engine and return its applied revision.

    Handles all three deployment states:
      - fresh database          -> run every migration from scratch
      - legacy create_all-era   -> create any v1.8.0 baseline table the old
                                   create_all never built, stamp the v1.8.0
                                   baseline, then upgrade; 0002 reconciles
                                   column drift idempotently
      - already Alembic-managed -> normal upgrade to the target revision
    """
    config = alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        adoption_revision = _unstamped_adoption_revision(connection)
        if adoption_revision:
            created = _create_missing_baseline_tables(connection)
            logger.info(
                "Existing pre-Alembic schema detected (no alembic_version); "
                "created %d missing baseline table(s) %s and stamping "
                "revision %s before upgrading",
                len(created),
                created,
                adoption_revision,
            )
            command.stamp(config, adoption_revision)
        command.upgrade(config, revision)
        current = MigrationContext.configure(connection).get_current_revision()
        # Alembic treats a SUPPLIED connection as externally managed and does
        # not commit it; SQLAlchemy 2.0 rolls back at connection close. On
        # SQLite the DDL autocommits at the driver level but the
        # alembic_version row insert does not — leaving tables present with no
        # recorded revision, so the NEXT deploy re-runs every migration and
        # dies with "table already exists". Commit explicitly.
        connection.commit()
    if current is None:
        raise RuntimeError("Alembic upgrade completed without recording a revision")
    return current
