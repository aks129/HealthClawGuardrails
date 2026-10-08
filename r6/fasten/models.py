"""
Fasten Connect database models.

FastenConnection — maps a patient's org_connection_id to a tenant.
FastenJob       — tracks the lifecycle of an EHI bulk export ingestion.
"""
from datetime import datetime, timezone

from models import db


class FastenConnection(db.Model):
    """
    Maps a Fasten org_connection_id to a tenant_id.

    Created when the patient completes the Stitch widget flow
    (POST /fasten/connections from the frontend callback).
    """
    __tablename__ = 'fasten_connections'

    # 255, not 64: Fasten mints this id — we don't control its length, and
    # SQLite masks a too-narrow varchar until Postgres truncation-errors
    # (bitten 3x; see tests/test_fasten_models_widths.py). Widening a
    # varchar PK on Postgres is a plain online type widen; schema_sync
    # ALTERs it on existing deployments.
    org_connection_id = db.Column(db.String(255), primary_key=True)
    # tenant_id is internally generated — 64 is intentional.
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    # EHR portal identifiers (absent in TEFCA mode)
    endpoint_id = db.Column(db.String(128), nullable=True)
    brand_id = db.Column(db.String(128), nullable=True)
    portal_id = db.Column(db.String(128), nullable=True)
    # TEFCA IAS identifier (use instead of endpoint_id/brand_id in TEFCA mode)
    tefca_directory_id = db.Column(db.String(128), nullable=True)
    platform_type = db.Column(db.String(64), nullable=True)
    connection_status = db.Column(db.String(32), default='authorized')  # authorized | revoked
    consent_expires_at = db.Column(db.DateTime, nullable=True)
    connected_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_export_at = db.Column(db.DateTime, nullable=True)
    # Set ONLY by the HMAC-verified patient.connection_success webhook — the
    # proof this org_connection_id is real, not fabricated by the registrant.
    webhook_verified_at = db.Column(db.DateTime, nullable=True)
    # Only the SHA-256 digest is persisted; the raw short-lived proof remains
    # inside the browser's signed HttpOnly session cookie.
    enrollment_proof_hash = db.Column(db.String(64), nullable=True)
    enrollment_expires_at = db.Column(db.DateTime, nullable=True)
    # Mint-once marker for the patient connect (agent read) token.
    agent_token_issued_at = db.Column(db.DateTime, nullable=True)


class FastenJob(db.Model):
    """
    Tracks a single EHI bulk export ingestion job.

    Lifecycle: pending → downloading → ingesting → complete | failed
    """
    __tablename__ = 'fasten_jobs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # task_id/org_connection_id are minted by Fasten (external ids) — 255,
    # not 64, for the same SQLite-masks-varchar reason as FastenConnection.
    task_id = db.Column(db.String(255), unique=True, nullable=False, index=True)
    org_connection_id = db.Column(db.String(255), nullable=False, index=True)
    # tenant_id is internally generated — 64 is intentional.
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    # Job lifecycle status
    status = db.Column(db.String(32), default='pending')
    # Ingestion counters (updated every 50 resources during download)
    ingested_resources = db.Column(db.Integer, default=0)
    skipped_resources = db.Column(db.Integer, default=0)
    failed_resources = db.Column(db.Integer, default=0)
    # Failure details (category only — never log raw Fasten failure_reason as it may contain PII)
    failure_reason = db.Column(db.String(256), nullable=True)
    # Signed download URLs (JSON array) — persisted so a job stranded by a
    # redeploy/crash mid-ingest can be re-run without the original webhook.
    download_links_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = db.Column(db.DateTime, nullable=True)


class TenantClosure(db.Model):
    """A tenant whose account holder disconnected or deleted its records.

    Tenant-wide, and no new records may arrive in a closed tenant. The
    Fasten paths (webhooks, retry, the boot reaper, a running ingest, the
    connect page) and the wearables poller ask `tenant_closed`; every
    record-writing route asks it through r6.access.require_open_tenant.
    Reads, audit writes and purge stay open.

    It lives with the Fasten models because Fasten is why it exists: a
    disconnect that lands before patient.connection_success has no
    FastenConnection row to flip, and the webhook would then create a
    fresh authorized one.
    """
    __tablename__ = 'tenant_closures'

    # tenant_id is internally generated — 64, like FastenConnection.tenant_id.
    tenant_id = db.Column(db.String(64), primary_key=True)
    revoked_at = db.Column(db.DateTime, nullable=False,
                           default=lambda: datetime.now(timezone.utc))


def tenant_closed(tenant_id) -> bool:
    """True when the account holder disconnected or deleted this tenant."""
    if not tenant_id:
        return False
    return db.session.get(TenantClosure, tenant_id) is not None


def connection_revoked(org_connection_id) -> bool:
    """True when Fasten reported this one connection's authorization revoked.

    Read as a column, not through a loaded row, so a long-running caller
    (the ingest thread) sees a revocation committed after it started.
    """
    if not org_connection_id:
        return False
    status = (db.session.query(FastenConnection.connection_status)
              .filter_by(org_connection_id=org_connection_id).scalar())
    return status == 'revoked'
