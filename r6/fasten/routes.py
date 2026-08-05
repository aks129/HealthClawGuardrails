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
from r6.access import TenantRejected, TenantSource, tenant_from_request
from r6.audit import add_audit_event, record_audit_event
from r6.fasten.enrollment import (
    clear_enrollment_session,
    enrollment_proof_hash,
    enrollment_tenant,
    establish_enrollment,
)
from r6.fasten.models import FastenConnection, FastenJob
from r6.fasten.verify import verify_webhook
from r6.fasten.ingester import stream_ingest
from r6.read_auth import authorize_tenant_read
from r6.stepup import generate_step_up_token, READ_TOKEN_TTL_SECONDS
from r6.fasten.api import trigger_ehi_export

logger = logging.getLogger(__name__)

fasten_blueprint = Blueprint('fasten', __name__, url_prefix='/fasten')


def _tenant_header_or_error():
    """Resolve X-Tenant-Id through the access kernel (spec §1.1, slice 9).

    Returns ``(tenant_id, None)`` or ``(None, response)``. Used by the two
    write routes whose only guard is the tenant header, so the id it returns
    is the partition key an EHR connection or an ingest job is bound to.

    The absent-header answer is the 400 these routes always gave. A MALFORMED
    id raises out to the app-wide TenantRejected handler — before this slice
    both routes accepted any string, so no caller can depend on the shape of a
    refusal that never happened. `_tenant_for_read` keeps its own dialect;
    see there.
    """
    try:
        return tenant_from_request(sources=(TenantSource.HEADER,)).id, None
    except TenantRejected as exc:
        if exc.reason == TenantRejected.MALFORMED:
            raise
        return None, (jsonify({'error': 'X-Tenant-Id header required'}), 400)


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

    # Fasten's live envelope nests the event fields under `data`:
    # {api_mode, type, date, id, data: {org_connection_id, external_id, ...}}
    # (verified from a live delivery 2026-07-07). Handlers receive the inner
    # object; a flat payload (fields at top level) passes through unchanged.
    event = payload.get('data') if isinstance(payload.get('data'), dict) \
        else payload

    if event_type == 'patient.ehi_export_success':
        _handle_export_success(event)

    elif event_type == 'patient.ehi_export_failed':
        _handle_export_failed(event)

    elif event_type == 'patient.authorization_revoked':
        _handle_revoked(event)

    elif event_type == 'patient.connection_success':
        # Optional event (disabled by default in Fasten).
        # If enabled, can auto-register connections server-side.
        _handle_connection_success(event)

    # webhook.test, patient.request_health_system, patient.request_support: accept silently
    return jsonify({'received': True}), 200


def _handle_export_success(payload: dict) -> None:
    """Handle patient.ehi_export_success — kick off streaming download."""
    task_id = payload.get('task_id', '')
    org_connection_id = payload.get('org_connection_id', '')
    # Live payloads carry download_links as [{content_type, export_type, url}]
    # (2026-07-08 delivery); older/flat shapes as plain URL strings. Normalize
    # to URL strings — the ingester streams from URLs.
    raw_links = payload.get('download_links') or []
    if not raw_links and payload.get('download_link'):
        raw_links = [payload['download_link']]
    download_links = [
        (d.get('url') if isinstance(d, dict) else d)
        for d in raw_links
        if (d.get('url') if isinstance(d, dict) else d)
    ]

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

    # Idempotency with recovery: skip only a COMPLETED job. A job stranded in
    # a non-terminal state (a redeploy/crash killed the daemon thread) or a
    # failed one is reset and re-run — otherwise the records are stuck forever.
    existing = FastenJob.query.filter_by(task_id=task_id).first()
    if existing:
        if existing.status == 'complete':
            logger.info('Fasten: job %s complete — skipping (idempotent)', task_id)
            return
        logger.info('Fasten: job %s in state %s — reprocessing',
                    task_id, existing.status)
        job = existing
        job.ingested_resources = 0
        job.skipped_resources = 0
        job.failed_resources = 0
        job.failure_reason = None
    else:
        job = FastenJob(
            task_id=task_id,
            org_connection_id=org_connection_id,
            tenant_id=conn.tenant_id,
        )
        db.session.add(job)
    job.status = 'pending'
    job.download_links_json = json.dumps(download_links)
    conn.last_export_at = datetime.now(timezone.utc)
    db.session.commit()

    record_audit_event(
        event_type='fasten_import_start',
        agent_id='fasten-connect',
        tenant_id=conn.tenant_id,
        outcome='success',
        detail=f'job={task_id} links={len(download_links)}',
    )

    _launch_ingest(job.id, download_links, conn.tenant_id, task_id)


