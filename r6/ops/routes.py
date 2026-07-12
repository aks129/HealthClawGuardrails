"""Ops blueprint — infra endpoints (spec W0 §1 preflight + Durable execution).

GET  /r6/ops/preflight — config preflight. Always 200; the JSON body carries
     the verdict ({ok: all-fatal-checks-ok, checks: [...]}) so monitoring
     reads the payload, not the status code.

POST /r6/ops/reap — the external-tick reaper (5-minute external cron; no
     RQ/Celery). At-most-once + detect-and-reconcile: stale 'executing'
     rows with a provider ref are reconciled against provider truth;
     crashed claims that provably never reached the provider fail safely;
     claims that MAY have reached the provider go to needs_review (never
     auto-retried); 'unknown' rows with a ref are reconciled; lapsed
     approval windows expire with a summary-only nudge to re-propose.

Auth: the same step-up gate as action commit (X-Tenant-Id + a valid
tenant-bound X-Step-Up-Token). The sweeps themselves are global — the tenant
binding just keeps this surface consistent with every other guarded endpoint
rather than inventing an infra-only credential.
"""
import json
import logging
import re
from datetime import timedelta

from flask import Blueprint, jsonify, request

from models import db
from r6.actions.models import ProposedAction, _utcnow
from r6.actions.registry import get_executor
from r6.actions.state import transition_action
from r6.audit import record_audit_event
from r6.ops import checks as preflight_checks
from r6.rate_limit import rate_limit_middleware
from r6.stepup import validate_step_up_token
from r6.telegram_push import notify_tenant

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


# --------------------------------------------------------------- reaper

# A row is reapable once it has sat untouched past this threshold (the
# external cron ticks every 5 minutes, so anything older missed its webhook
# or crashed mid-flight).
STALE_AFTER = timedelta(minutes=5)


def _audit_reaped(action, to_state, detail):
    record_audit_event(
        'update', resource_type='ProposedAction', resource_id=action.id,
        agent_id='reaper', tenant_id=action.tenant_id,
        outcome='success' if to_state in ('completed', 'expired')
        else 'failure',
        detail=detail)


def _apply_reconcile(action, result):
    """Map an executor reconcile() verdict onto the state machine via a
    guarded transition from the action's CURRENT state ('executing' or
    'unknown' — both admit completed/failed/needs_review; see _TRANSITIONS).
    A 'executing' verdict means the provider is still working: leave the row
    alone. Returns the transition dict, or None if nothing moved."""
    from_state = action.status
    if result.status == 'executing':
        return None
    if result.status == 'completed':
        to_state = 'completed'
        fields = {'outcome_summary': (json.dumps(result.outcome)
                                      if result.outcome else 'completed')}
        if result.provider_ref:
            fields['external_ref'] = result.provider_ref
    elif result.status == 'needs_review':
        # Evidence (whatever the provider could tell us) rides in
        # outcome_summary so a human reviewer sees WHY it needs review.
        to_state = 'needs_review'
        fields = {'outcome_summary': (json.dumps(result.outcome)
                                      if result.outcome else 'needs review')}
    else:   # 'failed' — ExecutionResult admits no other statuses
        to_state = 'failed'
        fields = {'outcome_summary': result.error or 'failed'}
    moved = transition_action(
        action.id, from_states=(from_state,), to_state=to_state,
        actor='reaper', detail='reconciled against provider truth', **fields)
    if not moved:
        return None     # a webhook resolved it mid-sweep — its verdict wins
    _audit_reaped(action, to_state,
                  'reaper reconcile: %s -> %s' % (from_state, to_state))
    return {'id': action.id, 'from': from_state, 'to': to_state}


