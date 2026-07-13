"""Application-factory and process-lifecycle contracts."""

from __future__ import annotations

import os
import subprocess
import sys

from sqlalchemy import inspect


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "APP_ENV": "testing",
        "TESTING": "1",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "STEP_UP_SECRET": "test-secret-for-hmac-validation",
    }
    env.pop("VERCEL", None)
    env.pop("HEALTHCLAW_LEGACY_BOOT", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_create_app_returns_distinct_configured_flask_apps():
    from flask import Flask
    from main import create_app

    first = create_app({"TESTING": True, "FACTORY_SENTINEL": "first"})
    second = create_app({"TESTING": True, "FACTORY_SENTINEL": "second"})

    assert isinstance(first, Flask)
    assert isinstance(second, Flask)
    assert first is not second
    assert first.config["FACTORY_SENTINEL"] == "first"
    assert second.config["FACTORY_SENTINEL"] == "second"
    first_routes = {rule.rule for rule in first.url_map.iter_rules()}
    assert "/" in first_routes
    assert "/r6/fhir/metadata" in first_routes
    assert "/api/subscribe" in first_routes


def test_import_and_factory_construction_do_not_run_lifecycle_actions():
    code = """
from unittest.mock import patch
from models import db
import r6.fasten.reaper
import r6.schema_sync
import r6.wearables.poller

with (
    patch.object(db, 'create_all') as create_all,
    patch.object(r6.schema_sync, 'reconcile_schema') as reconcile_schema,
    patch.object(r6.fasten.reaper, 'reap_zombie_jobs') as reap_zombie_jobs,
    patch.object(r6.wearables.poller, 'start_poller') as start_poller,
):
    import main
    another_app = main.create_app({'TESTING': True})

assert another_app is not main.app
for mocked in (create_all, reconcile_schema, reap_zombie_jobs, start_poller):
    assert mocked.call_count == 0, mocked.mock_calls
"""
    result = _run_python(code)
    assert result.returncode == 0, result.stdout + result.stderr


def test_database_initialization_is_an_explicit_operation():
    from main import create_app, initialize_database
    from models import db

    flask_app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
    })

    with flask_app.app_context():
        assert inspect(db.engine).get_table_names() == []

    initialize_database(flask_app)

    with flask_app.app_context():
        tables = set(inspect(db.engine).get_table_names())
    assert "r6_resources" in tables
    assert "smbp_sessions" in tables
    assert "fasten_connections" in tables


def test_lifecycle_operations_have_explicit_functions_and_cli_hooks():
    import main

    for name in (
        "initialize_database",
        "seed_demo_tenant",
        "recover_zombie_jobs",
        "start_wearables_poller",
    ):
        assert callable(getattr(main, name))

    flask_app = main.create_app({"TESTING": True})
    assert {"init-db", "seed-demo", "recover-zombies"} <= set(
        flask_app.cli.commands
    )
