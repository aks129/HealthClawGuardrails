"""
Fasten Connect Blueprint — webhook receiver, connection registry, job status API.

Routes (prefix: /fasten):
  POST /webhook                        Fasten event receiver (Standard-Webhooks verified)
  POST /connections                    Register org_connection_id → tenant mapping
  GET  /connections/<org_connection_id> Connection status
  GET  /jobs                           List ingestion jobs for tenant
  GET  /jobs/<task_id>                 Single job status

Security:
  - Webhook endpoint verified via HMAC-SHA256 (Standard-Webhooks spec)
  - Connection + job endpoints require X-Tenant-Id header (tenant isolation)
  - Fasten webhook payloads are never logged raw (may contain PHI per Fasten docs)
"""
import json
import logging
import threading
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app

from models import db
from r6.audit import record_audit_event
from r6.fasten.models import FastenConnection, FastenJob
from r6.fasten.verify import verify_webhook
from r6.fasten.ingester import stream_ingest

logger = logging.getLogger(__name__)

fasten_blueprint = Blueprint('fasten', __name__, url_prefix='/fasten')


# ---------------------------------------------------------------------------
# Webhook receiver
# ---------------------------------------------------------------------------

@fasten_blueprint.route('/webhook', methods=['POST'])
def webhook():
    """
    Receive Fasten Connect webhook events.
    Verified via Standard-Webhooks HMAC-SHA256 signature.
    Returns 200 immediately; ingestion runs in a background thread.
    """
    raw_body = request.get_data()

    if not verify_webhook(dict(request.headers), raw_body):
        return jsonify({'error': 'Invalid signature'}), 401

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return jsonify({'error': 'Invalid JSON'}), 400

    event_type = payload.get('type', '')
    # Log only the event type — never the full payload (may contain PHI)
    logger.info('Fasten webhook received: type=%s', event_type)

    if event_type == 'patient.ehi_export_success':
        _handle_export_success(payload)

    elif event_type == 'patient.ehi_export_failed':
        _handle_export_failed(payload)

    elif event_type == 'patient.authorization_revoked':
        _handle_revoked(payload)

    elif event_type == 'patient.connection_success':
        # Optional event (disabled by default in Fasten).
        # If enabled, can auto-register connections server-side.
        _handle_connection_success(payload)

    # webhook.test, patient.request_health_system, patient.request_support: accept silently
    return jsonify({'received': True}), 200


def _handle_export_success(payload: dict) -> None:
    """Handle patient.ehi_export_success — kick off streaming download."""
    task_id = payload.get('task_id', '')
    org_connection_id = payload.get('org_connection_id', '')
    download_links = payload.get('download_links', [])

    if not task_id or not org_connection_id or not download_links:
        logger.warning('Fasten export_success: missing required fields')
        return

    conn = FastenConnection.query.filter_by(
        org_connection_id=org_connection_id
    ).first()
    if not conn:
        logger.warning(
            'Fasten export_success: unknown org_connection_id (not registered)'
        )
        return

    # Idempotency: if we already processed this task, skip
    if FastenJob.query.filter_by(task_id=task_id).first():
        logger.info('Fasten: job %s already exists — skipping (idempotent)', task_id)
        return

    job = FastenJob(
        task_id=task_id,
        org_connection_id=org_connection_id,
        tenant_id=conn.tenant_id,
    )
    db.session.add(job)
    conn.last_export_at = datetime.now(timezone.utc)
    db.session.commit()

    record_audit_event(
        event_type='fasten_import_start',
        agent_id='fasten-connect',
        tenant_id=conn.tenant_id,
        outcome='success',
        detail=f'job={task_id} links={len(download_links)}',
    )

    # Launch background download thread — webhook must return 200 quickly
    app = current_app._get_current_object()
    t = threading.Thread(
        target=stream_ingest,
        args=(app, job.id, download_links, conn.tenant_id),
        daemon=True,
        name=f'fasten-ingest-{task_id[:8]}',
    )
    t.start()


def _handle_export_failed(payload: dict) -> None:
    """Handle patient.ehi_export_failed — record failure without logging PHI."""
    task_id = payload.get('task_id', '')
    org_connection_id = payload.get('org_connection_id', '')
    # failure_reason enum value only — may still be sensitive; truncate to category
    failure_reason = str(payload.get('failure_reason', 'unknown'))[:64]

    conn = FastenConnection.query.filter_by(
        org_connection_id=org_connection_id
    ).first()
    tenant_id = conn.tenant_id if conn else 'unknown'

    job = FastenJob.query.filter_by(task_id=task_id).first()
    if job:
        job.status = 'failed'
        job.failure_reason = failure_reason
        job.completed_at = datetime.now(timezone.utc)
        db.session.commit()

    record_audit_event(
        event_type='fasten_import_failed',
        agent_id='fasten-connect',
        tenant_id=tenant_id,
        outcome='failure',
        detail=f'job={task_id}',
    )


def _handle_revoked(payload: dict) -> None:
    """Handle patient.authorization_revoked — mark connection as revoked."""
    org_connection_id = payload.get('org_connection_id', '')
    conn = FastenConnection.query.filter_by(
        org_connection_id=org_connection_id
    ).first()
    if conn:
        conn.connection_status = 'revoked'
        db.session.commit()
        record_audit_event(
            event_type='fasten_connection_revoked',
            agent_id='fasten-connect',
            tenant_id=conn.tenant_id,
            outcome='success',
        )


