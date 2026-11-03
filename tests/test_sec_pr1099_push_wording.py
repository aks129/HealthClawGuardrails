"""Security review of PR #1099 (#1093): exploit tests.

Three findings, each a test that fails on the PR head:

F1 (Low, V5). `_PUSH_SMS_RECIPIENTS` maps the `to` label "patient" to "you".
   Any write-token holder can set that label (a raw propose, the MCP
   server's propose tool), so the push says "a text message to you" for a
   text to somebody else's number. And the only approval surface,
   CareAgents' `_text_not_yours`, refuses every `to` that is not
   "your phone", so it refuses a "patient" text even when the number IS the
   person's, with the not-your-number reason. The push promises an approval
   the relay then refuses.

F2 (Low, V3 UI). `settleDeclined` hides both buttons but leaves the
   attestation box live on the status-read path (an Approve that came back
   404, or a Decline that came back `declined: false`). Re-ticking it runs
   `evaluate()`, which re-enables the hidden Approve and replaces "This
   request was declined" with "Ready. Approve carries out exactly what is
   shown above."; Enter on the box then submits the review. The engine
   refuses that POST (404), so nothing is approved, but the decision is
   re-armed and the card says the opposite of what is true.

F3 (Medium, pre-existing, not in this diff). The engine review POST for a
   non-form kind accepts a plain tenant write token. Whoever holds one (the
   agent that proposed the action) can mint an `approved via review-page`
   ActionConfirmation with no human; the person's later approval through
   CareAgents then gets 409 and `review_submit` never calls confirm.
   Pinned strict-xfail so it does not block #1099.

Synthetic data only: 555-01xx numbers are fictional.
"""

# ruff: noqa: F811  (pytest fixtures imported, then requested by name)
from __future__ import annotations

import pathlib

import pytest

from tests.test_beta_acceptance_rows import TENANT
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_propose_action_real_engine import (  # noqa: F401
    OTHER, PHONE, _action, _confirmations, world)

ROOT = pathlib.Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.extension


def _raw_propose_and_commit(chain, monkeypatch, to, phone):
    """A raw propose + commit, as the MCP server or any token holder can."""
    pushes = []
    monkeypatch.setattr('r6.actions.routes.notify_tenant',
                        lambda tenant, message, **k: pushes.append(message) or 1)
    h = chain.hc._headers(TENANT)
    r = chain.engine.post('/r6/actions/propose', headers=h, json={
        'kind': 'sms',
        'payload': {'to': to, 'phone': phone, 'body': 'CareAgents: synthetic'}})
    assert r.status_code == 201, r.get_data(as_text=True)
    aid = r.get_json()['id']
    c = chain.engine.post(f'/r6/actions/{aid}/commit', headers=h)
    assert c.status_code == 202, c.get_data(as_text=True)
    return aid, pushes


# --- F1 ----------------------------------------------------------------------

def test_f1_push_does_not_claim_the_person_for_another_number(
        world, monkeypatch):
    chain, c, twilio = world
    aid, pushes = _raw_propose_and_commit(chain, monkeypatch, 'patient', OTHER)
    # The relay refuses it (good), so the push's claim cannot be acted on...
    r = c.post(f'/review/{chain.agent}/{aid}/submit', json={'ack': 'true'})
    assert r.status_code == 409 and r.get_json()['error'] == 'not_your_number'
    assert twilio.sent == [] and _confirmations(chain, aid) == 0
    # ...but the push told the person the text was going to them.
    assert pushes, 'commit sent no approval push'
    assert not any('to you' in m for m in pushes), pushes


def test_f1_push_promises_an_approval_the_relay_refuses(world, monkeypatch):
    """The genuine SMBP shape: to='patient' and the person's OWN number."""
    chain, c, twilio = world
    aid, pushes = _raw_propose_and_commit(chain, monkeypatch, 'patient', PHONE)
    r = c.post(f'/review/{chain.agent}/{aid}/submit', json={'ack': 'true'})
    refused = r.status_code == 409
    told_to_you = any('to you' in m for m in pushes)
    assert not (refused and told_to_you), (
        'push said %r; relay answered %s %s' % (
            pushes, r.status_code, r.get_json()))


# --- F2 ----------------------------------------------------------------------

def _settle_body() -> str:
    js = (ROOT / 'templates' / '_review_handlers.html').read_text(
        encoding='utf-8')
    start = js.index('function settleDeclined()')
    return js[start:js.index('\n    }\n', start)]


def test_f2_settling_a_decline_leaves_no_input_to_rearm_approve():
    """Reproduced in Chromium on the real page (relay mock): Approve -> 404
    -> status read 'declined' -> settle; re-tick the box -> Approve
    disabled=false (hidden), message 'Ready. Approve carries out exactly
    what is shown above.'; Enter on the box -> a second review POST.
    The click path disables the form's inputs before it settles; the
    status-read path does not. Settling must, on both."""
    body = _settle_body()
    assert "querySelectorAll('input')" in body or 'settled' in body, body


# --- F3 (pre-existing) -------------------------------------------------------

@pytest.mark.xfail(strict=True, reason=(
    'pre-existing, not #1099: the engine review POST accepts a plain '
    'tenant write token, so the proposer can mint its own consent record'))
def test_f3_a_write_token_alone_cannot_record_an_approval(world, monkeypatch):
    chain, c, twilio = world
    aid, _ = _raw_propose_and_commit(chain, monkeypatch, 'your phone', PHONE)
    agent = chain.engine.post(f'/r6/actions/{aid}/review',
                              headers=chain.hc._headers(TENANT),
                              json={'ack': 'true'})
    assert agent.status_code in (401, 403), agent.get_data(as_text=True)
    assert _confirmations(chain, aid) == 0
    # And the person can still approve afterwards.
    r = c.post(f'/review/{chain.agent}/{aid}/submit', json={'ack': 'true'})
    assert r.status_code == 200 and r.get_json().get('confirmed') is True
    assert _action(chain, aid)['status'] == 'executing'
