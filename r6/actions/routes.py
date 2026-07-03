"""
Action lifecycle API — propose / commit / status / provider callbacks.

Contract mirrors FHIR writes:
  propose  -> tenant header only, returns draft for human review
  commit   -> X-Step-Up-Token (validated, tuple destructured) AND
              X-Human-Confirmed: true, else 401 / 428
  callback -> shared-secret verification

Audit detail and Telegram pushes use ProposedAction.summary() ONLY (no PHI).
"""

import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from models import db
from r6.actions.executors import execute_action
from r6.actions.models import ProposedAction, VALID_KINDS
from r6.audit import record_audit_event
from r6.rate_limit import rate_limit_middleware
from r6.stepup import validate_step_up_token
from r6.telegram_push import notify_tenant

logger = logging.getLogger(__name__)

actions_blueprint = Blueprint('actions', __name__, url_prefix='/r6/actions')

# Register rate limiting (same pattern as r6_blueprint in r6/routes.py)
rate_limit_middleware(actions_blueprint)

_TENANT_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


def _error(status, message):
    return jsonify({'error': message}), status


def _tenant_or_none():
    tenant_id = request.headers.get('X-Tenant-Id', '')
    if not tenant_id or not _TENANT_PATTERN.match(tenant_id):
        return None
    return tenant_id


@actions_blueprint.route('/propose', methods=['POST'])
def propose_action():
    tenant_id = _tenant_or_none()
    if not tenant_id:
        return _error(400, 'X-Tenant-Id header is required')

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return _error(400, 'request body must be a JSON object')
    kind = body.get('kind')
    if kind not in VALID_KINDS:
        return _error(400, 'kind must be one of: %s' % ', '.join(VALID_KINDS))
    payload = body.get('payload')
    if not isinstance(payload, dict) or not payload.get('body'):
        return _error(400, 'payload.body is required')
    if not isinstance(payload.get('body'), str):
        return _error(400, 'payload.body must be a string')
    to_label = payload.get('to')
    if to_label is not None and (not isinstance(to_label, str) or len(to_label) > 128):
        return _error(400, 'payload.to must be a string of at most 128 chars')
    if len(json.dumps(payload)) > 65536:
        return _error(400, 'payload too large (64KB max)')

    action = ProposedAction(tenant_id=tenant_id, kind=kind, payload=payload)
    db.session.add(action)
    db.session.commit()

    record_audit_event(
        'create', resource_type='ProposedAction', resource_id=action.id,
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        detail=json.dumps(action.summary()),
    )
    return jsonify(action.to_dict()), 201


