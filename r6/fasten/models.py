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
