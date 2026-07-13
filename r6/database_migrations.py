"""Explicit database migration lifecycle helpers."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy.engine import Engine


_ROOT = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    """Build repository-local Alembic configuration without a Flask app."""
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    return config


def upgrade_database(engine: Engine, revision: str = "head") -> str:
    """Upgrade an existing SQLAlchemy engine and return its applied revision."""
    config = alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
        current = MigrationContext.configure(connection).get_current_revision()
    if current is None:
        raise RuntimeError("Alembic upgrade completed without recording a revision")
    return current
