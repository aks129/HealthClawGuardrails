"""#1104 across both layers: the proposer's own review POST records no
approval, and the person's Approve through the real relay still lands.

The engine-only twin is tests/actions/
test_review_write_token_records_no_approval.py; this one drives the real
client onto the real engine, so it proves the relay's review-then-confirm
sequence is accepted, not just made.

Synthetic data only: 555-01xx numbers are fictional.
"""

# ruff: noqa: F811  (pytest fixtures imported, then requested by name)
from __future__ import annotations

import pytest

from tests.test_beta_acceptance_rows import TENANT
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_propose_action_real_engine import (  # noqa: F401
    PHONE, _action, _audit_details, _confirmations, world)

pytestmark = pytest.mark.extension


def test_a_write_token_review_does_not_block_the_persons_approval(
        world, cfg, monkeypatch):
    from careagents import text_self
    chain, c, twilio = world
    monkeypatch.setattr('r6.actions.routes.notify_tenant',
                        lambda tenant, message, **k: 1)
    h = chain.hc._headers(TENANT)
    r = chain.engine.post('/r6/actions/propose', headers=h, json={
        'kind': 'sms',
        'payload': {'to': 'your phone', 'phone': PHONE,
                    # One of the fixed texts: the relay approves no other.
                    'body': text_self.body_for('visit_prep', cfg.origin)}})
    assert r.status_code == 201, r.get_data(as_text=True)
    aid = r.get_json()['id']
    assert chain.engine.post(f'/r6/actions/{aid}/commit',
                             headers=h).status_code == 202

    # The proposer, with the write token it already holds.
    agent = chain.engine.post(f'/r6/actions/{aid}/review', headers=h,
                              json={'ack': 'true'})
    assert agent.status_code == 200, agent.get_data(as_text=True)
    assert _confirmations(chain, aid) == 0
    details = _audit_details(chain)
    # Positive control: the scan below reads the rows this POST wrote.
    assert any(d.startswith('review received') for d in details), details
    assert not any(d.startswith('approved via') for d in details), details
    assert twilio.sent == []

    # The person, through the relay. This answered 409 and never confirmed.
    r = c.post(f'/review/{chain.agent}/{aid}/submit', json={'ack': 'true'})
    assert r.status_code == 200 and r.get_json().get('confirmed') is True
    assert _action(chain, aid)['status'] == 'executing'
    assert _confirmations(chain, aid) == 1


def _require(cond, msg):
    """Reach check. RuntimeError, so the strict xfail below (which expects
    AssertionError) fails on a broken set-up instead of passing as "still
    stale"."""
    if not cond:
        raise RuntimeError(msg)


def _text_awaiting_approval(chain, cfg, monkeypatch):
    from careagents import text_self
    monkeypatch.setattr('r6.actions.routes.notify_tenant',
                        lambda tenant, message, **k: 1)
    h = chain.hc._headers(TENANT)
    r = chain.engine.post('/r6/actions/propose', headers=h, json={
        'kind': 'sms',
        'payload': {'to': 'your phone', 'phone': PHONE,
                    'body': text_self.body_for('visit_prep', cfg.origin)}})
    assert r.status_code == 201, r.get_data(as_text=True)
    aid = r.get_json()['id']
    assert chain.engine.post(f'/r6/actions/{aid}/commit',
                             headers=h).status_code == 202
    return aid


def _approve_with_a_refused_mint(chain, c, aid, monkeypatch):
    """The person's Approve through the relay, with the approval credential
    mint refused by the engine (a wrong internal secret). The review POST
    lands; the confirm never goes out."""
    with monkeypatch.context() as m:
        m.setattr(chain.hc, 'mint_secret', 'not-the-secret')
        return c.post(f'/review/{chain.agent}/{aid}/submit',
                      json={'ack': 'true'})


def test_approving_again_works_after_a_failed_confirm(
        world, cfg, monkeypatch):
    """What the engine holds after the relay's confirm fails (#1104): no
    approval, and a second Approve completes. The relay's sentence for this
    case says the opposite (#1180); the strict xfail below pins that."""
    chain, c, twilio = world
    aid = _text_awaiting_approval(chain, cfg, monkeypatch)
    failed = _approve_with_a_refused_mint(chain, c, aid, monkeypatch)
    assert failed.status_code == 502, failed.get_data(as_text=True)
    assert failed.get_json().get('confirmed') is False
    # Positive control: the review POST reached the engine.
    assert any(d.startswith('review received')
               for d in _audit_details(chain))
    assert _confirmations(chain, aid) == 0
    assert _action(chain, aid)['status'] == 'awaiting_confirmation'
    assert twilio.sent == []

    again = c.post(f'/review/{chain.agent}/{aid}/submit',
                   json={'ack': 'true'})
    assert again.status_code == 200, again.get_data(as_text=True)
    assert again.get_json().get('confirmed') is True
    assert _confirmations(chain, aid) == 1
    assert _action(chain, aid)['status'] == 'executing'


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    '#1180: after a failed confirm the relay still says "your approval is '
    'recorded" and "approving again here will not resend it". Since #1104 '
    'neither is true for a kind other than a form. The in-repo copy of the '
    'consumer app is frozen, so the sentence moves with the caller-side '
    'change (#1116); the marker comes off then.'))
def test_the_failed_confirm_message_matches_what_the_engine_recorded(
        world, cfg, monkeypatch):
    chain, c, twilio = world
    aid = _text_awaiting_approval(chain, cfg, monkeypatch)
    failed = _approve_with_a_refused_mint(chain, c, aid, monkeypatch)
    # Reach proof: the failed-confirm arm answered, for a kind other than a
    # form, and the engine holds no approval for it.
    _require(failed.status_code == 502, 'not the failed-confirm arm')
    _require(failed.get_json().get('confirmed') is False, 'not a refusal')
    _require(_action(chain, aid)['kind'] == 'sms', 'not an sms action')
    _require(_confirmations(chain, aid) == 0, 'an approval is recorded')
    message = (failed.get_json().get('message') or '').lower()
    _require(message, 'the arm answered with no message')
    assert 'approval is recorded' not in message, message
    assert 'will not resend' not in message, message
