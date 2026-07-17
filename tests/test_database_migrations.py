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
    assert "outcome_detail_code" in {
        column["name"] for column in schema.get_columns("audit_events")
    }
    engine.dispose()


def test_audit_outcome_detail_migration_is_reversible(tmp_path):
    url = f"sqlite:///{tmp_path / 'audit-outcome.db'}"
    config = _config(url)
    engine = create_engine(url)

    command.upgrade(config, "head")
    assert "outcome_detail_code" in {
        column["name"] for column in inspect(engine).get_columns("audit_events")
    }

    command.downgrade(config, "0002_current_contract")
    assert "outcome_detail_code" not in {
        column["name"] for column in inspect(engine).get_columns("audit_events")
    }

    command.upgrade(config, "head")
    assert "outcome_detail_code" in {
        column["name"] for column in inspect(engine).get_columns("audit_events")
    }
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
    assert revision == "0003_audit_outcome_detail"


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
    assert "preDeployCommand" in railway
    # Migration must run before seeding. They are chained in ONE preDeploy
    # command because Railway's preDeployCommand accepts at most one element —
    # a multi-element array is a config parse error that fails the deploy.
    assert railway.index("flask --app main init-db") < railway.index(
        "flask --app main seed-demo --tenant-id desktop-demo"
    )
    import tomllib
    pre = tomllib.loads(railway)["deploy"]["preDeployCommand"]
    assert isinstance(pre, list) and len(pre) == 1, pre
    assert "&&" in pre[0]
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


def test_legacy_create_all_database_is_adopted_not_recreated(tmp_path):
    """THE PROD UPGRADE PATH: every deployed database predating #103 was built
    by db.create_all() and has real tables but NO alembic_version. Running the
    baseline migration against it dies with 'table already exists' (found live:
    the compose migrate service failed exactly this way on a reused volume).
    upgrade_database() must detect that state, STAMP the baseline, and then
    upgrade to head — 0002 reconciles drift idempotently (inspects first)."""
    from r6.database_migrations import upgrade_database
    import main as main_module  # registers every model on db.metadata
    from models import db

    main_module.register_model_metadata()
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(url)
    db.metadata.create_all(engine)  # what every pre-Alembic deploy did at boot

    inspector = inspect(engine)
    assert "r6_resources" in inspector.get_table_names()
    assert "alembic_version" not in inspector.get_table_names()

    revision = upgrade_database(engine)  # must NOT raise 'already exists'

    assert revision == "0003_audit_outcome_detail"
    inspector = inspect(engine)
    assert "alembic_version" in inspector.get_table_names()
    # And it must be repeatable (deploys run it every release).
    assert upgrade_database(engine) == "0003_audit_outcome_detail"


def test_pre_w0_sqlite_database_with_unnamed_pk_upgrades(tmp_path):
    """A pre-W0 SQLite deployment (docker-compose default) has r6_resources
    with an UNNAMED single-column primary key, VARCHAR(64) id, and nullable
    tenant_id. 0002 must rebuild it to the composite identity without dying on
    'No such constraint' (found live: compose migrate failed exactly here on a
    reused volume)."""
    from r6.database_migrations import upgrade_database
    import main as main_module
    from models import db

    main_module.register_model_metadata()
    url = f"sqlite:///{tmp_path / 'pre-w0.db'}"
    engine = create_engine(url)
    # Realistic legacy state: the complete schema the old boot path built,
    # with r6_resources swapped for its pre-W0 shape (single unnamed PK,
    # 64-char id, nullable tenant). A DB missing whole tables is older than
    # any supported deployment — 0002 fails loud there by design.
    db.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("DROP TABLE r6_resources"))
        c.execute(text(
            "CREATE TABLE r6_resources ("
            " id VARCHAR(64) NOT NULL PRIMARY KEY,"
            " resource_type VARCHAR(64) NOT NULL,"
            " tenant_id VARCHAR(64),"
            " resource_json TEXT NOT NULL,"
            " version_id INTEGER,"
            " last_updated DATETIME)"
        ))
        c.execute(text(
            "INSERT INTO r6_resources (id, resource_type, tenant_id,"
            " resource_json, version_id) VALUES"
            " ('p1', 'Patient', 't1', '{}', 1)"
        ))

    revision = upgrade_database(engine)
    assert revision == "0003_audit_outcome_detail"

    inspector = inspect(engine)
    pk = inspector.get_pk_constraint("r6_resources")
    assert pk["constrained_columns"] == ["tenant_id", "resource_type", "id"]
    # Data survives the rebuild.
    with engine.connect() as c:
        rows = list(c.execute(text("SELECT id, tenant_id FROM r6_resources")))
    assert rows == [("p1", "t1")]


def test_legacy_create_all_upgrade_on_configured_database():
    """The prod-down bug: a create_all-era database (real tables, no
    alembic_version) must be ADOPTED, not re-created. Runs against whatever
    SQLALCHEMY_DATABASE_URI is configured — the Postgres CI lane exercises the
    real Railway scenario; elsewhere it uses a temp SQLite file."""
    import tempfile
    from r6.database_migrations import upgrade_database
    import main as main_module
    from models import db

    main_module.register_model_metadata()

    url = os.environ.get("SQLALCHEMY_DATABASE_URI", "")
    is_pg = url.startswith(("postgresql://", "postgres://"))
    if is_pg:
        engine = create_engine(url.replace("postgres://", "postgresql://", 1))
        with engine.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE"))
            c.execute(text("CREATE SCHEMA public"))
        cleanup = engine
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        engine = create_engine(f"sqlite:///{tmp.name}")
        cleanup = None

    try:
        db.metadata.create_all(engine)  # pre-Alembic boot path
        assert "alembic_version" not in inspect(engine).get_table_names()

        revision = upgrade_database(engine)  # must not raise "already exists"

        assert revision == "0003_audit_outcome_detail"
        assert "alembic_version" in inspect(engine).get_table_names()
        assert upgrade_database(engine) == "0003_audit_outcome_detail"  # idempotent
        assert inspect(engine).get_pk_constraint("r6_resources")[
            "constrained_columns"
        ] == ["tenant_id", "resource_type", "id"]
    finally:
        if cleanup is not None:
            with cleanup.begin() as c:
                c.execute(text("DROP SCHEMA public CASCADE"))
                c.execute(text("CREATE SCHEMA public"))
        engine.dispose()