@actions_blueprint.route('/<action_id>/commit', methods=['POST'])
def commit_action(action_id):
    tenant_id = _tenant_or_none()
    if not tenant_id:
        return _error(400, 'X-Tenant-Id header is required')

    # Gate 1: step-up token (ALWAYS destructure the tuple)
    step_up_token = request.headers.get('X-Step-Up-Token')
    if not step_up_token:
        return _error(401, 'Action commit requires X-Step-Up-Token header')
    valid, err = validate_step_up_token(step_up_token, tenant_id)
    if not valid:
        return _error(401, 'Step-up token rejected: %s' % err)

    # Gate 2: human confirmation (same contract as clinical writes)
    confirmed = request.headers.get('X-Human-Confirmed', '').lower()
    if confirmed != 'true':
        return jsonify({
            'error': 'Real-world actions require human confirmation. '
                     'Set X-Human-Confirmed: true after the patient approves '
                     'the draft.',
            'requires_confirmation': True,
        }), 428

    action = ProposedAction.query.filter_by(
        id=action_id, tenant_id=tenant_id).first()
    if action is None:
        return _error(404, 'Unknown action')

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if action.status == 'proposed' and action.expires_at <= now:
        # Guarded expiry: only flips rows still 'proposed' — never clobbers a
        # concurrently claimed/executing action (double-call vector otherwise).
        expired = ProposedAction.query.filter_by(
            id=action_id, tenant_id=tenant_id, status='proposed'
        ).update({'status': 'expired'}, synchronize_session=False)
        db.session.commit()
        if expired:
            record_audit_event(
                'update', resource_type='ProposedAction', resource_id=action_id,
                agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
                detail='proposal expired',
            )
            return _error(410, 'Proposal expired — propose the action again')
        db.session.refresh(action)
        return _error(409, 'Action is %s, not proposed' % action.status)

    # ATOMIC claim straight to 'executing': single guarded UPDATE whose WHERE
    # re-checks both status and expiry, closing the TOCTOU between the check
    # above and the claim. Two concurrent commits cannot both pass (a
    # double-placed phone call is worse than a failed one).
    claimed = ProposedAction.query.filter(
        ProposedAction.id == action_id,
        ProposedAction.tenant_id == tenant_id,
        ProposedAction.status == 'proposed',
        ProposedAction.expires_at > now,
    ).update({'status': 'executing'}, synchronize_session=False)
    db.session.commit()
    if not claimed:
        db.session.refresh(action)
        if action.status == 'proposed':
            # only possible reason: expired between check and claim
            return _error(410, 'Proposal expired — propose the action again')
        return _error(409, 'Action is %s, not proposed' % action.status)

    db.session.refresh(action)

    record_audit_event(
        'update', resource_type='ProposedAction', resource_id=action.id,
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        detail=json.dumps(action.summary()),
    )

    result = execute_action(action.kind, action.payload, action_id=action.id)

    if not result.ok:
        # Post-send ambiguity (timeout/garbled response/5xx) -> 'unknown',
        # never 'failed': failed invites re-propose -> duplicate call.
        new_status = 'unknown' if result.outcome_unknown else 'failed'
        updated = ProposedAction.query.filter_by(
            id=action_id, status='executing'
        ).update({'status': new_status, 'outcome_summary': result.error},
                 synchronize_session=False)
        db.session.commit()
        db.session.refresh(action)
        if not updated:
            # A provider webhook resolved the action while we were waiting —
            # its verdict wins; report the authoritative state.
            return jsonify(action.to_dict()), 200
        record_audit_event(
            'update', resource_type='ProposedAction', resource_id=action.id,
            agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
            outcome='failure', detail=result.error,
        )
        return jsonify({'id': action.id, 'status': new_status,
                        'error': result.error}), 502

    if result.simulated:
        # No webhook will ever arrive — resolve synchronously (guarded).
        ProposedAction.query.filter_by(
            id=action_id, status='executing'
        ).update({'status': 'completed',
                  'external_ref': result.external_ref,
                  'outcome_summary': 'Simulated (no provider keys configured)'},
                 synchronize_session=False)
        db.session.commit()
        db.session.refresh(action)
        record_audit_event(
            'update', resource_type='ProposedAction', resource_id=action.id,
            agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
            detail=json.dumps(action.summary()),
        )
    else:
        # Store the provider ref WITHOUT touching status — a fast webhook may
        # already have resolved the action; never clobber its verdict.
        ProposedAction.query.filter_by(id=action_id).update(
            {'external_ref': result.external_ref}, synchronize_session=False)
        db.session.commit()
        db.session.refresh(action)

    response = action.to_dict()
    response['simulated'] = result.simulated
    return jsonify(response), 200


