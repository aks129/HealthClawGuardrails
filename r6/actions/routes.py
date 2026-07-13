"""
Action lifecycle API — propose / commit / confirm / status / provider callbacks.

Approve-is-the-commit (Task 10):
  propose  -> tenant header only; red-flag screened + rail-validated;
              returns draft for human review
  commit   -> X-Step-Up-Token (validated, tuple destructured); SUBMITS the
              action for out-of-band approval (202, awaiting_confirmation).
              Nothing executes here — the spoofable X-Human-Confirmed gate
              is gone.
  confirm  -> the human's out-of-band Approve (dashboard/Telegram). The ONLY
              place an action executes: guarded claim -> consent record ->
              provider call.
  callback -> shared-secret verification

Audit detail and Telegram pushes use ProposedAction.summary() ONLY (no PHI).
"""

import hmac
import json
import logging
import os
import re
import uuid

from flask import Blueprint, jsonify, request

from models import db
from r6.actions import errors
from r6.actions.confirmations import (APPROVED_VIA_VALUES,
                                      consume_confirmation,
                                      issue_confirmation)
from r6.actions.models import ProposedAction, VALID_KINDS, _utcnow
from r6.actions.registry import get_executor
from r6.actions.rx_transfer import build_transfer_request
from r6.actions.safety import EMERGENCY_MESSAGE, screen_text
from r6.actions.state import transition_action
from r6.audit import record_audit_event
from r6.rate_limit import rate_limit_middleware
from r6.read_auth import authorize_tenant_read
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


def _emergency_refusal_or_none(tenant_id, text):
    """Mandatory red-flag screen on free-text at PROPOSE. A hit refuses the
    action with 911/urgent-care escalation, audited like a Schedule-II
    refusal (matched lexicon phrase only — never the free text itself)."""
    hit = screen_text(text)
    if hit is None:
        return None
    record_audit_event(
        'create', resource_type='ProposedAction',
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        outcome='failure', detail='emergency_indicated: %s' % hit['matched'],
    )
    return jsonify({'error_code': errors.EMERGENCY_INDICATED,
                    'error': EMERGENCY_MESSAGE}), 422


def _rail_validation_or_none(kind, payload):
    """Executor validation when a rail is registered for this kind. A
    VALID_KINDS entry with no registered rail (none today; possible for a
    future kind) skips this — VALID_KINDS remains the propose gate, and
    such a kind fails loud at confirm instead."""
    ex = get_executor(kind)
    if ex is None:
        return None
    errs = ex.validate(payload)
    if errs:
        return jsonify({'error_code': errors.PAYLOAD_INVALID,
                        'error': 'Action payload failed %s rail validation.'
                                 % kind,
                        'errors': errs}), 422
    return None