def _launch_ingest(job_id, download_links, tenant_id, task_id):
    """Start the background ingest thread (webhook must return 200 quickly)."""
    app = current_app._get_current_object()
    t = threading.Thread(
        target=stream_ingest,
        args=(app, job_id, download_links, tenant_id),
        daemon=True,
        name=f'fasten-ingest-{task_id[:8]}',
    )
    t.start()


@fasten_blueprint.route('/jobs/<task_id>/retry', methods=['POST'])
def retry_job(task_id):
    """Re-run a stranded ingest job from its persisted download links.

    Recovery hatch for jobs stuck by a redeploy/crash mid-ingest, or failed
    ones. Tenant-scoped; refuses completed jobs (409) and jobs without stored
    links (409, cannot recover without the signed URLs).
    """
    tenant_id, tenant_error = _tenant_header_or_error()
    if tenant_error is not None:
        return tenant_error
    job = FastenJob.query.filter_by(task_id=task_id, tenant_id=tenant_id).first()
    if not job:
        return jsonify({'error': 'not found'}), 404
    if job.status == 'complete':
        return jsonify({'error': 'job already complete'}), 409
    if not job.download_links_json:
        return jsonify({'error': 'no stored download links — re-trigger the '
                                 'export instead'}), 409
    links = json.loads(job.download_links_json)
    job.status = 'pending'
    job.ingested_resources = 0
    job.skipped_resources = 0
    job.failed_resources = 0
    job.failure_reason = None
    db.session.commit()
    _launch_ingest(job.id, links, tenant_id, task_id)
    return jsonify({'status': 'retrying', 'task_id': task_id}), 202


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
    Handle patient.connection_success (optional event, must be enabled in Fasten portal).
    external_id is the value passed as &external-id= in the Stitch widget URL (our tenant_id).
    """
    org_connection_id = payload.get('org_connection_id', '')
    # Fasten echoes back the external-id widget param as external_id — that's our tenant_id
    tenant_id = payload.get('external_id', '') or payload.get('tenant_id', '')
    if not org_connection_id or not tenant_id:
        logger.warning(
            'Fasten connection_success: missing org_connection_id or external_id — '
            'enable patient.connection_success in the Fasten developer portal and '
            'pass external-id=<tenant> in the widget URL'
        )
        return

    existing = FastenConnection.query.filter_by(
        org_connection_id=org_connection_id).first()
    if existing:
        # The signature check already passed upstream — this is the proof the
        # org_connection_id is real. Stamp it for the agent-access mint gate.
        if existing.webhook_verified_at is None:
            existing.webhook_verified_at = datetime.now(timezone.utc)
            db.session.commit()
        # Fasten does NOT export automatically — request it (idempotent).
        trigger_ehi_export(org_connection_id)
        return

    conn = FastenConnection(
        org_connection_id=org_connection_id,
        tenant_id=tenant_id,
        webhook_verified_at=datetime.now(timezone.utc),
        endpoint_id=payload.get('endpoint_id'),
        brand_id=payload.get('brand_id'),
        portal_id=payload.get('portal_id'),
        tefca_directory_id=payload.get('tefca_directory_id'),
        platform_type=payload.get('platform_type'),
        connection_status=payload.get('connection_status', 'authorized'),
    )
    db.session.add(conn)
    db.session.commit()

    # Fasten does NOT export automatically — request it (idempotent).
    trigger_ehi_export(org_connection_id)


# ---------------------------------------------------------------------------
# Connection registry
# ---------------------------------------------------------------------------

def _tenant_for_read():
    """Resolve a tenant claim only after the shared read-auth gate.

    Kernel-backed since slice 9, but its refusals are unchanged: a malformed
    id was already refused here, because authorize_tenant_read validates the
    pattern before it consults any credential, and it answers 401 rather than
    400. Turning that into a 400 would be a status-dialect change, which is
    a separate deliberate PR — so the reason is mapped back onto the answer
    this helper already gave.
    """
    try:
        candidate = tenant_from_request(sources=(TenantSource.HEADER,)).id
    except TenantRejected as exc:
        if exc.reason == TenantRejected.ABSENT:
            return None, (jsonify({'error': 'X-Tenant-Id header required'}), 400)
        return None, (jsonify({
            'error': 'authentication required for this tenant',
        }), 401)
    tenant_id = authorize_tenant_read(candidate)
    if tenant_id is None:
        return None, (jsonify({
            'error': 'authentication required for this tenant',
        }), 401)
    return tenant_id, None


@fasten_blueprint.route('/connections', methods=['GET'])
def list_connections():
    """List EHR connections for the requesting tenant (max 50, newest first)."""
    tenant_id, auth_error = _tenant_for_read()
    if auth_error is not None:
        return auth_error

    conns = (
        FastenConnection.query
        .filter_by(tenant_id=tenant_id)
        .order_by(FastenConnection.connected_at.desc())
        .limit(50)
        .all()
    )
    return jsonify({
        'connections': [{
            'org_connection_id': c.org_connection_id,
            'connection_status': c.connection_status,
            'platform_type': c.platform_type,
            'tefca_mode': c.tefca_directory_id is not None,
            'connected_at': c.connected_at.isoformat() if c.connected_at else None,
            'last_export_at': c.last_export_at.isoformat() if c.last_export_at else None,
        } for c in conns],
        'count': len(conns),
    }), 200


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
    tenant_id, tenant_error = _tenant_header_or_error()
    if tenant_error is not None:
        return tenant_error

    data = request.get_json(silent=True) or {}
    org_connection_id = data.get('org_connection_id', '').strip()
    if not org_connection_id:
        return jsonify({'error': 'org_connection_id required'}), 400

    existing = FastenConnection.query.filter_by(
        org_connection_id=org_connection_id
    ).first()
    if existing:
        if existing.tenant_id != tenant_id:
            return jsonify({'error': 'connection belongs to another tenant'}), 409
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
    try:
        db.session.flush()
        proof_hash, proof_expires_at = establish_enrollment(
            tenant_id, org_connection_id
        )
        conn.enrollment_proof_hash = proof_hash
        conn.enrollment_expires_at = proof_expires_at
        add_audit_event(
            event_type='fasten_connection_registered',
            agent_id='fasten-connect',
            tenant_id=tenant_id,
            outcome='success',
            detail=f'platform={conn.platform_type}',
        )
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 - database drivers vary
        db.session.rollback()
        clear_enrollment_session()
        logger.error('Fasten connection registration failed: %s', type(exc).__name__)
        return jsonify({'error': 'enrollment proof unavailable'}), 503

    return jsonify({
        'status': 'registered',
        'org_connection_id': org_connection_id,
        # token is minted by GET .../agent-access AFTER the signed
        # connection_success webhook verifies this org_connection_id
        'agent_access_pending': True,
    }), 201



@fasten_blueprint.route('/connections/<org_connection_id>', methods=['GET'])
def get_connection(org_connection_id: str):
    """Get connection status. Requires X-Tenant-Id for tenant isolation."""
    tenant_id, auth_error = _tenant_for_read()
    if auth_error is not None:
        return auth_error
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
    tenant_id, auth_error = _tenant_for_read()
    if auth_error is not None:
        return auth_error

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
    tenant_id, auth_error = _tenant_for_read()
    if auth_error is not None:
        return auth_error
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


@fasten_blueprint.route('/connections/<org_connection_id>/agent-access',
                        methods=['GET'])
def agent_access(org_connection_id):
    """One-time mint of the patient connect (agent read) token.

    Issued only when ALL of:
    - the connection exists and belongs to the calling tenant,
    - the HMAC-verified patient.connection_success webhook has confirmed the
      org_connection_id (pre-claim protection: fabricated ids never verify),
    - this is the tenant's FIRST connection (no older connection rows),
    - the token has not been issued before (mint-once),
    - the tenant is not public (public reads are already open).

    Not yet verified -> 202 {pending}: the page polls briefly; the webhook
    usually lands within seconds of widget completion.
    """
    enrolled_tenant = enrollment_tenant(org_connection_id)
    claimed_header = request.headers.get('X-Tenant-Id', '').strip()
    using_enrollment = (
        enrolled_tenant is not None
        and (not claimed_header or claimed_header == enrolled_tenant)
    )
    if using_enrollment:
        tenant_id = enrolled_tenant
    else:
        tenant_id, auth_error = _tenant_for_read()
        if auth_error is not None:
            if not request.headers.get('X-Tenant-Id'):
                return jsonify({'error': 'enrollment proof required'}), 401
            return auth_error

    conn = FastenConnection.query.filter_by(
        org_connection_id=org_connection_id, tenant_id=tenant_id).first()
    if conn is None:
        return jsonify({'error': 'not found'}), 404

    from r6.command_center.access import is_public
    if is_public(tenant_id):
        return jsonify({'error': 'public tenant — no token needed'}), 409
    if conn.agent_token_issued_at is not None:
        return jsonify({'error': 'already issued'}), 410
    if conn.webhook_verified_at is None:
        return jsonify({'pending': True}), 202
    older = FastenConnection.query.filter(
        FastenConnection.tenant_id == tenant_id,
        FastenConnection.connected_at < conn.connected_at,
    ).count()
    if older:
        return jsonify({'error': 'not first connection for tenant'}), 409

    try:
        token = generate_step_up_token(
            tenant_id, agent_id='patient-connect',
            ttl_seconds=READ_TOKEN_TTL_SECONDS, scope='read')
    except ValueError:
        return jsonify({'error': 'server not configured to mint'}), 503

    issued_at = datetime.now(timezone.utc)
    claim_query = FastenConnection.query.filter_by(
        org_connection_id=org_connection_id,
        tenant_id=tenant_id,
        agent_token_issued_at=None,
    ).filter(FastenConnection.webhook_verified_at.isnot(None))
    claim_values = {
        'agent_token_issued_at': issued_at,
        'enrollment_proof_hash': None,
        'enrollment_expires_at': None,
    }
    if using_enrollment:
        proof_hash = enrollment_proof_hash(org_connection_id)
        if proof_hash is None:
            return jsonify({'error': 'enrollment proof expired or already used'}), 401
        claim_query = claim_query.filter(
            FastenConnection.enrollment_proof_hash == proof_hash,
            FastenConnection.enrollment_expires_at >= issued_at,
        )

    try:
        claimed = claim_query.update(claim_values, synchronize_session=False)
        if claimed != 1:
            db.session.rollback()
            current = FastenConnection.query.filter_by(
                org_connection_id=org_connection_id,
                tenant_id=tenant_id,
            ).first()
            if current and current.agent_token_issued_at is not None:
                return jsonify({'error': 'already issued'}), 410
            return jsonify({'error': 'enrollment proof expired or already used'}), 401
        add_audit_event(
            event_type='agent_read_token_issued',
            agent_id='fasten-connect',
            tenant_id=tenant_id,
            outcome='success',
            detail='read-scoped patient connect token (30d) issued after webhook verification',
        )
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 - database drivers vary
        db.session.rollback()
        logger.error('Fasten enrollment issuance failed: %s', type(exc).__name__)
        return jsonify({'error': 'enrollment issuance unavailable'}), 503

    if using_enrollment:
        clear_enrollment_session()
    expires = datetime.now(timezone.utc).timestamp() + READ_TOKEN_TTL_SECONDS
    return jsonify({
        'tenant_id': tenant_id,
        'read_token': token,
        'expires_at': datetime.fromtimestamp(
            expires, tz=timezone.utc).isoformat(timespec='seconds'),
        'scope': 'read',
        'instructions': (
            'Give these two values to your AI assistant: for every HealthClaw '
            'tool call include _tenantId and _stepUpToken (or send them as the '
            'X-Tenant-Id / X-Step-Up-Token headers). Treat the token like a '
            'password. It can read this record only — it can never change '
            'anything — and it expires in 30 days.'
        ),
    }), 200
