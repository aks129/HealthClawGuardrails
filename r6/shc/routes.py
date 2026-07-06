"""
SmartHealthConnect Bridge Blueprint.

Receives FHIR R4 bundles pushed by SmartHealthConnect after a successful
Flexpa, Health Skillz (Epic), or generic SMART on FHIR pull.  Applies the
full HealthClaw guardrail stack (redaction, audit, tenant isolation) and
emits a Telegram notification.

Routes (prefix: /shc):
  POST /ingest      Accept a FHIR transaction bundle from SHC
  GET  /health      Liveness probe for SHC to verify HealthClaw is reachable

Authentication:
  Bearer token in Authorization header, matched against SHC_WEBHOOK_SECRET
  env var.  SHC must set the same secret as HEALTHCLAW_WEBHOOK_SECRET.

SmartHealthConnect side (TypeScript / data-connections-routes.ts):

  After a successful Flexpa or Health Skillz pull, POST the FHIR bundle:

    await fetch(`${process.env.HEALTHCLAW_WEBHOOK_URL}/shc/ingest`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${process.env.HEALTHCLAW_WEBHOOK_SECRET}`,
        'X-Tenant-Id': tenantId,
        'X-Source': 'flexpa' | 'healthskillz' | 'smart',
      },
      body: JSON.stringify({ resourceType: 'Bundle', type: 'transaction', entry: [...] }),
    });

Environment variables:
  SHC_WEBHOOK_SECRET    Shared secret — must match HEALTHCLAW_WEBHOOK_SECRET in SHC
  SHC_BASE_URL          Where SmartHealthConnect is deployed (for Telegram links)
"""

import hmac
import logging
import os
import threading
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app
from markupsafe import escape

from r6.audit import record_audit_event

logger = logging.getLogger(__name__)

shc_blueprint = Blueprint('shc', __name__, url_prefix='/shc')

# In-memory pending code store: state -> {code, received_at}
# Short-lived — the Mac mini polls within CALLBACK_TIMEOUT seconds.
_pending_codes: dict = {}

_CURATR_ELIGIBLE = frozenset({
    'Condition', 'AllergyIntolerance', 'MedicationRequest',
    'Immunization', 'Procedure', 'DiagnosticReport',
})


# ── Auth ──────────────────────────────────────────────────────────────────────

def _verify_secret() -> bool:
    expected = os.environ.get('SHC_WEBHOOK_SECRET', '').strip()
    if not expected:
        logger.warning('SHC_WEBHOOK_SECRET not set — rejecting all /shc/ingest requests')
        return False
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return False
    token = auth[len('Bearer '):]
    return hmac.compare_digest(token.encode(), expected.encode())


# ── Routes ────────────────────────────────────────────────────────────────────

@shc_blueprint.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'healthclaw-shc-bridge'}), 200


# ── MEDENT OAuth callback broker ──────────────────────────────────────────────
# MEDENT validates redirect_uris must be publicly reachable.
# The Mac mini runs a local server but MEDENT can't reach it.
# Solution: Railway acts as the callback broker.
#   1. Mac mini starts authorize flow with redirect_uri=https://app.healthclaw.io/medent/callback
#   2. After patient portal login, MEDENT redirects browser to this endpoint
#   3. This endpoint stores code keyed by state
#   4. Mac mini polls GET /medent/code?state=<state> to pick up the code
#   5. Mac mini exchanges code for tokens locally

@shc_blueprint.route('/medent/callback', methods=['GET'])
def medent_callback():
    """OAuth callback broker — captures MEDENT authorization code for Mac mini to pick up."""
    code = request.args.get('code', '')
    state = request.args.get('state', '')
    error = request.args.get('error', '')

    if error:
        logger.warning('MEDENT callback error: %s', error)
        return f"""
        <html><body style="font-family:sans-serif;max-width:500px;margin:60px auto;color:#d1d5db;background:#0d1117">
        <h2 style="color:#f87171">Authorization error: {escape(error)}</h2>
        <p>You can close this tab.</p>
        </body></html>
        """, 400

    if not code or not state:
        return 'Missing code or state', 400

    _pending_codes[state] = {
        'code': code,
        'received_at': datetime.now(timezone.utc).isoformat(),
    }
    logger.info('MEDENT callback: stored code for state=%s...', state[:8])

    return """
    <html><body style="font-family:sans-serif;max-width:500px;margin:60px auto;color:#d1d5db;background:#0d1117">
    <h2 style="color:#34d399">Authorization successful!</h2>
    <p>You can close this tab and return to Telegram.<br>
    Your records will start pulling in a moment.</p>
    </body></html>
    """, 200


@shc_blueprint.route('/medent/code', methods=['GET'])
def medent_poll_code():
    """Mac mini polls this to pick up the authorization code after browser redirect."""
    state = request.args.get('state', '')
    if not state:
        return jsonify({'error': 'state required'}), 400
    entry = _pending_codes.pop(state, None)
    if not entry:
        return jsonify({'pending': True}), 202
    return jsonify({'code': entry['code'], 'state': state}), 200


# ── Health Bank One OAuth callback broker ─────────────────────────────────────
# Same pattern as MEDENT broker above — Railway captures the code so any
# browser (phone, laptop, VPS) can complete the flow without localhost:8742.
#   1. Script builds auth URL with redirect_uri=https://app.healthclaw.io/shc/hbo/callback
#   2. User opens URL, approves in HBO app
#   3. Browser redirects here — code stored keyed by state
#   4. Script polls GET /shc/hbo/code?state=<state> to pick up the code
#   5. Script exchanges code for tokens locally