def _resolve_from_executing(action, result):
    """Map an executor verdict onto the state machine for an action just
    claimed into 'executing'. The never-clobber rule lives HERE and only
    here: every status change is a guarded transition from 'executing', and
    a False return means a provider webhook resolved the action first — the
    webhook's verdict wins and the authoritative state is reported (200)."""
    action_id = action.id
    tenant_id = action.tenant_id
    agent_id = request.headers.get('X-Agent-Id')
    label = action.summary().get('to') or 'recipient'

    if result.status == 'executing':
        # Provider accepted; a webhook will resolve. Store the ref WITHOUT
        # touching status — a fast webhook may already have resolved the
        # action; never clobber its verdict.
        ProposedAction.query.filter_by(id=action_id).update(
            {'external_ref': result.provider_ref}, synchronize_session=False)
        db.session.commit()
        db.session.refresh(action)
        record_audit_event(
            'update', resource_type='ProposedAction', resource_id=action.id,
            agent_id=agent_id, tenant_id=tenant_id,
            detail=json.dumps(action.summary()),
        )
        notify_tenant(tenant_id, '📤 %s to %s: %s'
                      % (action.kind, label, action.status))
        if action.status != 'executing':
            # Fast webhook won while we were storing the ref — report the
            # authoritative state.
            return jsonify(action.to_dict()), 200
        return jsonify({'id': action.id, 'status': 'executing',
                        'note': 'provider accepted; webhook will resolve'}), 200

    if result.status == 'completed':
        fields = {'outcome_summary': (json.dumps(result.outcome)
                                      if result.outcome else 'completed')}
        if result.provider_ref:
            fields['external_ref'] = result.provider_ref
        resolved = transition_action(
            action_id, from_states=('executing',), to_state='completed',
            actor='confirm', **fields)
        db.session.refresh(action)
        if not resolved:
            return jsonify(action.to_dict()), 200   # webhook won
        record_audit_event(
            'update', resource_type='ProposedAction', resource_id=action.id,
            agent_id=agent_id, tenant_id=tenant_id,
            detail=json.dumps(action.summary()),
        )
        notify_tenant(tenant_id, '✅ %s to %s: completed'
                      % (action.kind, label))
        return jsonify(action.to_dict()), 200

    if result.status == 'needs_review':
        resolved = transition_action(
            action_id, from_states=('executing',), to_state='needs_review',
            actor='confirm',
            outcome_summary=(json.dumps(result.outcome)
                             if result.outcome else 'needs review'))
        db.session.refresh(action)
        if not resolved:
            return jsonify(action.to_dict()), 200   # webhook won
        record_audit_event(
            'update', resource_type='ProposedAction', resource_id=action.id,
            agent_id=agent_id, tenant_id=tenant_id,
            detail=json.dumps(action.summary()),
        )
        notify_tenant(tenant_id, '⚠️ %s to %s: needs review'
                      % (action.kind, label))
        return jsonify({'id': action.id, 'status': 'needs_review',
                        'outcome_summary': action.outcome_summary}), 200

    # result.status == 'failed' (ExecutionResult admits no other statuses).
    # Post-send ambiguity (timeout/garbled response/5xx) -> 'unknown', never
    # 'failed': failed invites re-propose -> duplicate call.
    new_status = 'unknown' if result.outcome_unknown else 'failed'
    resolved = transition_action(
        action_id, from_states=('executing',), to_state=new_status,
        actor='confirm', outcome_summary=result.error)
    db.session.refresh(action)
    if not resolved:
        return jsonify(action.to_dict()), 200       # webhook won
    record_audit_event(
        'update', resource_type='ProposedAction', resource_id=action.id,
        agent_id=agent_id, tenant_id=tenant_id,
        outcome='failure', detail=result.error,
    )
    notify_tenant(tenant_id, '⚠️ %s to %s: %s'
                  % (action.kind, label, new_status))
    # Coerce onto the error taxonomy: executors SHOULD return codes from
    # r6.actions.errors, but the API contract never leaks anything else.
    error_code = (result.error if result.error in errors.ALL
                  else errors.PROVIDER_ERROR)
    response = {'id': action.id, 'status': new_status,
                'error_code': error_code,
                'error': 'Provider execution failed (%s).' % error_code}
    if result.outcome_unknown:
        response['note'] = ('Provider outcome unknown — the provider may '
                            'have acted; do not re-propose without '
                            'reconciliation.')
    return jsonify(response), 502