def _handle_connection_success(payload: dict) -> None:
    """
    Handle patient.connection_success (optional event, disabled by default).
    Auto-registers the connection if tenant_id is present in the payload.
    """
    org_connection_id = payload.get('org_connection_id', '')
    tenant_id = payload.get('tenant_id', '')  # custom field — may not be present
    if not org_connection_id or not tenant_id:
        return

    if FastenConnection.query.filter_by(org_connection_id=org_connection_id).first():
        return

    conn = FastenConnection(
        org_connection_id=org_connection_id,
        tenant_id=tenant_id,
        endpoint_id=payload.get('endpoint_id'),
        brand_id=payload.get('brand_id'),
        portal_id=payload.get('portal_id'),
        tefca_directory_id=payload.get('tefca_directory_id'),
        platform_type=payload.get('platform_type'),
        connection_status=payload.get('connection_status', 'authorized'),
    )
    db.session.add(conn)
    db.session.commit()


# ---------------------------------------------------------------------------
# Connection registry
# ---------------------------------------------------------------------------

@fasten_blueprint.route('/connections', methods=['POST'])
def register_connection():
    """
    Register an org_connection_id → tenant mapping.

    Called from your frontend immediately after the Fasten Stitch widget
    fires the widget.complete event with the org_connection_id.

    Required header: X-Tenant-Id
    Required body:   { "org_connection_id": "..." }
    Optional body:   endpoint_id, brand_id, portal_id, tefca_directory_id,
                     platform_type, connection_status, consent_expires_at
    """
    tenant_id = request.headers.get('X-Tenant-Id', '').strip()
    if not tenant_id:
        return jsonify({'error': 'X-Tenant-Id header required'}), 400

    data = request.get_json(silent=True) or {}
    org_connection_id = data.get('org_connection_id', '').strip()
    if not org_connection_id:
        return jsonify({'error': 'org_connection_id required'}), 400

    existing = FastenConnection.query.filter_by(
        org_connection_id=org_connection_id
    ).first()
    if existing:
        return jsonify({
            'status': 'already_registered',
            'org_connection_id': org_connection_id,
        }), 200

    conn = FastenConnection(
        org_connection_id=org_connection_id,
        tenant_id=tenant_id,
        endpoint_id=data.get('endpoint_id'),
        brand_id=data.get('brand_id'),
        portal_id=data.get('portal_id'),
        tefca_directory_id=data.get('tefca_directory_id'),
        platform_type=data.get('platform_type'),
        connection_status=data.get('connection_status', 'authorized'),
    )
    if data.get('consent_expires_at'):
        try:
            conn.consent_expires_at = datetime.fromisoformat(
                data['consent_expires_at'].rstrip('Z')
            )
        except ValueError:
            pass  # ignore malformed timestamps

    db.session.add(conn)
    db.session.commit()

    record_audit_event(
        event_type='fasten_connection_registered',
        agent_id='fasten-connect',
        tenant_id=tenant_id,
        outcome='success',
        detail=f'platform={conn.platform_type}',
    )

    return jsonify({
        'status': 'registered',
        'org_connection_id': org_connection_id,
    }), 201


@fasten_blueprint.route('/connections/<org_connection_id>', methods=['GET'])
def get_connection(org_connection_id: str):
    """Get connection status. Requires X-Tenant-Id for tenant isolation."""
    tenant_id = request.headers.get('X-Tenant-Id', '').strip()
    conn = FastenConnection.query.filter_by(
        org_connection_id=org_connection_id,
        tenant_id=tenant_id,
    ).first()
    if not conn:
        return jsonify({'error': 'Not found'}), 404

    return jsonify({
        'org_connection_id': conn.org_connection_id,
        'connection_status': conn.connection_status,
        'platform_type': conn.platform_type,
        'tefca_mode': conn.tefca_directory_id is not None,
        'connected_at': conn.connected_at.isoformat() if conn.connected_at else None,
        'last_export_at': conn.last_export_at.isoformat() if conn.last_export_at else None,
        'consent_expires_at': (
            conn.consent_expires_at.isoformat() if conn.consent_expires_at else None
        ),
    }), 200


# ---------------------------------------------------------------------------
# Job status
# ---------------------------------------------------------------------------

@fasten_blueprint.route('/jobs', methods=['GET'])
def list_jobs():
    """List EHI ingestion jobs for the requesting tenant (max 50, newest first)."""
    tenant_id = request.headers.get('X-Tenant-Id', '').strip()
    if not tenant_id:
        return jsonify({'error': 'X-Tenant-Id header required'}), 400

    jobs = (
        FastenJob.query
        .filter_by(tenant_id=tenant_id)
        .order_by(FastenJob.created_at.desc())
        .limit(50)
        .all()
    )

    return jsonify({'jobs': [_job_to_dict(j) for j in jobs], 'count': len(jobs)}), 200


@fasten_blueprint.route('/jobs/<task_id>', methods=['GET'])
def get_job(task_id: str):
    """Get status of a specific EHI ingestion job."""
    tenant_id = request.headers.get('X-Tenant-Id', '').strip()
    job = FastenJob.query.filter_by(
        task_id=task_id, tenant_id=tenant_id
    ).first()
    if not job:
        return jsonify({'error': 'Not found'}), 404

    return jsonify(_job_to_dict(job)), 200


def _job_to_dict(job: FastenJob) -> dict:
    return {
        'task_id': job.task_id,
        'org_connection_id': job.org_connection_id,
        'status': job.status,
        'ingested_resources': job.ingested_resources,
        'skipped_resources': job.skipped_resources,
        'failed_resources': job.failed_resources,
        'failure_reason': job.failure_reason,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'completed_at': job.completed_at.isoformat() if job.completed_at else None,
    }