@shc_blueprint.route('/hbo/callback', methods=['GET'])
def hbo_callback():
    """OAuth callback broker — captures HBO authorization code."""
    code = request.args.get('code', '')
    state = request.args.get('state', '')
    error = request.args.get('error', '')

    if error:
        logger.warning('HBO callback error: %s', error)
        return f"""
        <html><body style="font-family:sans-serif;max-width:500px;margin:60px auto;color:#d1d5db;background:#0d1117">
        <h2 style="color:#f87171">Authorization error: {escape(error)}</h2>
        <p>You can close this tab.</p>
        </body></html>
        """, 400

    if not code or not state:
        return 'Missing code or state', 400

    _pending_codes[f'hbo:{state}'] = {
        'code': code,
        'received_at': datetime.now(timezone.utc).isoformat(),
    }
    logger.info('HBO callback: stored code for state=%s...', state[:8])

    return """
    <html><body style="font-family:sans-serif;max-width:500px;margin:60px auto;color:#d1d5db;background:#0d1117">
    <h2 style="color:#34d399">Health Bank One connected!</h2>
    <p>You can close this tab and return to Telegram.<br>
    Your records will start pulling in a moment.</p>
    </body></html>
    """, 200


@shc_blueprint.route('/hbo/code', methods=['GET'])
def hbo_poll_code():
    """Script polls this to pick up the HBO authorization code after browser redirect."""
    state = request.args.get('state', '')
    if not state:
        return jsonify({'error': 'state required'}), 400
    entry = _pending_codes.pop(f'hbo:{state}', None)
    if not entry:
        return jsonify({'pending': True}), 202
    return jsonify({'code': entry['code'], 'state': state}), 200


@shc_blueprint.route('/ingest', methods=['POST'])
def ingest():
    """Accept a FHIR R4 transaction Bundle from SmartHealthConnect."""
    if not _verify_secret():
        return jsonify({'error': 'Unauthorized'}), 401

    tenant_id = request.headers.get('X-Tenant-Id', '').strip()
    if not tenant_id:
        return jsonify({'error': 'X-Tenant-Id header required'}), 400

    source = request.headers.get('X-Source', 'shc').strip()

    try:
        bundle = request.get_json(force=True, silent=True)
    except Exception:
        bundle = None
    if not bundle:
        return jsonify({'error': 'Invalid JSON body'}), 400

    if bundle.get('resourceType') != 'Bundle':
        return jsonify({'error': 'Expected a FHIR Bundle'}), 400

    entries = [e.get('resource', e) for e in bundle.get('entry', [])]
    if not entries:
        return jsonify({'received': True, 'ingested': 0, 'message': 'empty bundle'}), 200

    job_id = str(uuid.uuid4())[:8]
    logger.info('SHC ingest: source=%s tenant=%s entries=%d job=%s',
                source, tenant_id, len(entries), job_id)

    app = current_app._get_current_object()
    t = threading.Thread(
        target=_ingest_bundle,
        args=(app, entries, tenant_id, source, job_id),
        daemon=True,
    )
    t.start()

    return jsonify({'received': True, 'job_id': job_id, 'entries': len(entries)}), 200


# ── Background ingest ─────────────────────────────────────────────────────────

def _ingest_bundle(app, entries: list, tenant_id: str, source: str, job_id: str) -> None:
    from r6.fasten.ingester import _ingest_one  # reuse existing ingest logic

    with app.app_context():
        ingested = skipped = failed = 0
        curatr_eligible_ids: list[tuple[str, str]] = []

        for resource in entries:
            try:
                result, rid = _ingest_one(resource, tenant_id)
                if result == 'ok':
                    ingested += 1
                    rt = resource.get('resourceType', '')
                    if rt in _CURATR_ELIGIBLE and rid:
                        curatr_eligible_ids.append((rt, rid))
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                logger.warning('SHC ingest error (job=%s): %s', job_id, exc)

        record_audit_event(
            event_type='shc_import_complete',
            agent_id=f'shc-{source}',
            tenant_id=tenant_id,
            outcome='success',
            detail=(
                f'job={job_id} source={source} '
                f'ingested={ingested} skipped={skipped} failed={failed}'
            ),
        )
        logger.info(
            'SHC job %s complete: source=%s ingested=%d skipped=%d failed=%d',
            job_id, source, ingested, skipped, failed,
        )

        curatr_issues = 0
        if os.environ.get('FASTEN_CURATR_SCAN', '').lower() == 'true':
            try:
                from r6.fasten.ingester import _run_curatr_scan
                curatr_issues = _run_curatr_scan(curatr_eligible_ids, tenant_id, job_id) or 0
            except Exception as exc:
                logger.warning('SHC curatr scan failed: %s', exc)

        try:
            from r6.telegram_push import notify_tenant
            source_label = {
                'flexpa': 'Flexpa (insurance/payer)',
                'healthskillz': 'Health Skillz (Epic/patient portal)',
                'smart': 'SMART on FHIR',
            }.get(source, source)
            msg_lines = [
                f'📥 *Records imported* — {source_label}',
                f'• {ingested} resources ingested',
            ]
            if skipped:
                msg_lines.append(f'• {skipped} skipped')
            if failed:
                msg_lines.append(f'• {failed} failed')
            if curatr_issues:
                msg_lines.append(f'• {curatr_issues} data-quality issues flagged')
            msg_lines += ['', 'Try `/summary`, `/conditions`, or `/dashboard`.']
            notify_tenant(tenant_id, '\n'.join(msg_lines))
        except Exception as exc:
            logger.warning('SHC notify push failed: %s', exc)