@actions_blueprint.route('/rx-transfer/propose', methods=['POST'])
def propose_rx_transfer():
    """Build a prescription-transfer request call from the tenant's active
    MedicationRequests and stage it as a proposed phone-call action.

    Body: {to_pharmacy: {name, phone}, from_pharmacy?: {name, phone},
           medication_names?: [str]}  (names filter; default = all active)

    Commit is the EXISTING /actions/<id>/commit — step-up stays mandatory and
    commit only SUBMITS for out-of-band approval; this endpoint only drafts.
    Schedule II medications are refused with an explanation (never
    transferable; see rx_transfer.py).
    """
    tenant_id = _tenant_or_none()
    if not tenant_id:
        return _error(400, 'X-Tenant-Id header is required')
    tenant_id = authorize_tenant_read(tenant_id)
    if tenant_id is None:
        return _error(401, 'authentication required for this tenant')

    body = request.get_json(silent=True) or {}
    to_pharmacy = body.get('to_pharmacy') or {}
    if not (isinstance(to_pharmacy, dict) and to_pharmacy.get('name')
            and to_pharmacy.get('phone')):
        return _error(400, 'to_pharmacy {name, phone} is required')
    from_pharmacy = body.get('from_pharmacy') \
        if isinstance(body.get('from_pharmacy'), dict) else None
    name_filter = body.get('medication_names')

    from r6.models import R6Resource
    rows = R6Resource.query.filter_by(
        resource_type='MedicationRequest', tenant_id=tenant_id).all()
    meds = [r.to_fhir_json() for r in rows]
    if isinstance(name_filter, list) and name_filter:
        wanted = {n.lower() for n in name_filter if isinstance(n, str)}
        meds = [m for m in meds
                if (m.get('medicationCodeableConcept') or {}).get('text', '')
                .lower() in wanted]

    result = build_transfer_request(meds, to_pharmacy,
                                    from_pharmacy=from_pharmacy)
    if result['action_payload'] is None:
        return jsonify({
            'error': 'no transferable medications',
            'refused': result['refused'],
            'detail': ('Nothing to transfer: no active medication orders '
                       'matched, or all matches are Schedule II (which can '
                       'never be transferred — a new prescription is '
                       'required).'),
        }), 422

    # This endpoint becomes a write only when a transferable draft exists.
    # Read-scoped credentials may preview the no-action/refusal response above,
    # but persisting a ProposedAction requires an explicit write-capable token.
    step_up_token = (request.headers.get('X-Step-Up-Token') or '').strip()
    if not step_up_token:
        auth = (request.headers.get('Authorization') or '').strip()
        if auth.lower().startswith('bearer '):
            step_up_token = auth[7:].strip()
    if not step_up_token:
        return _error(401, 'write-scoped X-Step-Up-Token required')
    valid, _token_error = validate_step_up_token(step_up_token, tenant_id)
    if not valid:
        return _error(401, 'write-scoped token rejected')

    refusal = _emergency_refusal_or_none(tenant_id,
                                         result['action_payload'].get('body'))
    if refusal is not None:
        return refusal
    invalid = _rail_validation_or_none('phone-call', result['action_payload'])
    if invalid is not None:
        return invalid

    action = ProposedAction(tenant_id=tenant_id, kind='phone-call',
                            payload=result['action_payload'])
    db.session.add(action)
    db.session.commit()

    record_audit_event(
        'create', resource_type='ProposedAction', resource_id=action.id,
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        detail=json.dumps(action.summary()),
    )

    return jsonify({
        'action': action.summary(),
        'allowed': result['allowed'],
        'refused': result['refused'],
        'next_step': ('Review the draft with the patient, then submit via '
                      'POST /r6/actions/%s/commit with X-Step-Up-Token. The '
                      'patient then approves out of band (dashboard/Telegram) '
                      'to execute.' % action.id),
    }), 201


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

    refusal = _emergency_refusal_or_none(tenant_id, payload['body'])
    if refusal is not None:
        return refusal
    invalid = _rail_validation_or_none(kind, payload)
    if invalid is not None:
        return invalid

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
    """SUBMIT-FOR-CONFIRMATION. Nothing executes here: commit moves the
    proposal to awaiting_confirmation and pushes an approval request to the
    patient's out-of-band channels. The old X-Human-Confirmed header gate is
    gone — any caller could spoof a header; nobody can spoof the patient
    tapping Approve in their own dashboard/Telegram."""
    tenant_id = _tenant_or_none()
    if not tenant_id:
        return _error(400, 'X-Tenant-Id header is required')

    # Gate: step-up token (ALWAYS destructure the tuple)
    step_up_token = request.headers.get('X-Step-Up-Token')
    if not step_up_token:
        return _error(401, 'Action commit requires X-Step-Up-Token header')
    valid, err = validate_step_up_token(step_up_token, tenant_id)
    if not valid:
        return _error(401, 'Step-up token rejected: %s' % err)

    action = ProposedAction.query.filter_by(
        id=action_id, tenant_id=tenant_id).first()
    if action is None:
        return _error(404, 'Unknown action')

    if action.status == 'proposed' and action.is_expired():
        # Guarded expiry: only flips rows still 'proposed' — never clobbers a
        # concurrently advanced action.
        expired = transition_action(
            action_id, from_states=('proposed',), to_state='expired',
            actor='commit-route', detail='proposal expired')
        db.session.refresh(action)
        if expired:
            record_audit_event(
                'update', resource_type='ProposedAction', resource_id=action_id,
                agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
                detail='proposal expired',
            )
            return _error(410, 'Proposal expired — propose the action again')
        return _error(409, 'Action is %s, not proposed' % action.status)

    # The claim's WHERE re-checks expiry (extra_criteria), closing the TOCTOU
    # between the snapshot check above and the claim: an expired row can
    # never win, even if it expired between the two statements.
    moved = transition_action(
        action_id, from_states=('proposed',),
        to_state='awaiting_confirmation', actor='commit-route',
        extra_criteria=[ProposedAction.expires_at > _utcnow()])
    db.session.refresh(action)
    if not moved:
        if action.status == 'proposed':
            # Still 'proposed' means the expiry predicate refused the claim —
            # flip it (guarded) and report, mirroring the snapshot path.
            expired = transition_action(
                action_id, from_states=('proposed',), to_state='expired',
                actor='commit-route', detail='proposal expired')
            db.session.refresh(action)
            if expired:
                record_audit_event(
                    'update', resource_type='ProposedAction',
                    resource_id=action_id,
                    agent_id=request.headers.get('X-Agent-Id'),
                    tenant_id=tenant_id, detail='proposal expired',
                )
                return _error(410, 'Proposal expired — propose the action '
                                   'again')
        if action.status == 'expired':
            return _error(410, 'Proposal expired — propose the action again')
        return _error(409, 'Action is %s, not proposed' % action.status)

    record_audit_event(
        'update', resource_type='ProposedAction', resource_id=action.id,
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        detail=json.dumps(action.summary()),
    )

    # Telegram push: summary-level ONLY (kind + recipient label)
    label = action.summary().get('to') or 'recipient'
    notify_tenant(tenant_id,
                  '🔔 Approval needed: %s to %s. Review and approve in your '
                  'HealthClaw dashboard.' % (action.kind, label))

    return jsonify({
        'id': action.id,
        'status': 'awaiting_confirmation',
        'next_step': ('Terminal for this turn: the patient must approve out '
                      'of band (dashboard/Telegram). Poll GET /r6/actions/%s '
                      'or end your turn. Do not retry commit.' % action.id),
    }), 202


