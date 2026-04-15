"""
Flask Blueprint for wearables: provider discovery, OAuth kickoff/callback,
sync status, manual sync trigger.

Mounted at /wearables. Follows the Fasten Connect blueprint pattern.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone

from flask import Blueprint, Response, current_app, jsonify, redirect, request

from models import db
from r6.audit import record_audit_event
from r6.stepup import validate_step_up_token
from r6.wearables.client import WearablesClient
from r6.wearables.models import SUPPORTED_PROVIDERS, WearableConnection
from r6.wearables.poller import run_once

logger = logging.getLogger(__name__)

wearables_blueprint = Blueprint('wearables', __name__, url_prefix='/wearables')

_STATE_TTL_SECONDS = 600  # 10 minutes


# --- state signing ---------------------------------------------------------

def _state_secret() -> bytes:
    secret = (
        os.environ.get('WEARABLES_OAUTH_STATE_SECRET')
        or os.environ.get('STEP_UP_SECRET')
        or ''
    )
    return secret.encode('utf-8')


def _sign_state(payload: dict) -> str:
    raw = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    sig = hmac.new(_state_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f'{body}.{sig}'


def _verify_state(state: str) -> dict | None:
    if not state or '.' not in state:
        return None
    body, sig = state.rsplit('.', 1)
    expected = hmac.new(
        _state_secret(), body.encode(), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        padding = '=' * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + padding))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get('exp', 0) < int(time.time()):
        return None
    return payload


# --- provider discovery ----------------------------------------------------

def _configured_providers_from_env() -> set[str]:
    """
    Infer which providers have OAuth app credentials configured in the
    sidecar's env. Operators who set {PROVIDER}_CLIENT_ID will have the
    provider appear as 'configured'.
    """
    configured = set()
    for p in SUPPORTED_PROVIDERS:
        if os.environ.get(f'{p.upper()}_CLIENT_ID'):
            configured.add(p)
    return configured


@wearables_blueprint.route('/providers', methods=['GET'])
def list_providers():
    wc = WearablesClient()
    providers_raw = wc.list_providers() if wc.enabled() else [
        {'name': n} for n in SUPPORTED_PROVIDERS
    ]
    configured = _configured_providers_from_env()
    providers = []
    for p in providers_raw:
        name = p.get('name') if isinstance(p, dict) else str(p)
        if not name:
            continue
        providers.append({
            'name': name,
            'configured': name in configured,
            'oauth_start': f'/wearables/oauth/start?provider={name}',
        })
    return jsonify({
        'enabled': wc.enabled(),
        'base_url_configured': wc.enabled(),
        'providers': providers,
    })


# --- OAuth kickoff + callback ---------------------------------------------

@wearables_blueprint.route('/oauth/start', methods=['GET'])
def oauth_start():
    provider = (request.args.get('provider') or '').lower()
    tenant_id = request.args.get('tenant_id') or ''
    if provider not in SUPPORTED_PROVIDERS:
        return jsonify({'error': f'unsupported provider: {provider}'}), 400
    if not tenant_id:
        return jsonify({'error': 'tenant_id required'}), 400

    wc = WearablesClient()
    if not wc.enabled():
        return jsonify({
            'error': 'OPEN_WEARABLES_URL not configured',
            'hint': 'Set OPEN_WEARABLES_URL to enable wearables integration',
        }), 503

    # Open Wearables wants a stable user id. We scope one per tenant so
    # the same patient can hold multiple provider connections.
    ow_user_id = f'hc-{tenant_id}'

    state = _sign_state({
        'tenant_id': tenant_id,
        'provider': provider,
        'ow_user_id': ow_user_id,
        'nonce': secrets.token_hex(8),
        'exp': int(time.time()) + _STATE_TTL_SECONDS,
    })

    base = request.host_url.rstrip('/')
    callback_url = f'{base}/wearables/oauth/callback'
    kickoff = wc.oauth_kickoff_url(
        provider,
        ow_user_id=ow_user_id,
        callback_url=callback_url,
        state=state,
    )
    return redirect(kickoff, code=302)


@wearables_blueprint.route('/oauth/callback', methods=['GET'])
def oauth_callback():
    state = request.args.get('state', '')
    payload = _verify_state(state)
    if not payload:
        return jsonify({'error': 'invalid or expired state'}), 400

    tenant_id = payload['tenant_id']
    provider = payload['provider']
    ow_user_id = payload['ow_user_id']
    patient_ref = request.args.get('patient_ref') or None

    # Upsert connection. If one already exists for this tuple, update
    # connected_at and clear status.
    conn = WearableConnection.query.filter_by(
        tenant_id=tenant_id,
        provider=provider,
        ow_user_id=ow_user_id,
    ).first()
    now = datetime.now(timezone.utc)
    if conn is None:
        conn = WearableConnection(
            tenant_id=tenant_id,
            provider=provider,
            ow_user_id=ow_user_id,
            patient_ref=patient_ref,
            connected_at=now,
            last_sync_status='never',
        )
        db.session.add(conn)
    else:
        conn.connected_at = now
        if patient_ref:
            conn.patient_ref = patient_ref
        conn.last_sync_status = 'never'
        conn.last_sync_detail = None
    try:
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        logger.error('wearable connection commit failed: %s', exc)
        return jsonify({'error': 'commit failed'}), 500

    record_audit_event(
        'create', 'WearableConnection', str(conn.id),
        agent_id='wearable-oauth',
        tenant_id=tenant_id,
        detail=f'connected {provider}',
    )

    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Wearable Connected</title>'
        '<style>body{font-family:system-ui;background:#0b0f14;color:#dbe3ef;'
        'display:flex;align-items:center;justify-content:center;'
        'min-height:100vh;margin:0}'
        '.card{background:#0f141b;border:1px solid #1c242f;border-radius:10px;'
        'padding:32px;max-width:480px;text-align:center}'
        '.ok{color:#34d399;font-size:42px;margin-bottom:12px}'
        'a{color:#22d3ee}</style></head><body><div class="card">'
        f'<div class="ok">✓</div><h2>{provider.title()} connected</h2>'
        '<p>Wearable data will start flowing through HealthClaw Guardrails '
        'on the next sync pass.</p>'
        f'<p><a href="/r6/fhir/mcp-apps/wearables/?tenant_id={tenant_id}">'
        'View connections</a></p>'
        '</div></body></html>'
    )
    return Response(html, mimetype='text/html')


# --- status + manual sync --------------------------------------------------

@wearables_blueprint.route('/sync-status', methods=['GET'])
def sync_status():
    tenant_id = request.args.get('tenant_id') or request.headers.get(
        'X-Tenant-Id',
    )
    if not tenant_id:
        return jsonify({'error': 'tenant_id required'}), 400
    conns = WearableConnection.query.filter_by(tenant_id=tenant_id).all()
    wc = WearablesClient()
    return jsonify({
        'tenant_id': tenant_id,
        'enabled': wc.enabled(),
        'connections': [c.to_dict() for c in conns],
    })


@wearables_blueprint.route('/sync-now', methods=['POST'])
def sync_now():
    tenant_id = request.headers.get('X-Tenant-Id')
    if not tenant_id:
        return jsonify({'error': 'X-Tenant-Id required'}), 400
    token = request.headers.get('X-Step-Up-Token')
    if not token:
        return jsonify({'error': 'X-Step-Up-Token required'}), 403
    valid, err = validate_step_up_token(token, tenant_id)
    if not valid:
        return jsonify({'error': f'token rejected: {err}'}), 403

    summary = run_once(current_app)
    record_audit_event(
        'update', 'WearableConnection', None,
        agent_id=request.headers.get('X-Agent-Id', 'wearable-manual-sync'),
        tenant_id=tenant_id,
        detail=(
            f"manual sync: checked={summary.get('connections_checked')} "
            f"ingested={summary.get('observations_ingested')} "
            f"errors={summary.get('errors')}"
        ),
    )
    return jsonify(summary)
