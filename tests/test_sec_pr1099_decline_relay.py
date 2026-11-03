"""Security re-verify of PR #1099: the 409 status read in decline_action.

`HealthClawClient.decline_action` now reads the action's status when the
approval-token mint answers 409, and reports a decline when the engine says
`declined`. These try to make it report a decline that did not happen, or
answer about another tenant's action. On the real engine through the real
client (the Chain harness). Synthetic data only.
"""

# ruff: noqa: F811  (pytest fixtures imported, then requested by name)
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.test_beta_acceptance_rows import TENANT
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_propose_action_real_engine import (  # noqa: F401
    OTHER, PHONE, _action, world)

pytestmark = pytest.mark.extension

FOREIGN = 'sec1099-foreign-tenant'


def _stage(chain, monkeypatch, tenant=TENANT, commit=True):
    monkeypatch.setattr('r6.actions.routes.notify_tenant',
                        lambda tenant, message, **k: 1)
    h = chain.hc._headers(tenant)
    r = chain.engine.post('/r6/actions/propose', headers=h, json={
        'kind': 'sms', 'payload': {'to': 'your phone', 'phone': PHONE,
                                   'body': 'CareAgents: synthetic'}})
    assert r.status_code == 201, r.get_data(as_text=True)
    aid = r.get_json()['id']
    if commit:
        c = chain.engine.post(f'/r6/actions/{aid}/commit', headers=h)
        assert c.status_code == 202, c.get_data(as_text=True)
    return aid


def _engine_decline(chain, tenant, aid):
    """Decline on the engine directly, as the relay's own credential does."""
    secret = chain.hc.mint_secret
    tok = chain.engine.post(
        f'/r6/actions/{aid}/approval-token',
        headers={'X-Tenant-Id': tenant, 'X-Internal-Secret': secret})
    assert tok.status_code == 200, tok.get_data(as_text=True)
    d = chain.engine.post(
        f'/r6/actions/{aid}/decline',
        headers={'X-Tenant-Id': tenant,
                 'X-Step-Up-Token': tok.get_json()['token']},
        json={'declined_via': 'review-page'})
    assert d.status_code == 200, d.get_data(as_text=True)


def test_another_tenants_declined_action_is_not_answered(world, monkeypatch):
    chain, c, _ = world
    aid = _stage(chain, monkeypatch, tenant=FOREIGN)
    _engine_decline(chain, FOREIGN, aid)
    r = c.post(f'/review/{chain.agent}/{aid}/decline', json={})
    assert r.status_code == 404, (r.status_code, r.get_json())
    assert r.get_json() == {'error': 'not yours'}


def test_a_lapsed_request_is_not_reported_as_declined(world, monkeypatch):
    chain, c, _ = world
    aid = _stage(chain, monkeypatch)
    from models import db
    from r6.actions.models import ProposedAction
    with chain.engine_app.app_context():
        row = db.session.get(ProposedAction, aid)
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
    first = c.post(f'/review/{chain.agent}/{aid}/decline', json={})
    assert first.get_json().get('declined') is not True, first.get_json()
    second = c.post(f'/review/{chain.agent}/{aid}/decline', json={})
    assert second.get_json().get('declined') is not True, second.get_json()
    # One outcome: the mint answers 410 (window lapsed) and nothing on the
    # decline path flips the row, so it stays awaiting_confirmation until
    # the reaper expires it. Checked against the engine 2026-11-03.
    assert _action(chain, aid)['status'] == 'awaiting_confirmation'


def test_an_uncommitted_request_is_not_reported_as_declined(
        world, monkeypatch):
    chain, c, _ = world
    aid = _stage(chain, monkeypatch, commit=False)
    r = c.post(f'/review/{chain.agent}/{aid}/decline', json={})
    assert r.get_json().get('declined') is not True, r.get_json()
    assert _action(chain, aid)['status'] == 'proposed'


@pytest.mark.parametrize('answer', [
    {'status': 'DECLINED'}, {'status': ' declined'}, {'status': ['declined']},
    {'state': 'declined'}, {},
])
def test_only_the_exact_engine_word_counts(world, monkeypatch, answer):
    """A 409 mint plus a status body that is not exactly `declined`."""
    chain, c, _ = world
    aid = _stage(chain, monkeypatch)
    _engine_decline(chain, TENANT, aid)   # so the mint answers 409
    real = chain.hc.action_status
    calls = []

    def fake(tenant, action_id):
        calls.append((tenant, action_id))
        if len(calls) == 1:          # the ownership read in the relay
            return real(tenant, action_id)
        return answer
    monkeypatch.setattr(chain.hc, 'action_status', fake)
    r = c.post(f'/review/{chain.agent}/{aid}/decline', json={})
    assert r.get_json().get('declined') is not True, r.get_json()
    assert len(calls) >= 2, 'the 409 status read never ran'
    assert all(t == TENANT and a == aid for t, a in calls), calls


def test_a_failed_status_read_is_not_a_decline(world, monkeypatch):
    chain, c, _ = world
    aid = _stage(chain, monkeypatch)
    _engine_decline(chain, TENANT, aid)
    from careagents.healthclaw import HealthClawError
    real = chain.hc.action_status
    calls = []

    def fake(tenant, action_id):
        calls.append(1)
        if len(calls) == 1:
            return real(tenant, action_id)
        raise HealthClawError('synthetic outage', 503)
    monkeypatch.setattr(chain.hc, 'action_status', fake)
    r = c.post(f'/review/{chain.agent}/{aid}/decline', json={})
    assert r.status_code == 503, (r.status_code, r.get_json())
    assert r.get_json().get('declined') is None