@actions_blueprint.route('/<action_id>/confirm', methods=['POST'])
def confirm_action(action_id):
    """The human's out-of-band Approve — the ONLY place an action executes.
    Called by the dashboard/Telegram approve handler with the patient's own
    tenant-bound step-up token. Body optional: {'approved_via': 'dashboard'
    | 'telegram'} (default 'dashboard').

    Ordering rationale (do not reorder):
      1. THE CLAIM FIRST. The guarded transition awaiting_confirmation ->
         executing is the mutex — a single guarded UPDATE, so exactly one
         concurrent Approve wins. It must precede issuing the confirmation:
         transition_action() COMMITS the session on both branches, so a
         pre-issued ActionConfirmation row would be committed even when the
         claim loses — a consent record for an execution that never happened.
      2. AFTER a winning claim, issue + immediately consume the
         ActionConfirmation. The claim is the LOCK; the confirmation row
         (issued and consumed at the same instant, one transaction) is the
         CONSENT RECORD — the durable who/when/via artifact of the approval.
    """
    tenant_id = _tenant_or_none()
    if not tenant_id:
        return _error(400, 'X-Tenant-Id header is required')

    # Pure input validation FIRST: a 400 on a malformed body must not burn
    # the single-use credential consumed just below. Everything that touches
    # state (load/claim/execute) stays behind the consuming validation.
    body = request.get_json(silent=True) or {}
    approved_via = body.get('approved_via', 'dashboard')
    if approved_via not in APPROVED_VIA_VALUES:
        return _error(400, 'approved_via must be one of: %s'
                      % ', '.join(APPROVED_VIA_VALUES))

    step_up_token = request.headers.get('X-Step-Up-Token')
    if not step_up_token:
        return _error(401, 'Action confirm requires X-Step-Up-Token header')
    # consume_nonce: the confirm token is a SINGLE-USE execution credential
    # (spec v3). Commit validates the same token multi-use (submitting is not
    # an execution); only the human's Approve spends the nonce, so a captured
    # token can never authorize a second real-world execution.
    valid, err = validate_step_up_token(step_up_token, tenant_id,
                                        consume_nonce=True)
    if not valid:
        return _error(401, 'Step-up token rejected: %s' % err)

    # (a) Load, tenant-scoped.
    action = ProposedAction.query.filter_by(
        id=action_id, tenant_id=tenant_id).first()
    if action is None:
        return _error(404, 'Unknown action')

    # (b) Guarded expiry: a stale approval window lapses instead of dialing.
    if action.status == 'awaiting_confirmation' and action.is_expired():
        lapsed = transition_action(
            action_id, from_states=('awaiting_confirmation',),
            to_state='expired', actor='confirm',
            detail='approval window lapsed')
        db.session.refresh(action)
        if lapsed:
            record_audit_event(
                'update', resource_type='ProposedAction', resource_id=action_id,
                agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
                detail='approval window lapsed',
            )
            return _error(410, 'Approval window lapsed — propose the action '
                               'again')
        # Lost to a concurrent claim — the claim below settles it.

    # (c) THE CLAIM: single-winner mutex (see docstring for why this comes
    # before the confirmation record). The WHERE re-checks expiry
    # (extra_criteria), closing the TOCTOU between the snapshot check above
    # and the claim: a lapsed approval window can never dial.
    moved = transition_action(
        action_id, from_states=('awaiting_confirmation',),
        to_state='executing', actor='confirm',
        attempt_id=str(uuid.uuid4()), claimed_at=_utcnow(),
        extra_criteria=[ProposedAction.expires_at > _utcnow()])
    if not moved:
        db.session.refresh(action)
        if action.status == 'awaiting_confirmation':
            # Still awaiting means the expiry predicate refused the claim —
            # flip it (guarded) and report, mirroring the snapshot path.
            lapsed = transition_action(
                action_id, from_states=('awaiting_confirmation',),
                to_state='expired', actor='confirm',
                detail='approval window lapsed')
            db.session.refresh(action)
            if lapsed:
                record_audit_event(
                    'update', resource_type='ProposedAction',
                    resource_id=action_id,
                    agent_id=request.headers.get('X-Agent-Id'),
                    tenant_id=tenant_id, detail='approval window lapsed',
                )
                return _error(410, 'Approval window lapsed — propose the '
                                   'action again')
        if action.status == 'expired':
            return _error(410, 'Approval window lapsed — propose the action '
                               'again')
        return _error(409, 'Action is %s, not awaiting_confirmation'
                      % action.status)
    db.session.refresh(action)

    # (d) Consent record: issued + immediately consumed — same instant, one
    # transaction. The claim above is the lock; this row is the audit
    # artifact of who/when/via.
    issue_confirmation(action_id, approved_via, ttl_minutes=15)
    db.session.flush()
    consume_confirmation(action_id)
    db.session.commit()

    record_audit_event(
        'update', resource_type='ProposedAction', resource_id=action.id,
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        detail='approved via %s; %s' % (approved_via,
                                        json.dumps(action.summary())),
    )

    # (e) A kind with no registered rail fails loud — never a fake success.
    # Every VALID_KINDS entry has a rail today (form-fill's is a Task-3
    # skeleton), so this is defensive for a future kind added to
    # VALID_KINDS ahead of its rail. Checked before the provider_request_at
    # stamp: no provider call is even possible here.
    ex = get_executor(action.kind)
    if ex is None:
        summary = 'No executor for kind: %s' % action.kind
        failed = transition_action(
            action_id, from_states=('executing',), to_state='failed',
            actor='confirm', outcome_summary=summary)
        db.session.refresh(action)
        if failed:
            record_audit_event(
                'update', resource_type='ProposedAction', resource_id=action.id,
                agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
                outcome='failure', detail=summary,
            )
            label = action.summary().get('to') or 'recipient'
            notify_tenant(tenant_id, '⚠️ %s to %s: failed'
                          % (action.kind, label))
        return jsonify({'id': action.id, 'status': action.status,
                        'error_code': errors.PROVIDER_NOT_CONFIGURED,
                        'error': summary}), 502

    # (f) Stamp provider_request_at immediately before the provider call so a
    # crash is distinguishable: claimed-but-never-called (safe to fail) vs
    # called-but-unresolved (needs review). Guarded update + commit.
    ProposedAction.query.filter_by(id=action_id, status='executing').update(
        {'provider_request_at': _utcnow()}, synchronize_session=False)
    db.session.commit()

    # (g) Execute, then (h) resolve — the never-clobber / webhook-wins
    # mapping lives in ONE place: _resolve_from_executing().
    result = ex.execute(action)
    return _resolve_from_executing(action, result)


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
        expired = transition_action(
            action_id, from_states=('proposed',), to_state='expired',
            actor='status-route', detail='proposal expired')
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
    # or duplicate webhook (or one racing the confirm route) changes nothing.
    # Via transition_action so webhook resolutions land in the ActionEvent
    # ledger (state.py is the only sanctioned status writer; W1 views read
    # these events).
    updated = transition_action(
        action_id, from_states=('executing', 'unknown'), to_state=new_status,
        actor='callback:%s' % provider, outcome_summary=summary)
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
