"""POST /r6/ops/reap — external-tick ops reaper (spec: Durable execution).

Branches under test:
  executing + external_ref + stale updated_at      -> reconcile() mapping
  executing + no ref + no provider_request_at,
      claimed_at stale                             -> failed (never called)
  executing + no ref + provider_request_at stale   -> needs_review, no retry
  unknown  + external_ref                          -> reconcile() mapping
  awaiting_confirmation + expires_at past          -> expired + notify
Plus: per-action exception containment, audit/ledger writes, auth.
"""
from datetime import timedelta

import pytest

from models import db
from r6.actions.models import ProposedAction, _utcnow
from r6.actions.registry import ExecutionResult

STALE = timedelta(minutes=10)   # comfortably past the 5-minute threshold


def make_action(status='executing', age=STALE, external_ref=None,
                claimed_at=None, provider_request_at=None, expires_at=None,
                tenant_id='test-tenant', kind='sms'):
    """Fabricate a row in an arbitrary lifecycle state. Timestamps are set
    via a direct UPDATE so onupdate can't re-stamp updated_at."""
    action = ProposedAction(tenant_id=tenant_id, kind=kind,
                            payload={'body': 'hello', 'to': 'CVS Pharmacy'})
    db.session.add(action)
    db.session.commit()
    then = _utcnow() - age
    updates = {'status': status, 'updated_at': then}
    if external_ref is not None:
        updates['external_ref'] = external_ref
    if claimed_at is not None:
        updates['claimed_at'] = claimed_at
    if provider_request_at is not None:
        updates['provider_request_at'] = provider_request_at
    if expires_at is not None:
        updates['expires_at'] = expires_at
    ProposedAction.query.filter_by(id=action.id).update(
        updates, synchronize_session=False)
    db.session.commit()
    return action.id


def get_action(action_id):
    db.session.expire_all()
    return ProposedAction.query.filter_by(id=action_id).first()


class FakeExecutor:
    """Registry stand-in whose reconcile() returns a canned result (or
    raises). execute() asserts the reaper NEVER executes."""
    kind = 'sms'
    required_env = ()

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.reconciled = []

    def validate(self, payload):
        return []

    def execute(self, action):
        raise AssertionError('the reaper must never call execute()')

    def reconcile(self, action):
        self.reconciled.append(action.id)
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture
def patch_executor(monkeypatch):
    """Route r6.ops.routes.get_executor at a FakeExecutor."""
    def _install(result=None, exc=None):
        fake = FakeExecutor(result=result, exc=exc)
        monkeypatch.setattr('r6.ops.routes.get_executor',
                            lambda kind: fake)
        return fake
    return _install


@pytest.fixture
def notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(
        'r6.ops.routes.notify_tenant',
        lambda tenant_id, message, **kw: sent.append(
            {'tenant_id': tenant_id, 'message': message}) or 1)
    return sent


def reap(client, auth_headers):
    resp = client.post('/r6/ops/reap', headers=auth_headers)
    assert resp.status_code == 200
    return resp.get_json()


# ------------------------------------------------------------------ auth

class TestReapAuth:
    def test_requires_tenant_header(self, client):
        assert client.post('/r6/ops/reap').status_code == 400

    def test_requires_step_up_token(self, client, tenant_headers):
        resp = client.post('/r6/ops/reap', headers=tenant_headers)
        assert resp.status_code == 401

    def test_rejects_bad_token(self, client, tenant_id):
        resp = client.post('/r6/ops/reap', headers={
            'X-Tenant-Id': tenant_id, 'X-Step-Up-Token': 'garbage.token'})
        assert resp.status_code == 401


# ------------------------------------------- executing + ref -> reconcile

