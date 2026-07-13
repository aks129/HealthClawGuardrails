"""Explicit database migration lifecycle helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Engine


logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]

# The revision that mirrors the schema every pre-Alembic deployment built via
# db.create_all(). A database that has real tables but no alembic_version row
# is exactly that legacy state and must be ADOPTED (stamped at this baseline),
# never re-created — running the baseline migration against it dies with
# "table ... already exists".
_BASELINE_REVISION = "0001_v1_8_0"

# A table that has existed since long before v1.8.0 — its presence (without
# alembic_version) is the legacy-database fingerprint.
_LEGACY_SENTINEL_TABLE = "r6_resources"


def alembic_config() -> Config:
    """Build repository-local Alembic configuration without a Flask app."""
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    return config


def _is_unstamped_legacy_database(connection) -> bool:
    """True when the schema was built by pre-Alembic create_all: real tables
    exist but Alembic has never recorded a revision.

    Checks the recorded REVISION, not mere table presence — an interrupted
    earlier run can leave an empty alembic_version table behind, and that
    state is still unstamped."""
    current = MigrationContext.configure(connection).get_current_revision()
    if current is not None:
        return False
    inspector = inspect(connection)
    return _LEGACY_SENTINEL_TABLE in inspector.get_table_names()


def upgrade_database(engine: Engine, revision: str = "head") -> str:
    """Upgrade an existing SQLAlchemy engine and return its applied revision.

    Handles all three deployment states:
      - fresh database          -> run every migration from scratch
      - legacy create_all-era   -> stamp the v1.8.0 baseline, then upgrade;
                                   0002 reconciles any drift idempotently
      - already Alembic-managed -> normal upgrade to the target revision
    """
    config = alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        if _is_unstamped_legacy_database(connection):
            logger.info(
                "Existing pre-Alembic schema detected (no alembic_version); "
                "stamping baseline %s before upgrading", _BASELINE_REVISION,
            )
            command.stamp(config, _BASELINE_REVISION)
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
