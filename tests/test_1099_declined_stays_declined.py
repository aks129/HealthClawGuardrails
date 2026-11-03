"""#1099 QA: a declined request reads as declined after a reload, and a
Decline from a stale tab settles instead of reporting an outage.

On the real engine through the real HealthClaw client (the Chain harness):
the engine's decline, its approval-token mint refusing a request that is no
longer awaiting an answer (409), and its status read. Only Twilio is faked.

Synthetic data only: 555-01xx numbers are fictional.
"""

# ruff: noqa: F811  (pytest fixtures imported, then requested by name)
from __future__ import annotations

import pytest

from careagents import text_self
from tests.test_beta_acceptance_rows import TENANT
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_propose_action_real_engine import (  # noqa: F401
    PHONE, _action, world)

pytestmark = pytest.mark.extension

DECLINED = 'This request was declined and will not be carried out.'


def _staged(chain, cfg, monkeypatch):
    """An sms the relay would approve: the account's number, the fixed
    label and one of the fixed texts."""
    body = text_self.body_for(next(iter(text_self.PURPOSES)),
                              cfg.origin)
    monkeypatch.setattr('r6.actions.routes.notify_tenant',
                        lambda tenant, message, **k: 1)
    h = chain.hc._headers(TENANT)
    r = chain.engine.post('/r6/actions/propose', headers=h, json={
        'kind': 'sms',
        'payload': {'to': 'your phone', 'phone': PHONE,
                    'body': body}})
    assert r.status_code == 201, r.get_data(as_text=True)
    aid = r.get_json()['id']
    c = chain.engine.post(f'/r6/actions/{aid}/commit', headers=h)
    assert c.status_code == 202, c.get_data(as_text=True)
    return aid


def _declined(chain, c, cfg, monkeypatch):
    aid = _staged(chain, cfg, monkeypatch)
    r = c.post(f'/review/{chain.agent}/{aid}/decline', json={})
    assert r.status_code == 200 and r.get_json()['declined'] is True, (
        r.status_code, r.get_json())
    assert _action(chain, aid)['status'] == 'declined'
    return aid


def test_a_reload_after_a_decline_says_it_was_declined(
        world, cfg, monkeypatch):
    """MUTATION: in _form_past_review, drop the 'declined' state -> red
    (the page reads "This request is already done.")."""
    chain, c, twilio = world
    aid = _declined(chain, c, cfg, monkeypatch)
    page = c.get(f'/review/{chain.agent}/{aid}')
    html = page.get_data(as_text=True)
    assert page.status_code == 200, html
    assert DECLINED in html, html
    assert 'already done' not in html, html
    assert twilio.sent == []


def test_a_decline_from_a_stale_tab_settles(world, cfg, monkeypatch):
    """The first tab declined; the second, still showing the card, declines
    too. The mint refuses (409, no longer awaiting an answer), and that is
    the answer the person wanted, not an outage.

    MUTATION: in HealthClawClient.decline_action, drop the 409 status read
    -> red (503 "We couldn't record your answer")."""
    chain, c, twilio = world
    aid = _declined(chain, c, cfg, monkeypatch)
    again = c.post(f'/review/{chain.agent}/{aid}/decline', json={})
    assert again.status_code == 200, (again.status_code, again.get_json())
    assert again.get_json()['declined'] is True
    assert _action(chain, aid)['status'] == 'declined'
    assert twilio.sent == []


def test_a_stale_decline_of_an_approved_request_is_not_a_decline(
        world, cfg, monkeypatch):
    """The 409 read settles only a request that IS declined. One that was
    approved elsewhere is never reported as declined."""
    chain, c, twilio = world
    aid = _staged(chain, cfg, monkeypatch)
    ok = c.post(f'/review/{chain.agent}/{aid}/submit', json={'ack': 'true'})
    assert ok.status_code == 200, (ok.status_code, ok.get_json())
    assert _action(chain, aid)['status'] != 'awaiting_confirmation'
    late = c.post(f'/review/{chain.agent}/{aid}/decline', json={})
    assert late.get_json().get('declined') is not True, late.get_json()
