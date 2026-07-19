"""Flask application factory and WSGI entry point for HealthClaw Guardrails.

``create_app`` only configures an application. Database DDL, schema
reconciliation, seeding, recovery jobs, and background workers are explicit
lifecycle operations so importing ``main`` is safe in tests, CLIs, and WSGI
worker processes.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click
from flask import Flask, g, request as flask_request
from models import db
from r6.database_migrations import upgrade_database
from r6.runtime_config import validate_runtime_environment


logger = logging.getLogger(__name__)
request_logger = logging.getLogger("request")
_ROOT_DIR = Path(__file__).resolve().parent
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class JSONFormatter(logging.Formatter):
    """Compact structured formatter used by production deployments."""

    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


def _is_true(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE_VALUES


def _configure_logging(app_env: str) -> None:
    log_level = os.environ.get(
        "LOG_LEVEL", "DEBUG" if app_env == "development" else "INFO"
    )
    level = getattr(logging, log_level.upper(), logging.INFO)
    if app_env == "production" or os.environ.get("LOG_FORMAT") == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logging.root.handlers = [handler]
        logging.root.setLevel(level)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )


def _database_uri(app_env: str, settings: Mapping[str, Any]) -> str:
    configured = settings.get("SQLALCHEMY_DATABASE_URI")
    db_uri = str(configured).strip() if configured else os.environ.get(
        "SQLALCHEMY_DATABASE_URI", ""
    ).strip()
    if db_uri:
        return db_uri
    if os.environ.get("VERCEL"):
        return "sqlite:////tmp/mcp_server.db"
    if app_env == "production":
        raise RuntimeError(
            "SQLALCHEMY_DATABASE_URI environment variable is required in "
            "production. SQLite is not suitable for production use."
        )
    return "sqlite:///mcp_server.db"


def register_model_metadata() -> None:
    """Import every model module that database lifecycle tasks must see."""
    from r6.models import R6Resource  # noqa: F401
    import r6.actions.confirmations  # noqa: F401
    import r6.actions.events  # noqa: F401
    import r6.actions.models  # noqa: F401
    import r6.command_center.models  # noqa: F401
    import r6.fasten.models  # noqa: F401
    import r6.smbp.models  # noqa: F401
    import r6.wearables.models  # noqa: F401


def initialize_database(flask_app: Flask) -> str:
    """Upgrade the configured database to the current Alembic revision."""
    register_model_metadata()
    with flask_app.app_context():
        revision = upgrade_database(db.engine)
    logger.info("Database upgraded to Alembic revision %s", revision)
    return revision


def seed_demo_tenant(flask_app: Flask, tenant_id: str | None = None) -> int:
    """Seed one demo tenant if it has no existing resources."""
    from r6.models import R6Resource
    from r6.seed import seed_demo_data

    selected_tenant = tenant_id or flask_app.config.get(
        "DEMO_TENANT_ID", "desktop-demo"
    )
    with flask_app.app_context():
        existing = R6Resource.query.filter_by(tenant_id=selected_tenant).first()
        if existing is not None:
            logger.info(
                "Demo tenant '%s' already has data, skipping auto-seed",
                selected_tenant,
            )
            return 0
        count = seed_demo_data(selected_tenant)
        logger.info("Seeded %d resources into tenant '%s'", count, selected_tenant)
        return count


def recover_zombie_jobs(flask_app: Flask) -> int:
    """Run Fasten restart recovery without allowing it to block a deploy."""
    try:
        from r6.fasten.reaper import reap_zombie_jobs

        with flask_app.app_context():
            reaped = reap_zombie_jobs()
        if reaped:
            logger.info("Fasten reaper re-triggered %d zombie job(s)", reaped)
        return reaped
    except Exception as exc:  # noqa: BLE001
        logger.error("Fasten reaper failed (non-fatal): %s", exc)
        return 0


def start_wearables_poller(flask_app: Flask) -> bool:
    """Explicitly start the in-process wearables poller when supported."""
    if _is_true(flask_app.config.get("VERCEL")):
        logger.info("Wearables poller disabled on Vercel")
        return False
    from r6.wearables.poller import start_poller

    started = start_poller(flask_app)
    if started:
        logger.info("Wearables poller started (background thread)")
    return started


def run_legacy_boot_tasks(flask_app: Flask) -> None:
    """Run the pre-factory boot sequence for explicitly opted-in deployments."""
    initialize_database(flask_app)
    recover_zombie_jobs(flask_app)
    if _is_true(flask_app.config.get("SEED_DEMO_TENANT")):
        seed_demo_tenant(flask_app)
    start_wearables_poller(flask_app)


def _register_lifecycle_cli(flask_app: Flask) -> None:
    @flask_app.cli.command("init-db")
    def init_db_command() -> None:
        """Upgrade the configured database to the current schema."""
        revision = initialize_database(flask_app)
        click.echo(f"Database upgraded to Alembic revision {revision}.")

    @flask_app.cli.command("seed-demo")
    @click.option("--tenant-id", default=None, help="Tenant to seed.")
    def seed_demo_command(tenant_id: str | None) -> None:
        """Seed an empty demo tenant."""
        count = seed_demo_tenant(flask_app, tenant_id)
        click.echo(f"Seeded {count} resource(s).")

    @flask_app.cli.command("recover-zombies")
    def recover_zombies_command() -> None:
        """Retry eligible Fasten jobs stranded by a process restart."""
        count = recover_zombie_jobs(flask_app)
        click.echo(f"Recovered {count} job(s).")

    @flask_app.cli.command("legacy-boot")
    def legacy_boot_command() -> None:
        """Run all legacy boot tasks once under operator control."""
        run_legacy_boot_tasks(flask_app)
        click.echo("Legacy boot tasks completed.")


def _register_blueprints(flask_app: Flask) -> None:
    from r6.routes import r6_blueprint

    flask_app.register_blueprint(r6_blueprint)

    from r6.fasten.routes import fasten_blueprint

    flask_app.register_blueprint(fasten_blueprint)

    from r6.actions.routes import actions_blueprint
    from r6.actions.registry import all_kinds as action_kinds
    import r6.actions.rails

    r6.actions.rails.register_all()
    # Import for side effect: registers the /<id>/review GET+POST routes on
    # actions_blueprint (Task 6 structured per-item review page). MUST precede
    # register_blueprint — routes can't be added to a blueprint after it is
    # registered on the app.
    import r6.actions.review  # noqa: F401

    flask_app.register_blueprint(actions_blueprint)
    logger.info(
        "Actions Blueprint registered at /r6/actions (rails: %s)",
        ", ".join(action_kinds()),
    )

    from r6.ops.routes import ops_blueprint

    flask_app.register_blueprint(ops_blueprint)

    from r6.smbp.routes import smbp_blueprint

    flask_app.register_blueprint(smbp_blueprint)

    # SDC delivery Blueprint — public signed download route for intake PDFs
    # (Task 7). On its OWN blueprint (not r6_blueprint) so it is reachable
    # without X-Tenant-Id / X-Step-Up-Token headers: the signed URL is the
    # credential.
    from r6.sdc.delivery import sdc_delivery_blueprint

    flask_app.register_blueprint(sdc_delivery_blueprint)

    from r6.wearables.routes import wearables_blueprint

    flask_app.register_blueprint(wearables_blueprint)

    from r6.shc.routes import shc_blueprint

    flask_app.register_blueprint(shc_blueprint)

    from r6.email_inbound import email_blueprint

    flask_app.register_blueprint(email_blueprint)

    if _is_true(flask_app.config.get("DISABLE_COMMAND_CENTER")):
        logger.info("Command Center disabled via DISABLE_COMMAND_CENTER")
    else:
        from r6.command_center.routes import command_center_blueprint

        flask_app.register_blueprint(command_center_blueprint)

    from app import web_blueprint

    # Preserve the historical endpoint names used by templates while the
    # route declarations themselves now live on a reusable Blueprint.
    flask_app.register_blueprint(web_blueprint, name="")


def _register_request_hooks(flask_app: Flask) -> None:
    @flask_app.context_processor
    def inject_fasten_public_key():
        return {"fasten_public_key": os.environ.get("FASTEN_PUBLIC_KEY", "")}

    @flask_app.context_processor
    def inject_health_context():
        from r6.health_context import load_health_context

        return {"health_context": load_health_context()}

    @flask_app.before_request
    def attach_request_id():
        g.request_id = flask_request.headers.get(
            "X-Request-Id", str(uuid.uuid4())[:8]
        )
        g.request_start = time.time()

    @flask_app.after_request
    def log_request(response):
        if flask_request.path.startswith("/static"):
            return response
        duration_ms = round(
            (time.time() - getattr(g, "request_start", time.time())) * 1000, 1
        )
        request_logger.info(
            json.dumps(
                {
                    "request_id": getattr(g, "request_id", "-"),
                    "method": flask_request.method,
                    "path": flask_request.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                    "tenant_id": flask_request.headers.get("X-Tenant-Id", "-"),
                    "agent_id": flask_request.headers.get("X-Agent-Id", "-"),
                }
            )
        )
        response.headers["X-Request-Id"] = getattr(g, "request_id", "-")
        return response


def create_app(settings: Mapping[str, Any] | None = None) -> Flask:
    """Create and configure an independent Flask application instance."""
    supplied_settings = dict(settings or {})
    app_env = validate_runtime_environment()
    _configure_logging(app_env)

    flask_app = Flask(
        __name__,
        template_folder=str(_ROOT_DIR / "templates"),
        static_folder=str(_ROOT_DIR / "static"),
    )
    flask_app.config.from_mapping(
        APP_ENV=app_env,
        SECRET_KEY=os.environ.get("SESSION_SECRET")
        or "a-development-secret-key",
        SQLALCHEMY_DATABASE_URI=_database_uri(app_env, supplied_settings),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        # Large FHIR history Bundles and base64 PDF attachments can exceed
        # 5 MB; retain bounded headroom without accepting huge request bodies.
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        DISABLE_COMMAND_CENTER=os.environ.get("DISABLE_COMMAND_CENTER", ""),
        VERCEL=os.environ.get("VERCEL", ""),
        SEED_DEMO_TENANT=os.environ.get("SEED_DEMO_TENANT", ""),
        DEMO_TENANT_ID=os.environ.get("DEMO_TENANT_ID", "desktop-demo"),
    )
    flask_app.config.update(supplied_settings)

    if app_env == "production":
        flask_app.config.update(
            SESSION_COOKIE_SECURE=True,
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
        )

    db_uri = flask_app.config["SQLALCHEMY_DATABASE_URI"]
    if (
        ("postgresql" in db_uri or "postgres" in db_uri)
        and "SQLALCHEMY_ENGINE_OPTIONS" not in supplied_settings
    ):
        flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_size": int(os.environ.get("DB_POOL_SIZE", "10")),
            "pool_recycle": 3600,
            "pool_pre_ping": True,
        }

    db.init_app(flask_app)
    register_model_metadata()
    _register_blueprints(flask_app)
    _register_request_hooks(flask_app)
    _register_lifecycle_cli(flask_app)

    upstream_url = os.environ.get("FHIR_UPSTREAM_URL", "").strip()
    if upstream_url:
        logger.info("Upstream FHIR proxy enabled: %s", upstream_url)
        logger.info(
            "Guardrails (redaction, audit, step-up, tenant isolation) apply "
            "to upstream data"
        )
    else:
        logger.info(
            "Running in local mode (SQLite JSON blobs). Set FHIR_UPSTREAM_URL "
            "for upstream proxy."
        )

    return flask_app


# Thin WSGI compatibility layer for gunicorn ``main:app`` and Vercel.
app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        # Overridable so the e2e suite can run on machines where :5000 is
        # taken (macOS AirPlay Receiver binds it by default).
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