def _reap_executing(action, stale_before):
    """One stale-'executing' row. Three forensic cases (attempt ledger):
    ref recorded -> ask the provider; no ref and provider provably never
    called -> safe to fail; no ref but the provider POST was attempted ->
    needs_review, NEVER auto-retry (a re-dial could double-act)."""
    if action.external_ref:
        if action.updated_at is None or action.updated_at >= stale_before:
            return None     # fresh — its webhook may still be coming
        executor = get_executor(action.kind)
        if executor is None:
            logger.warning('reaper: no executor for kind=%s (action %s); '
                           'leaving for manual review', action.kind,
                           action.id)
            return None
        return _apply_reconcile(action, executor.reconcile(action))

    if action.provider_request_at is None:
        # Claimed but the provider POST never started — failing is safe.
        if action.claimed_at is None or action.claimed_at >= stale_before:
            return None
        moved = transition_action(
            action.id, from_states=('executing',), to_state='failed',
            actor='reaper', detail='claimed but provider never called',
            outcome_summary='provider never called; reaped after crash')
        if not moved:
            return None
        _audit_reaped(action, 'failed', 'claimed but provider never called')
        return {'id': action.id, 'from': 'executing', 'to': 'failed'}

    # provider_request_at set, no ref recorded: the provider MAY have acted.
    if action.provider_request_at >= stale_before:
        return None
    moved = transition_action(
        action.id, from_states=('executing',), to_state='needs_review',
        actor='reaper', detail='provider request sent; no ref recorded',
        outcome_summary='provider may have acted; no ref recorded — '
                        'do not re-propose without manual review')
    if not moved:
        return None
    _audit_reaped(action, 'needs_review',
                  'provider may have acted; no ref recorded')
    return {'id': action.id, 'from': 'executing', 'to': 'needs_review'}


def _reap_unknown(action):
    """'unknown' + external_ref: post-possible-send ambiguity — reconcile
    against provider truth (unknown -> completed/failed/needs_review are all
    legal transitions)."""
    executor = get_executor(action.kind)
    if executor is None:
        logger.warning('reaper: no executor for kind=%s (action %s)',
                       action.kind, action.id)
        return None
    return _apply_reconcile(action, executor.reconcile(action))


def _reap_lapsed_approval(action):
    """awaiting_confirmation past its window -> expired, plus a summary-only
    nudge (kind + recipient label, NEVER the payload) to re-propose."""
    moved = transition_action(
        action.id, from_states=('awaiting_confirmation',),
        to_state='expired', actor='reaper',
        detail='approval window lapsed (reaper sweep)')
    if not moved:
        return None
    _audit_reaped(action, 'expired', 'approval window lapsed (reaper sweep)')
    label = action.summary().get('to') or 'recipient'
    notify_tenant(action.tenant_id,
                  '⏰ Your pending approval for the %s to %s lapsed — '
                  'propose it again if you still want it.'
                  % (action.kind, label))
    return {'id': action.id, 'from': 'awaiting_confirmation', 'to': 'expired'}


@ops_blueprint.route('/reap', methods=['POST'])
def reap():
    auth_err = _auth_error_or_none()
    if auth_err is not None:
        return auth_err

    now = _utcnow()
    stale_before = now - STALE_AFTER
    transitions = []

    def _executing_handler(action):
        return _reap_executing(action, stale_before)

    sweep = []
    sweep.extend(
        (a, _executing_handler)
        for a in ProposedAction.query.filter_by(status='executing').all())
    sweep.extend(
        (a, _reap_unknown)
        for a in ProposedAction.query.filter(
            ProposedAction.status == 'unknown',
            ProposedAction.external_ref.isnot(None)).all())
    sweep.extend(
        (a, _reap_lapsed_approval)
        for a in ProposedAction.query.filter(
            ProposedAction.status == 'awaiting_confirmation',
            ProposedAction.expires_at <= now).all())

    for action, handler in sweep:
        # Per-action containment: one bad row (provider API down, malformed
        # payload, ...) must never abort the rest of the sweep.
        try:
            outcome = handler(action)
            if outcome is not None:
                transitions.append(outcome)
        except Exception:   # noqa: BLE001 — contain, log, keep sweeping
            logger.exception('reaper: action %s raised during sweep; '
                             'skipping', action.id)
            db.session.rollback()

    return jsonify({'swept': len(transitions),
                    'transitions': transitions}), 200
