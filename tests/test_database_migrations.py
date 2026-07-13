"""Alembic lifecycle and schema-upgrade contracts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]
BASELINE_REVISION = "0001_v1_8_0"


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["database_url"] = url
    return config


def test_fresh_install_builds_current_schema_without_flask_app(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"

    command.upgrade(_config(url), "head")

    engine = create_engine(url)
    schema = inspect(engine)
    tables = set(schema.get_table_names())
    assert {
        "alembic_version",
        "r6_resources",
        "audit_events",
        "context_envelopes",
        "fasten_connections",
        "fasten_jobs",
        "proposed_actions",
        "action_events",
        "action_confirmations",
        "cc_conversation_messages",
        "cc_agent_tasks",
        "wearable_connections",
        "smbp_sessions",
        "telegram_bindings",
    } <= tables
    assert schema.get_pk_constraint("r6_resources")["constrained_columns"] == [
        "tenant_id",
        "resource_type",
        "id",
    ]
    resource_columns = {
        column["name"]: column for column in schema.get_columns("r6_resources")
    }
    assert resource_columns["id"]["type"].length == 255
    engine.dispose()


def test_migrations_are_explicit_and_never_delegate_to_create_all():
    migration_sources = "\n".join(
        path.read_text()
        for path in sorted((ROOT / "migrations" / "versions").glob("*.py"))
    )
    assert "create_all" not in migration_sources
    assert "reconcile_schema" not in migration_sources
    environment_source = (ROOT / "migrations" / "env.py").read_text()
    assert "import main" not in environment_source
    assert "from main import" not in environment_source


def test_initialize_database_runs_alembic_on_the_app_engine(monkeypatch):
    from main import create_app, initialize_database
    from models import db

    flask_app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        }
    )
    monkeypatch.setattr(db, "create_all", lambda: pytest.fail("create_all called"))

    revision = initialize_database(flask_app)

    with flask_app.app_context():
        schema = inspect(db.engine)
        assert "r6_resources" in schema.get_table_names()
        assert schema.get_pk_constraint("r6_resources")[
            "constrained_columns"
        ] == ["tenant_id", "resource_type", "id"]
    assert revision == "0002_current_contract"


def test_legacy_environment_flag_cannot_run_ddl_during_factory(monkeypatch):
    import main

    monkeypatch.setenv("HEALTHCLAW_LEGACY_BOOT", "1")
    monkeypatch.setattr(
        main,
        "run_legacy_boot_tasks",
        lambda _app: pytest.fail("mutable lifecycle ran during create_app"),
    )

    main.create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        }
    )


def test_deploy_configs_run_migrations_before_web_processes():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    assert services["migrate"]["command"] == ["flask", "--app", "main", "init-db"]
    assert services["fhir-mcp-guardrails"]["depends_on"]["migrate"] == {
        "condition": "service_completed_successfully"
    }
    assert services["seed-demo"]["depends_on"]["migrate"] == {
        "condition": "service_completed_successfully"
    }

    railway = (ROOT / "railway.toml").read_text()
    migrate = '"flask --app main init-db"'
    seed = '"flask --app main seed-demo --tenant-id desktop-demo"'
    assert "preDeployCommand" in railway
    assert railway.index(migrate) < railway.index(seed)
    assert "SEED_DEMO_TENANT" not in railway


def test_postgres_fresh_install_and_v1_8_upgrade_path():
    """Exercise both operator paths against a disposable CI database."""
    url = os.environ.get("MIGRATION_TEST_DATABASE_URL", "")
    if not url.startswith(("postgresql://", "postgres://")):
        pytest.skip("MIGRATION_TEST_DATABASE_URL is not configured for Postgres")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    config = _config(url)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))

    try:
        # A completely new deployment reaches the current schema.
        command.upgrade(config, "head")
        command.check(config)
        schema = inspect(engine)
        assert schema.get_pk_constraint("r6_resources")[
            "constrained_columns"
        ] == ["tenant_id", "resource_type", "id"]
        assert {
            col["name"]: getattr(col["type"], "length", None)
            for col in schema.get_columns("fasten_jobs")
        }["task_id"] == 255

        # Rehearse an existing v1.8 deployment at the compatibility baseline
        # before the former boot-time schema_sync changes. The contract
        # migration must preserve data while adding/widening known fields.
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        command.upgrade(config, BASELINE_REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO r6_resources
                        (id, resource_type, version_id, last_updated,
                         resource_json, sha256, tenant_id)
                    VALUES
                        ('legacy-id', 'Patient', 1, NOW(), '{}',
                         repeat('0', 64), 'legacy-tenant');
                    """
                )
            )
        command.upgrade(config, "head")
        command.check(config)

        schema = inspect(engine)
        assert schema.get_pk_constraint("r6_resources")[
            "constrained_columns"
        ] == ["tenant_id", "resource_type", "id"]
        r6_columns = {
            col["name"]: getattr(col["type"], "length", None)
            for col in schema.get_columns("r6_resources")
        }
        assert r6_columns["id"] == 255
        assert {"curation_state", "quality_score", "review_needed"} <= set(
            r6_columns
        )
        fasten_columns = {
            col["name"]: getattr(col["type"], "length", None)
            for col in schema.get_columns("fasten_connections")
        }
        assert fasten_columns["org_connection_id"] == 255
        assert {
            "webhook_verified_at",
            "agent_token_issued_at",
            "enrollment_proof_hash",
            "enrollment_expires_at",
        } <= set(fasten_columns)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT resource_json FROM r6_resources")
            ).scalar_one() == "{}"
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        engine.dispose()
