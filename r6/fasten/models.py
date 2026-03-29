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

    org_connection_id = db.Column(db.String(64), primary_key=True)
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


class FastenJob(db.Model):
    """
    Tracks a single EHI bulk export ingestion job.

    Lifecycle: pending → downloading → ingesting → complete | failed
    """
    __tablename__ = 'fasten_jobs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    task_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    org_connection_id = db.Column(db.String(64), nullable=False, index=True)
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    # Job lifecycle status
    status = db.Column(db.String(32), default='pending')
    # Ingestion counters (updated every 50 resources during download)
    ingested_resources = db.Column(db.Integer, default=0)
    skipped_resources = db.Column(db.Integer, default=0)
    failed_resources = db.Column(db.Integer, default=0)
    # Failure details (category only — never log raw Fasten failure_reason as it may contain PII)
    failure_reason = db.Column(db.String(256), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = db.Column(db.DateTime, nullable=True)
