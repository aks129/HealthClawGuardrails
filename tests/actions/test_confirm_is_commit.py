"""Approve-is-the-commit contract (Task 10).

commit = SUBMIT-FOR-CONFIRMATION (202, nothing executes, no spoofable
header); POST /<id>/confirm = the human's out-of-band Approve, and the ONLY
place a provider call happens. The guarded claim transition is the mutex
(single winner); the ActionConfirmation row is the consent record.
"""
import json
from datetime import datetime, timedelta, timezone

from models import db
from r6.actions import errors
from r6.actions.models import ProposedAction


PROPOSE_BODY = {
    'kind': 'phone-call',
    'payload': {
        'to': 'CVS Pharmacy',
        'phone': '617-555-0100',
        'body': 'Hi, calling for a refill of metformin 500mg for John Smith.',
    },
}


def _past():
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)


def _propose(client, tenant_headers, body=None):
    resp = client.post('/r6/actions/propose', json=body or PROPOSE_BODY,
                       headers=tenant_headers)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()['id']


def _commit(client, auth_headers, action_id):
    return client.post('/r6/actions/%s/commit' % action_id,
                       headers=auth_headers)


def _confirm(client, auth_headers, action_id, body=None):
    return client.post('/r6/actions/%s/confirm' % action_id,
                       headers=auth_headers, json=body or {})


# ---------------------------------------------------------------------------
# commit = submit-for-confirmation (nothing executes)
# ---------------------------------------------------------------------------