class TestReconcileExecutingWithRef:
    def test_completed(self, app, client, auth_headers, patch_executor):
        fake = patch_executor(ExecutionResult(
            status='completed', provider_ref='call-99',
            outcome={'transcript': 'done'}))
        action_id = make_action(external_ref='call-42')
        body = reap(client, auth_headers)
        assert body['swept'] == 1
        assert body['transitions'] == [
            {'id': action_id, 'from': 'executing', 'to': 'completed'}]
        row = get_action(action_id)
        assert row.status == 'completed'
        assert 'transcript' in row.outcome_summary
        assert fake.reconciled == [action_id]

    def test_failed(self, app, client, auth_headers, patch_executor):
        patch_executor(ExecutionResult(status='failed',
                                       error='provider_error'))
        action_id = make_action(external_ref='call-42')
        body = reap(client, auth_headers)
        row = get_action(action_id)
        assert row.status == 'failed'
        assert 'provider_error' in row.outcome_summary
        assert body['swept'] == 1

    def test_still_executing_leaves_row_alone(self, app, client,
                                              auth_headers, patch_executor):
        fake = patch_executor(ExecutionResult(status='executing',
                                              provider_ref='call-42'))
        action_id = make_action(external_ref='call-42')
        body = reap(client, auth_headers)
        assert body['swept'] == 0
        assert body['transitions'] == []
        assert get_action(action_id).status == 'executing'
        assert fake.reconciled == [action_id]   # it DID ask the provider

    def test_needs_review_carries_evidence(self, app, client, auth_headers,
                                           patch_executor):
        patch_executor(ExecutionResult(
            status='needs_review',
            outcome={'evidence': 'clinic asked patient to call directly'}))
        action_id = make_action(external_ref='call-42')
        reap(client, auth_headers)
        row = get_action(action_id)
        assert row.status == 'needs_review'
        assert 'clinic asked patient to call directly' in row.outcome_summary

    def test_fresh_row_is_not_reconciled(self, app, client, auth_headers,
                                         patch_executor):
        fake = patch_executor(ExecutionResult(status='completed'))
        action_id = make_action(external_ref='call-42',
                                age=timedelta(minutes=1))
        body = reap(client, auth_headers)
        assert body['swept'] == 0
        assert fake.reconciled == []
        assert get_action(action_id).status == 'executing'


# ------------------------------------- executing without ref (crash forensics)

class TestExecutingWithoutRef:
    def test_never_called_provider_fails_safely(self, app, client,
                                                auth_headers, patch_executor):
        fake = patch_executor(ExecutionResult(status='completed'))
        action_id = make_action(claimed_at=_utcnow() - STALE)
        body = reap(client, auth_headers)
        row = get_action(action_id)
        assert row.status == 'failed'
        assert body['transitions'] == [
            {'id': action_id, 'from': 'executing', 'to': 'failed'}]
        assert fake.reconciled == []    # nothing to reconcile against

    def test_fresh_claim_is_left_alone(self, app, client, auth_headers,
                                       patch_executor):
        patch_executor(ExecutionResult(status='completed'))
        action_id = make_action(claimed_at=_utcnow() - timedelta(minutes=1),
                                age=timedelta(minutes=1))
        body = reap(client, auth_headers)
        assert body['swept'] == 0
        assert get_action(action_id).status == 'executing'

    def test_provider_may_have_acted_goes_to_review(self, app, client,
                                                    auth_headers,
                                                    patch_executor):
        fake = patch_executor(ExecutionResult(status='completed'))
        action_id = make_action(
            claimed_at=_utcnow() - STALE,
            provider_request_at=_utcnow() - STALE)
        body = reap(client, auth_headers)
        row = get_action(action_id)
        assert row.status == 'needs_review'     # NEVER auto-retried
        assert 'provider may have acted' in row.outcome_summary
        assert fake.reconciled == []
        assert body['transitions'] == [
            {'id': action_id, 'from': 'executing', 'to': 'needs_review'}]

    def test_fresh_provider_request_is_left_alone(self, app, client,
                                                  auth_headers,
                                                  patch_executor):
        patch_executor(ExecutionResult(status='completed'))
        action_id = make_action(
            claimed_at=_utcnow() - timedelta(minutes=1),
            provider_request_at=_utcnow() - timedelta(minutes=1),
            age=timedelta(minutes=1))
        body = reap(client, auth_headers)
        assert body['swept'] == 0
        assert get_action(action_id).status == 'executing'


# --------------------------------------------- unknown + ref -> reconcile

class TestReconcileUnknown:
    def test_unknown_resolves_to_completed(self, app, client, auth_headers,
                                           patch_executor):
        patch_executor(ExecutionResult(status='completed',
                                       outcome={'ok': True}))
        action_id = make_action(status='unknown', external_ref='call-42')
        body = reap(client, auth_headers)
        assert get_action(action_id).status == 'completed'
        assert body['transitions'] == [
            {'id': action_id, 'from': 'unknown', 'to': 'completed'}]

    def test_unknown_resolves_to_failed(self, app, client, auth_headers,
                                        patch_executor):
        patch_executor(ExecutionResult(status='failed', error='no-answer'))
        action_id = make_action(status='unknown', external_ref='call-42')
        reap(client, auth_headers)
        assert get_action(action_id).status == 'failed'

    def test_unknown_resolves_to_needs_review(self, app, client,
                                              auth_headers, patch_executor):
        patch_executor(ExecutionResult(status='needs_review',
                                       outcome={'evidence': 'ambiguous'}))
        action_id = make_action(status='unknown', external_ref='call-42')
        reap(client, auth_headers)
        row = get_action(action_id)
        assert row.status == 'needs_review'
        assert 'ambiguous' in row.outcome_summary

    def test_unknown_still_executing_stays_unknown(self, app, client,
                                                   auth_headers,
                                                   patch_executor):
        patch_executor(ExecutionResult(status='executing'))
        action_id = make_action(status='unknown', external_ref='call-42')
        body = reap(client, auth_headers)
        assert body['swept'] == 0
        assert get_action(action_id).status == 'unknown'

    def test_unknown_without_ref_is_not_touched(self, app, client,
                                                auth_headers,
                                                patch_executor):
        fake = patch_executor(ExecutionResult(status='completed'))
        action_id = make_action(status='unknown')
        body = reap(client, auth_headers)
        assert body['swept'] == 0
        assert fake.reconciled == []
        assert get_action(action_id).status == 'unknown'


