"""Application-independent Alembic environment."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from models import db

# Import model modules only. This populates the shared metadata without
# importing main.py or constructing the WSGI application.
import r6.actions.confirmations  # noqa: F401
import r6.actions.events  # noqa: F401
import r6.actions.models  # noqa: F401
import r6.command_center.models  # noqa: F401
import r6.fasten.models  # noqa: F401
import r6.models  # noqa: F401
import r6.smbp.models  # noqa: F401
import r6.wearables.models  # noqa: F401


config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False is deliberate: upgrade_database() runs
    # migrations IN-PROCESS from the app (initialize_database), and the default
    # (True) would disable every already-configured application logger
    # ('openclaw', 'request', ...), silently dropping their log records for the
    # rest of the process. Alembic's own logging config still applies.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = db.metadata


def _database_url() -> str:
    url = (
        config.attributes.get("database_url")
        or os.environ.get("SQLALCHEMY_DATABASE_URI")
        or os.environ.get("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
    )
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        _run_with_connection(supplied_connection)
        return

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run_with_connection(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