@actions_blueprint.route('/<action_id>', methods=['GET'])
def action_status(action_id):
    tenant_id = _tenant_or_none()
    if not tenant_id:
        return _error(400, 'X-Tenant-Id header is required')

    # Read-auth: for non-public tenants (when the flag is on) require a
    # tenant-bound token/bearer, same posture as FHIR + SMBP reads.
    from r6.routes import authenticate_tenant_read
    auth_err = authenticate_tenant_read(tenant_id)
    if auth_err is not None:
        return auth_err

    action = ProposedAction.query.filter_by(
        id=action_id, tenant_id=tenant_id).first()
    if action is None:
        return _error(404, 'Unknown action')

    # Lazy expiry: a stale proposal flips to expired on read (guarded — never
    # clobbers a concurrent claim that moved the row past 'proposed').
    if action.status == 'proposed' and action.is_expired():
        expired = ProposedAction.query.filter_by(
            id=action_id, tenant_id=tenant_id, status='proposed'
        ).update({'status': 'expired'}, synchronize_session=False)
        db.session.commit()
        db.session.refresh(action)
        if expired:
            record_audit_event(
                'update', resource_type='ProposedAction', resource_id=action_id,
                agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
                detail='proposal expired',
            )

    # Only a caller holding a valid tenant-bound step-up token gets the full
    # record (phone number + message body). Everyone else gets the PHI-safe
    # summary (id/kind/recipient-label/status).
    step_up = request.headers.get('X-Step-Up-Token')
    privileged = False
    if step_up:
        valid, _err = validate_step_up_token(step_up, tenant_id)
        privileged = valid
    return jsonify(action.to_dict() if privileged else action.summary()), 200


@actions_blueprint.route('/callback/<provider>', methods=['POST'])
def action_callback(provider):
    if provider not in ('bland', 'twilio'):
        return _error(404, 'Unknown provider')

    # Shared-secret verification (constant-time). The secret rides in the
    # webhook URL registered with the provider at execution time. An
    # unconfigured secret rejects ALL callbacks — fail closed.
    expected = os.environ.get('ACTIONS_WEBHOOK_SECRET', '')
    supplied = request.args.get('secret', '')
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        return _error(403, 'Webhook verification failed')

    action_id = request.args.get('action_id', '')
    action = ProposedAction.query.filter_by(id=action_id).first()
    if action is None:
        return _error(404, 'Unknown action')

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        # Twilio status callbacks are form-encoded (MessageStatus/MessageSid)
        body = request.form.to_dict() if request.form else {}

    provider_ref = str(body.get('call_id') or body.get('MessageSid') or '')
    if provider_ref and action.external_ref and provider_ref != action.external_ref:
        return _error(404, 'Unknown action')
    provider_status = str(body.get('status') or body.get('MessageStatus') or '').lower()
    if provider_status in ('completed', 'success', 'delivered'):
        new_status = 'completed'
    elif provider_status in ('failed', 'error', 'no-answer', 'busy', 'canceled',
                             'cancelled', 'undelivered'):
        new_status = 'failed'
    else:
        # Interim or unrecognized event (queued/sent/in-progress/ringing/...):
        # acknowledge without resolving — the terminal webhook decides.
        return jsonify({'ok': True, 'note': 'non-terminal status ignored'}), 200
    summary = str(body.get('summary') or '')[:2000]

    # Atomic first-verdict-wins: only resolves rows still in flight. A late
    # or duplicate webhook (or one racing the commit route) changes nothing.
    updated = ProposedAction.query.filter(
        ProposedAction.id == action_id,
        ProposedAction.status.in_(('executing', 'unknown')),
    ).update({'status': new_status, 'outcome_summary': summary},
             synchronize_session=False)
    db.session.commit()
    db.session.refresh(action)
    if not updated:
        return jsonify({'ok': True, 'note': 'no state change'}), 200

    record_audit_event(
        'update', resource_type='ProposedAction', resource_id=action.id,
        tenant_id=action.tenant_id,
        outcome='success' if new_status == 'completed' else 'failure',
        detail=json.dumps(action.summary()),
    )

    # Telegram push: summary-level ONLY (kind + recipient label + status)
    label = action.summary().get('to') or 'recipient'
    icon = '✅' if new_status == 'completed' else '⚠️'
    notify_tenant(action.tenant_id,
                  '%s %s to %s: %s' % (icon, action.kind, label, new_status))

    return jsonify({'ok': True}), 200
