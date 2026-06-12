"""
Action lifecycle API — propose / commit / status / provider callbacks.

Contract mirrors FHIR writes:
  propose  -> tenant header only, returns draft for human review
  commit   -> X-Step-Up-Token (validated, tuple destructured) AND
              X-Human-Confirmed: true, else 401 / 428
  callback -> shared-secret verification

Audit detail and Telegram pushes use ProposedAction.summary() ONLY (no PHI).
"""

import json
import logging
import re

from flask import Blueprint, jsonify, request

from models import db
from r6.actions.models import ProposedAction, VALID_KINDS
from r6.audit import record_audit_event
from r6.rate_limit import rate_limit_middleware

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