def test_commit_returns_202_and_executes_nothing(client, tenant_headers,
                                                 auth_headers, app,
                                                 action_registry,
                                                 fake_providers, monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _propose(client, tenant_headers)
    resp = _commit(client, auth_headers, action_id)
    assert resp.status_code == 202
    data = resp.get_json()
    assert data['status'] == 'awaiting_confirmation'
    # THE guarantee: submit never touches the provider.
    assert fake_providers == []
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'awaiting_confirmation'


def test_double_commit_conflicts(client, tenant_headers, auth_headers):
    action_id = _propose(client, tenant_headers)
    assert _commit(client, auth_headers, action_id).status_code == 202
    assert _commit(client, auth_headers, action_id).status_code == 409


def test_spoofed_human_confirmed_header_does_not_execute(
        client, tenant_headers, auth_headers, action_registry,
        fake_providers, monkeypatch):
    """The old spoofable gate is dead: an agent asserting X-Human-Confirmed
    gets a 202 submit like everyone else — never an execution."""
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _propose(client, tenant_headers)
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code == 202
    assert resp.get_json()['status'] == 'awaiting_confirmation'
    assert fake_providers == []


# ---------------------------------------------------------------------------
# confirm = the human's Approve (this executes)
# ---------------------------------------------------------------------------

def test_confirm_executes_exactly_once(client, tenant_headers, auth_headers,
                                       app, action_registry, fake_providers,
                                       monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)

    resp = _confirm(client, auth_headers, action_id)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['status'] == 'executing'
    assert len(fake_providers) == 1
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'executing'
        assert row.external_ref == 'fake-123'   # provider ref stored
        assert row.attempt_id                   # claim stamped the ledger
        assert row.claimed_at is not None
        assert row.provider_request_at is not None

    # Second Approve from another device carries its own FRESH step-up token
    # (the first token's nonce is consumed by the first confirm — see the
    # replay tests below): the claim is already spent -> 409, and crucially
    # the provider was NOT called again.
    from r6.stepup import generate_step_up_token
    fresh = dict(auth_headers)
    fresh['X-Step-Up-Token'] = generate_step_up_token(
        tenant_headers['X-Tenant-Id'])
    second = _confirm(client, fresh, action_id)
    assert second.status_code == 409
    assert len(fake_providers) == 1


def test_confirm_requires_step_up(client, tenant_headers, auth_headers):
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    resp = client.post('/r6/actions/%s/confirm' % action_id,
                       headers=tenant_headers)
    assert resp.status_code == 401


def test_confirm_before_commit_conflicts(client, tenant_headers, auth_headers,
                                         action_registry, fake_providers):
    action_id = _propose(client, tenant_headers)
    resp = _confirm(client, auth_headers, action_id)
    assert resp.status_code == 409
    assert fake_providers == []


def test_confirm_expired_awaiting_confirmation_410(client, tenant_headers,
                                                   auth_headers, app,
                                                   action_registry,
                                                   fake_providers):
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        row.expires_at = _past()
        db.session.commit()
    resp = _confirm(client, auth_headers, action_id)
    assert resp.status_code == 410
    assert fake_providers == []
    with app.app_context():
        assert db.session.get(ProposedAction, action_id).status == 'expired'


def test_expired_row_cannot_win_commit_claim(client, tenant_headers,
                                             auth_headers, app, monkeypatch):
    """TOCTOU closure: expiry that lands between the route's snapshot check
    and the claim must still lose — the claim's WHERE re-checks expires_at.
    Blinding is_expired() simulates the row expiring after the snapshot."""
    action_id = _propose(client, tenant_headers)
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        row.expires_at = _past()
        db.session.commit()
    monkeypatch.setattr(ProposedAction, 'is_expired', lambda self: False)
    resp = client.post('/r6/actions/%s/commit' % action_id,
                       headers=auth_headers)
    assert resp.status_code == 410
    with app.app_context():
        assert db.session.get(ProposedAction, action_id).status == 'expired'


def test_expired_row_cannot_win_confirm_claim(client, tenant_headers,
                                              auth_headers, app,
                                              action_registry, fake_providers,
                                              monkeypatch):
    """Same TOCTOU closure on the execution claim: an approval window that
    lapses between the snapshot check and the claim must never dial."""
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        row.expires_at = _past()
        db.session.commit()
    monkeypatch.setattr(ProposedAction, 'is_expired', lambda self: False)
    resp = _confirm(client, auth_headers, action_id)
    assert resp.status_code == 410
    assert fake_providers == []   # never executed
    with app.app_context():
        from r6.actions.confirmations import ActionConfirmation
        assert db.session.get(ProposedAction, action_id).status == 'expired'
        # No consent record for an execution that never happened.
        assert ActionConfirmation.query.filter_by(
            action_id=action_id).count() == 0


def test_confirm_tenant_isolation(client, tenant_headers, auth_headers,
                                  other_tenant_headers, action_registry,
                                  fake_providers):
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    resp = client.post('/r6/actions/%s/confirm' % action_id,
                       headers=other_tenant_headers, json={})
    assert resp.status_code == 404
    assert fake_providers == []


def test_confirmation_row_is_the_consent_record(client, tenant_headers,
                                                auth_headers, app,
                                                action_registry,
                                                fake_providers, monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    resp = _confirm(client, auth_headers, action_id,
                    body={'approved_via': 'telegram'})
    assert resp.status_code == 200
    with app.app_context():
        from r6.actions.confirmations import ActionConfirmation
        rows = ActionConfirmation.query.filter_by(action_id=action_id).all()
        assert len(rows) == 1
        assert rows[0].approved_via == 'telegram'
        assert rows[0].consumed_at is not None   # issued + spent atomically


def test_confirm_rejects_unknown_approval_channel(client, tenant_headers,
                                                  auth_headers, app,
                                                  action_registry,
                                                  fake_providers, monkeypatch):
    """A 400 on pure input validation happens BEFORE nonce consumption: it
    neither claims the action nor burns the single-use token."""
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    resp = _confirm(client, auth_headers, action_id,
                    body={'approved_via': 'carrier-pigeon'})
    assert resp.status_code == 400
    assert fake_providers == []
    with app.app_context():
        # Not claimed: the action is still approvable.
        assert db.session.get(ProposedAction,
                              action_id).status == 'awaiting_confirmation'
    # The credential was NOT consumed by the 400 — the SAME token now
    # confirms successfully.
    retry = _confirm(client, auth_headers, action_id,
                     body={'approved_via': 'dashboard'})
    assert retry.status_code == 200
    assert len(fake_providers) == 1


# ---------------------------------------------------------------------------
# step-up nonce consumption at confirm (single-use execution credential)
# ---------------------------------------------------------------------------

def test_same_token_cannot_confirm_twice(client, tenant_headers, auth_headers,
                                         app, action_registry, fake_providers,
                                         monkeypatch):
    """Spec v3: /confirm consumes the step-up token's nonce. One token
    authorizes at most ONE real-world execution — a captured token can't be
    replayed against a second pending action. (Nonce cache is cleared by the
    autouse fixture in tests/actions/conftest.py.)"""
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')

    first_action = _propose(client, tenant_headers)
    second_action = _propose(client, tenant_headers)
    assert _commit(client, auth_headers, first_action).status_code == 202
    assert _commit(client, auth_headers, second_action).status_code == 202

    first = _confirm(client, auth_headers, first_action)
    assert first.status_code == 200
    assert len(fake_providers) == 1

    # Same token against the OTHER awaiting_confirmation action: replay.
    second = _confirm(client, auth_headers, second_action)
    assert second.status_code == 401
    assert 'already used (replay)' in second.get_json()['error']
    assert len(fake_providers) == 1   # nothing executed
    with app.app_context():
        row = db.session.get(ProposedAction, second_action)
        assert row.status == 'awaiting_confirmation'  # still approvable


def test_commit_does_not_consume_token_only_confirm_does(
        client, tenant_headers, auth_headers, action_registry,
        fake_providers, monkeypatch):
    """A token used for commit and then confirm is legitimate: commit
    validates multi-use (submit is not an execution), confirm consumes."""
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')

    # propose -> commit (token X) -> confirm (token X): succeeds end-to-end.
    action_id = _propose(client, tenant_headers)
    assert _commit(client, auth_headers, action_id).status_code == 202
    resp = _confirm(client, auth_headers, action_id)
    assert resp.status_code == 200
    assert len(fake_providers) == 1

    # A NEW action: commit with token X still works (multi-use validation),
    # but confirm with the now-spent token X is a replay -> 401.
    new_action = _propose(client, tenant_headers)
    assert _commit(client, auth_headers, new_action).status_code == 202
    replay = _confirm(client, auth_headers, new_action)
    assert replay.status_code == 401
    assert 'already used (replay)' in replay.get_json()['error']
    assert len(fake_providers) == 1


# ---------------------------------------------------------------------------
# executor result mapping at confirm
# ---------------------------------------------------------------------------

def test_form_fill_without_public_base_url_fails_loud_at_confirm(
        client, tenant_headers, auth_headers, app, action_registry,
        fake_providers, monkeypatch):
    """form-fill has a registered rail (Task 3), but it's a skeleton:
    execute() fails loud instead of pretending when PUBLIC_BASE_URL isn't
    configured — same fail-loud contract as an unregistered kind, now
    routed through the executor itself rather than the no-executor path."""
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    action_id = _propose(client, tenant_headers, body={
        'kind': 'form-fill',
        'payload': {'to': 'Intake portal', 'questionnaire': 'healthclaw-intake',
                    'body': 'demographics form'},
    })
    _commit(client, auth_headers, action_id)
    resp = _confirm(client, auth_headers, action_id)
    assert resp.status_code == 502
    assert resp.get_json()['error_code'] == errors.PROVIDER_NOT_CONFIGURED
    assert fake_providers == []
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'failed'
        assert row.outcome_summary == errors.PROVIDER_NOT_CONFIGURED


def test_needs_review_result_maps_to_needs_review(client, tenant_headers,
                                                  auth_headers, app,
                                                  action_registry,
                                                  monkeypatch):
    from r6.actions.registry import get_executor, ExecutionResult
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    ex = get_executor('phone-call')
    monkeypatch.setattr(ex, 'execute', lambda action: ExecutionResult(
        status='needs_review', outcome={'reason': 'ivr_loop_detected'}))
    resp = _confirm(client, auth_headers, action_id)
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'needs_review'
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'needs_review'
        assert 'ivr_loop_detected' in row.outcome_summary  # evidence kept


def test_executing_result_never_clobbers_fast_webhook(client, tenant_headers,
                                                      auth_headers, app,
                                                      action_registry,
                                                      monkeypatch):
    """Provider accepted -> webhook resolved BEFORE execute() returned. The
    external_ref must still land, and the webhook's verdict must win."""
    from r6.actions.registry import get_executor, ExecutionResult
    from r6.actions.state import transition_action
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    ex = get_executor('phone-call')

    def fast_webhook_execute(action):
        transition_action(action.id, from_states=('executing',),
                          to_state='completed', actor='webhook-test',
                          outcome_summary='resolved by webhook')
        return ExecutionResult(status='executing', provider_ref='ref-999')

    monkeypatch.setattr(ex, 'execute', fast_webhook_execute)
    resp = _confirm(client, auth_headers, action_id)
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'completed'   # authoritative state
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'completed'              # never clobbered
        assert row.external_ref == 'ref-999'          # ref still stored
        assert row.outcome_summary == 'resolved by webhook'


def test_outcome_unknown_maps_to_unknown(client, tenant_headers, auth_headers,
                                         app, action_registry, monkeypatch):
    import requests as req
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')

    def timeout(method, url, **kw):
        raise req.Timeout('slow')

    monkeypatch.setattr('requests.request', timeout)
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    resp = _confirm(client, auth_headers, action_id)
    assert resp.status_code == 502
    with app.app_context():
        # NEVER 'failed' on ambiguity — re-propose could double-place the call
        assert db.session.get(ProposedAction, action_id).status == 'unknown'


# ---------------------------------------------------------------------------
# propose-time gates (red-flag screen + executor validation)
# ---------------------------------------------------------------------------

def test_propose_emergency_text_refused_and_audited(client, tenant_headers,
                                                    app):
    resp = client.post('/r6/actions/propose', json={
        'kind': 'sms',
        'payload': {'to': 'Dr. Smith', 'phone': '617-555-0100',
                    'body': 'Patient reports crushing chest pain since 2am'},
    }, headers=tenant_headers)
    assert resp.status_code == 422
    data = resp.get_json()
    assert data['error_code'] == errors.EMERGENCY_INDICATED
    assert '911' in data['error']
    with app.app_context():
        from r6.models import AuditEventRecord
        events = AuditEventRecord.query.filter_by(
            tenant_id=tenant_headers['X-Tenant-Id'],
            resource_type='ProposedAction', outcome='failure').all()
        assert len(events) == 1
        assert 'emergency_indicated' in events[0].detail
        assert '2am' not in (events[0].detail or '')   # no free text in audit
        row_count = ProposedAction.query.count()
        assert row_count == 0                          # nothing staged


def test_propose_executor_validation_422(client, tenant_headers,
                                         action_registry):
    # phone-call rail requires payload.phone; propose fails fast with the
    # taxonomy code instead of staging a doomed action.
    resp = client.post('/r6/actions/propose', json={
        'kind': 'phone-call',
        'payload': {'to': 'CVS Pharmacy', 'body': 'refill please'},
    }, headers=tenant_headers)
    assert resp.status_code == 422
    data = resp.get_json()
    assert data['error_code'] == errors.PAYLOAD_INVALID
    assert data['error']                            # human-readable message
    assert errors.PAYLOAD_INVALID in data['errors']


def test_rx_transfer_propose_next_step_describes_new_flow(client, auth_headers,
                                                          tenant_headers):
    med = {
        'resourceType': 'MedicationRequest', 'status': 'active',
        'intent': 'order',
        'medicationCodeableConcept': {'text': 'Metformin 500 mg tablet'},
        'subject': {'reference': 'Patient/rx-test-pt'},
    }
    r = client.post('/r6/fhir/MedicationRequest',
                    headers={**auth_headers, 'X-Human-Confirmed': 'true',
                             'Content-Type': 'application/fhir+json'},
                    data=json.dumps(med))
    assert r.status_code == 201
    resp = client.post('/r6/actions/rx-transfer/propose',
                       headers=auth_headers,
                       json={'to_pharmacy': {'name': 'CVS #1234',
                                             'phone': '617-555-0100'}})
    assert resp.status_code == 201
    next_step = resp.get_json()['next_step']
    assert 'X-Human-Confirmed' not in next_step
    assert 'out of band' in next_step or 'approve' in next_step.lower()
