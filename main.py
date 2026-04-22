"""
FHIR R6 MCP Showcase — Flask Application Entry Point.

Initializes the Flask app, database, and R6 FHIR Blueprint.
Run with: python main.py (development) or gunicorn main:app (production)
"""

import json
import os
import logging
import time
import uuid
from flask import Flask, request as flask_request, g
from models import db

# Configure logging — structured JSON in production, human-readable in dev
log_level = os.environ.get('LOG_LEVEL', 'DEBUG' if os.environ.get('FLASK_ENV') == 'development' else 'INFO')

if os.environ.get('FLASK_ENV') == 'production' or os.environ.get('LOG_FORMAT') == 'json':
    class JSONFormatter(logging.Formatter):
        def format(self, record):
            log_entry = {
                'timestamp': self.formatTime(record),
                'level': record.levelname,
                'logger': record.name,
                'message': record.getMessage(),
            }
            if record.exc_info:
                log_entry['exception'] = self.formatException(record.exc_info)
            return json.dumps(log_entry)

    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.root.handlers = [handler]
    logging.root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
else:
    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO),
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

logger = logging.getLogger(__name__)

# Create the Flask app with explicit paths for Vercel compatibility
_root_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__,
            template_folder=os.path.join(_root_dir, 'templates'),
            static_folder=os.path.join(_root_dir, 'static'))
app.secret_key = os.environ.get("SESSION_SECRET") or "a-development-secret-key"

# Configure database — require explicit URI in production (unless VERCEL)
db_uri = os.environ.get("SQLALCHEMY_DATABASE_URI")
if not db_uri:
    if os.environ.get('VERCEL'):
        # Vercel serverless: use ephemeral SQLite in /tmp
        db_uri = "sqlite:////tmp/mcp_server.db"
    elif os.environ.get('FLASK_ENV') == 'production':
        raise RuntimeError(
            'SQLALCHEMY_DATABASE_URI environment variable is required in production. '
            'SQLite is not suitable for production use.'
        )
    else:
        db_uri = "sqlite:///mcp_server.db"

app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
logger.info("Database configured (URI not logged for security)")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Database connection pooling (PostgreSQL in production)
if 'postgresql' in db_uri or 'postgres' in db_uri:
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_size": int(os.environ.get("DB_POOL_SIZE", "10")),
        "pool_recycle": 3600,
        "pool_pre_ping": True,
    }

# Require STEP_UP_SECRET in production (auto-generate on Vercel for demo)
if os.environ.get('FLASK_ENV') == 'production' and not os.environ.get('STEP_UP_SECRET'):
    if os.environ.get('VERCEL'):
        import secrets
        os.environ['STEP_UP_SECRET'] = secrets.token_hex(32)
        logger.info("STEP_UP_SECRET auto-generated for Vercel demo deployment")
    else:
        raise RuntimeError(
            'STEP_UP_SECRET environment variable is required in production. '
            'Generate a secure random secret: python -c "import secrets; print(secrets.token_hex(32))"'
        )

# Initialize database
db.init_app(app)

with app.app_context():
    from r6.models import R6Resource, ContextEnvelope, ContextItem, AuditEventRecord
    from r6.fasten.models import FastenConnection, FastenJob
    from r6.wearables.models import WearableConnection
    from r6.command_center.models import ConversationMessage, AgentTask
    from sqlalchemy.exc import OperationalError, IntegrityError, ProgrammingError
    try:
        db.create_all()
        logger.info("Database tables created (R6 + Fasten + Wearables + Command Center)")
    except (OperationalError, IntegrityError, ProgrammingError) as e:
        # Concurrent gunicorn workers can race on first-boot table creation.
        # Seen on Postgres as IntegrityError on pg_type_typname_nsp_index;
        # on SQLite as OperationalError "already exists".
        msg = str(e).lower()
        if "already exists" in msg or "pg_type_typname" in msg or "duplicate key" in msg:
            logger.info("Database tables already exist (created by another worker)")
            db.session.rollback()
        else:
            raise

    # Auto-seed demo tenant on first boot (Railway / Docker deployments)
    if os.environ.get('SEED_DEMO_TENANT'):
        _demo_tenant = os.environ.get('DEMO_TENANT_ID', 'desktop-demo')
        _existing = R6Resource.query.filter_by(tenant_id=_demo_tenant).first()
        if _existing is None:
            from r6.seed import seed_demo_data
            _count = seed_demo_data(_demo_tenant)
            logger.info("Auto-seeded %d resources into tenant '%s'", _count, _demo_tenant)
        else:
            logger.info("Demo tenant '%s' already has data, skipping auto-seed", _demo_tenant)

# Register R6 FHIR Blueprint
from r6.routes import r6_blueprint
app.register_blueprint(r6_blueprint)
logger.info("R6 FHIR Blueprint registered at /r6/fhir")

# Register Fasten Connect Blueprint
from r6.fasten.routes import fasten_blueprint
app.register_blueprint(fasten_blueprint)
logger.info("Fasten Connect Blueprint registered at /fasten")

# Register Wearables Blueprint (opt-in via OPEN_WEARABLES_URL)
from r6.wearables.routes import wearables_blueprint
app.register_blueprint(wearables_blueprint)
logger.info("Wearables Blueprint registered at /wearables")

# Start wearables poller daemon thread if configured. Safe no-op when
# OPEN_WEARABLES_URL is unset or on serverless platforms.
if not os.environ.get('VERCEL'):
    from r6.wearables.poller import start_poller
    if start_poller(app):
        logger.info("Wearables poller started (background thread)")

# Register Command Center Blueprint — "My Health in Good Hands" dashboard
from r6.command_center.routes import command_center_blueprint
app.register_blueprint(command_center_blueprint)
logger.info("Command Center Blueprint registered at /command-center")

# Log upstream FHIR server configuration
_upstream_url = os.environ.get('FHIR_UPSTREAM_URL', '').strip()
if _upstream_url:
    logger.info(f"Upstream FHIR proxy enabled: {_upstream_url}")
    logger.info("Guardrails (redaction, audit, step-up, tenant isolation) apply to upstream data")
else:
    logger.info("Running in local mode (SQLite JSON blobs). Set FHIR_UPSTREAM_URL for upstream proxy.")

# Structured request logging with correlation IDs
request_logger = logging.getLogger('request')


@app.context_processor
def inject_fasten_public_key():
    """Expose FASTEN_PUBLIC_KEY to all templates (safe — public key only)."""
    return {'fasten_public_key': os.environ.get('FASTEN_PUBLIC_KEY', '')}


@app.context_processor
def inject_health_context():
    """Expose health context to all templates (single-sourced version, etc.)."""
    from r6.health_context import load_health_context
    return {'health_context': load_health_context()}


@app.before_request
def attach_request_id():
    g.request_id = flask_request.headers.get('X-Request-Id', str(uuid.uuid4())[:8])
    g.request_start = time.time()

@app.after_request
def log_request(response):
    if flask_request.path.startswith('/static'):
        return response
    duration_ms = round((time.time() - getattr(g, 'request_start', time.time())) * 1000, 1)
    request_logger.info(json.dumps({
        'request_id': getattr(g, 'request_id', '-'),
        'method': flask_request.method,
        'path': flask_request.path,
        'status': response.status_code,
        'duration_ms': duration_ms,
        'tenant_id': flask_request.headers.get('X-Tenant-Id', '-'),
        'agent_id': flask_request.headers.get('X-Agent-Id', '-'),
    }))
    response.headers['X-Request-Id'] = getattr(g, 'request_id', '-')
    return response

# Import web UI routes
from app import *  # noqa: F401,F403,E402

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
