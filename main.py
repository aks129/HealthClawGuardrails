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
from werkzeug.middleware.proxy_fix import ProxyFix
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
    import r6.agent_runs.models  # noqa: F401
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

    @flask_app.cli.command("seed-demo-history")
    @click.option("--tenant-id", default="desktop-demo",
                  help="Tenant to load the history into.")
    def seed_demo_history_command(tenant_id: str) -> None:
        """Load the multi-year synthetic BP history into a demo tenant.

        Separate from `seed-demo`, which loads the small built-in set that
        runs before every deploy. This one is bulkier and is loaded on
        demand: it exists so a demo, a screenshot or an acceptance test has
        data with a shape worth looking at.

        Idempotent — every resource carries a fixed id, so a second run
        reports 0.
        """
        from r6.seed import seed_demo_data
        from r6.smbp.demo_history import smbp_history_resources
        with flask_app.app_context():
            count = seed_demo_data(tenant_id,
                                   resources=smbp_history_resources())
        click.echo(f"Seeded {count} resource(s) into {tenant_id}.")

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

    from r6.agent_runs.routes import agent_runs_blueprint

    flask_app.register_blueprint(agent_runs_blueprint)

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
    # Access-kernel slice 2 (docs/2026-08-03-access-kernel-spec.md §2.4).
    # HANDLERS ONLY — no before_request hook is added to any blueprint, because
    # r6/sdc/delivery.py runs off r6_blueprint on purpose (the HMAC signature in
    # the URL is the credential, and the route must work with no headers).
    # Behaviourally inert today: nothing raises StepUpDenied or calls audit()
    # yet, since the kernel is still adopted by no production module.
    # install_audit_assertions is registered here (#321), before any audit()
    # adoption rather than with it — a guard that arrives alongside the
    # migration it guards protected nobody. Testing-mode only: it fails a
    # request that flushed an AuditEvent and returned without resolving the
    # transaction. It does NOT catch a flush that is rolled back behind a 2xx;
    # docs/2026-08-03-audit-assertion-ruling.md lists that gap and six others.
    # install_read_audit_assertion stays unregistered: it goes red on the five
    # unaudited-404 paths (S-9), and that redness is its own slice (12x).
    from r6.access import install_audit_assertions, register_error_handlers

    register_error_handlers(flask_app)
    install_audit_assertions(flask_app)

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
        # This is a debug access log, not an audit trail — AuditEvent is that,
        # and it is durable and PHI-free. Logged at INFO for every request it
        # made retention a line budget: the idle run-claim poll alone was
        # ~100% of the engine's volume and evicted the reaper warnings and
        # ingest errors an operator needs. Successes go to DEBUG; anything
        # that did not succeed stays at INFO, correlation id and all.
        level = (
            logging.DEBUG if 200 <= response.status_code < 300
            else logging.INFO
        )
        request_logger.log(
            level,
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

    # Every URL this app publishes is built from ``request.host_url``: the
    # SMART configuration, the OAuth discovery document, the
    # CapabilityStatement, ``Bundle.entry.fullUrl`` and every search self
    # link. The hosting platform terminates TLS and forwards to the container
    # over plain HTTP, so without this Flask sees scheme "http" and we
    # advertise http:// endpoints on a deployment reachable only over https
    # (#567). One middleware fixes every call site at once, including ones
    # nobody has written yet — the alternative, a helper applied per site,
    # is only ever as complete as the last person to remember it.
    #
    # Trusting X-Forwarded-* is a proxy-trust decision. It is safe here
    # because the platform's edge is the only route to the container (the
    # container publishes no port of its own), because each header is
    # trusted for exactly one hop so the value the edge appends is the one
    # that wins over anything a client sends, and because nothing in this
    # app keys a security decision on the request scheme —
    # SESSION_COOKIE_SECURE above is static config, not derived from it. A
    # deployment that ever exposes this container directly must drop this.
    #
    # x_for stays 0 deliberately: r6/rate_limit.py does its own hop-counted
    # X-Forwarded-For parse, and letting ProxyFix rewrite REMOTE_ADDR would
    # give the limiter a second, differently-trusted answer for the same
    # question.
    flask_app.wsgi_app = ProxyFix(
        flask_app.wsgi_app, x_for=0, x_proto=1, x_host=1, x_port=0, x_prefix=0
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