# --------------------------------------- awaiting_confirmation expiry sweep

class TestExpirySweep:
    def test_lapsed_approval_expires_and_notifies(self, app, client,
                                                  auth_headers,
                                                  notifications):
        action_id = make_action(status='awaiting_confirmation',
                                expires_at=_utcnow() - timedelta(minutes=1))
        body = reap(client, auth_headers)
        row = get_action(action_id)
        assert row.status == 'expired'
        assert body['transitions'] == [
            {'id': action_id, 'from': 'awaiting_confirmation',
             'to': 'expired'}]
        assert len(notifications) == 1
        note = notifications[0]
        assert note['tenant_id'] == 'test-tenant'
        assert 'lapsed' in note['message']
        assert 'propose' in note['message']
        # summary-only: the recipient label may appear, the body must not
        assert 'hello' not in note['message']

    def test_unexpired_approval_is_left_alone(self, app, client,
                                              auth_headers, notifications):
        action_id = make_action(status='awaiting_confirmation',
                                expires_at=_utcnow() + timedelta(minutes=10))
        body = reap(client, auth_headers)
        assert body['swept'] == 0
        assert get_action(action_id).status == 'awaiting_confirmation'
        assert notifications == []


# --------------------------------------------------- containment + ledger

class TestContainment:
    def test_reconcile_exception_skips_row_and_sweep_continues(
            self, app, client, auth_headers, monkeypatch, notifications):
        bad_id = make_action(external_ref='call-bad')
        never_called_id = make_action(claimed_at=_utcnow() - STALE)
        lapsed_id = make_action(status='awaiting_confirmation',
                                expires_at=_utcnow() - timedelta(minutes=1))

        fake = FakeExecutor(exc=RuntimeError('provider API down'))
        monkeypatch.setattr('r6.ops.routes.get_executor', lambda kind: fake)

        body = reap(client, auth_headers)

        assert get_action(bad_id).status == 'executing'      # skipped
        assert get_action(never_called_id).status == 'failed'
        assert get_action(lapsed_id).status == 'expired'
        assert body['swept'] == 2
        moved = {t['id'] for t in body['transitions']}
        assert moved == {never_called_id, lapsed_id}

    def test_missing_executor_is_contained(self, app, client, auth_headers,
                                           monkeypatch):
        action_id = make_action(external_ref='call-42')
        other_id = make_action(claimed_at=_utcnow() - STALE)
        monkeypatch.setattr('r6.ops.routes.get_executor', lambda kind: None)
        body = reap(client, auth_headers)
        assert get_action(action_id).status == 'executing'
        assert get_action(other_id).status == 'failed'
        assert body['swept'] == 1


class TestReaperLedger:
    def test_transitions_land_in_action_events_as_reaper(
            self, app, client, auth_headers, patch_executor):
        from r6.actions.events import ActionEvent
        patch_executor(ExecutionResult(status='completed'))
        action_id = make_action(external_ref='call-42')
        reap(client, auth_headers)
        events = ActionEvent.query.filter_by(action_id=action_id,
                                             actor='reaper').all()
        assert len(events) == 1
        assert events[0].to_status == 'completed'

    def test_reap_feeds_the_preflight_heartbeat(self, app, client,
                                                auth_headers,
                                                patch_executor):
        from r6.ops import checks
        patch_executor(ExecutionResult(status='completed'))
        make_action(external_ref='call-42')
        reap(client, auth_headers)
        heartbeat = checks.check_reaper_heartbeat()
        assert heartbeat['ok'] is True


class TestEmptySweep:
    def test_nothing_to_do(self, app, client, auth_headers):
        body = reap(client, auth_headers)
        assert body == {'swept': 0, 'transitions': []}
