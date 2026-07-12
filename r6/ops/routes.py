"""Ops blueprint — infra endpoints (spec W0 §1 preflight + Durable execution).

GET  /r6/ops/preflight — config preflight. Always 200; the JSON body carries
     the verdict ({ok: all-fatal-checks-ok, checks: [...]}) so monitoring
     reads the payload, not the status code.

Auth: the same step-up gate as action commit (X-Tenant-Id + a valid
tenant-bound X-Step-Up-Token). The checks themselves are global — the tenant
binding just keeps this surface consistent with every other guarded endpoint
rather than inventing an infra-only credential.
"""
import logging
import re

from flask import Blueprint, jsonify, request

from r6.ops import checks as preflight_checks
from r6.rate_limit import rate_limit_middleware
from r6.stepup import validate_step_up_token

logger = logging.getLogger(__name__)

ops_blueprint = Blueprint('ops', __name__, url_prefix='/r6/ops')

rate_limit_middleware(ops_blueprint)

_TENANT_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


def _error(status, message):
    return jsonify({'error': message}), status


def _auth_error_or_none():
    """Gate 1, same as action commit: tenant header + valid tenant-bound
    step-up token (multi-use validation; ops calls are not executions, so
    no nonce is consumed). Returns an error response or None."""
    tenant_id = request.headers.get('X-Tenant-Id', '')
    if not tenant_id or not _TENANT_PATTERN.match(tenant_id):
        return _error(400, 'X-Tenant-Id header is required')
    step_up_token = request.headers.get('X-Step-Up-Token')
    if not step_up_token:
        return _error(401, 'Ops endpoints require X-Step-Up-Token header')
    valid, err = validate_step_up_token(step_up_token, tenant_id)
    if not valid:
        return _error(401, 'Step-up token rejected: %s' % err)
    return None


@ops_blueprint.route('/preflight', methods=['GET'])
def preflight():
    auth_err = _auth_error_or_none()
    if auth_err is not None:
        return auth_err

    results = preflight_checks.run_all()
    ok = all(check['ok'] for check in results if check['fatal'])
    return jsonify({'ok': ok, 'checks': results}), 200
