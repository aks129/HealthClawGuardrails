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
    assert not any(d.startswith('approved via')
                   for d in _audit_details(chain))
    assert twilio.sent == []

    # The person, through the relay. This answered 409 and never confirmed.
    r = c.post(f'/review/{chain.agent}/{aid}/submit', json={'ack': 'true'})
    assert r.status_code == 200 and r.get_json().get('confirmed') is True
    assert _action(chain, aid)['status'] == 'executing'
    assert _confirmations(chain, aid) == 1
