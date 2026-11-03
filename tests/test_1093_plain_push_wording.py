"""#1093: the approval push and the declined review card, in plain words.

The push printed the engine's kind and the fixed recipient label ("form-fill
to a form"), and named a section ("Waiting for you") that the page it sends
people to calls "Waiting for your OK". After a Decline the card still read
"awaiting your answer" beside a disabled Approve.

Synthetic data only: 555-01xx numbers are fictional, the name is a canary.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from r6.actions.models import VALID_KINDS, ProposedAction
from r6.actions.routes import (APPROVALS_SECTION, _PUSH_KIND_NAMES,
                               _approval_push)

ROOT = pathlib.Path(__file__).resolve().parents[1]
CANARY = 'Zelda Canaryfield 617-555-0142'

#: The sentence each kind's push opens with (summary()['to'] as staged).
EXPECTED = {
    ('phone-call', 'a phone number'):
        'Approval needed: a phone call.',
    # No recipient for a text, whatever the label (#1099 security F1).
    ('sms', 'your phone'):
        'Approval needed: a text message.',
    ('sms', 'patient'):
        'Approval needed: a text message.',
    ('sms', 'a phone number'):
        'Approval needed: a text message.',
    ('insurance-call', 'an insurer'):
        'Approval needed: a call to your insurer.',
    ('form-fill', 'a form'):
        'Approval needed: an intake form.',
    ('webhook-poster', 'a webhook'):
        'Approval needed: a message to a connected service.',
    ('curatr-fix', 'your record'):
        'Approval needed: a correction to your health record.',
}


@pytest.mark.parametrize('kind,to', sorted(EXPECTED))
def test_each_kind_has_one_plain_sentence(kind, to):
    push = _approval_push({'kind': kind, 'to': to})
    assert EXPECTED[(kind, to)] in push, push
    assert kind not in push, push


def test_every_kind_has_a_sentence():
    assert set(_PUSH_KIND_NAMES) == set(VALID_KINDS)


def test_an_unknown_kind_gets_a_generic_sentence():
    push = _approval_push({'kind': 'teleport', 'to': CANARY})
    assert 'Approval needed: a request from your assistant.' in push, push
    assert 'teleport' not in push and 'Zelda' not in push, push


def test_the_push_names_match_the_approvals_page():
    """Retyped rather than imported (the engine never loads careagents):
    this keeps the push's names and the page's from drifting apart."""
    from careagents.app import _KIND_LABELS
    for kind, label in _KIND_LABELS.items():
        article = 'an' if label[0].lower() in 'aeiou' else 'a'
        assert _PUSH_KIND_NAMES[kind] == '%s %s' % (article, label.lower())


def test_the_push_section_is_the_page_title():
    page = (ROOT / 'careagents' / 'templates' / 'approvals.html').read_text(
        encoding='utf-8')
    assert '<h1>%s</h1>' % APPROVALS_SECTION in page
    push = _approval_push({'kind': 'sms', 'to': 'your phone'})
    assert 'under "%s"' % APPROVALS_SECTION in push, push


def test_the_smbp_label_passes_through_summary():
    from r6.smbp.outreach import reminder_action
    staged = reminder_action('Patient/x', '+15555550142', 'en', 0, 7)
    action = ProposedAction('t-1093', staged['kind'], staged['payload'])
    assert action.summary()['to'] == 'patient'


@pytest.mark.parametrize('kind,extra', [
    ('sms', {'phone': '+15555550142', 'body': 'synthetic reminder'}),
    ('phone-call', {'phone': '+15555550142', 'body': 'synthetic script'}),
])
def test_a_canary_in_to_never_reaches_the_push(
        client, tenant_headers, monkeypatch, kind, extra):
    """MUTATION: in commit_action, send
    '... %s to %s' % (action.kind, action.payload.get('to')) as the push
    instead of _approval_push(action.summary()). This goes red."""
    from r6.stepup import generate_step_up_token
    sent = []
    monkeypatch.setattr('r6.actions.routes.notify_tenant',
                        lambda tenant, message, **k: sent.append(message) or 1)
    tenant = tenant_headers['X-Tenant-Id']
    h = dict(tenant_headers, **{
        'X-Step-Up-Token': generate_step_up_token(tenant)})
    body = {'kind': kind, 'payload': dict(extra, to=CANARY)}
    resp = client.post('/r6/actions/propose', json=body, headers=h)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    action_id = resp.get_json()['id']
    resp = client.post(f'/r6/actions/{action_id}/commit', headers=h)
    assert resp.status_code == 202, resp.get_data(as_text=True)

    assert sent, 'commit sent no approval push'
    blob = json.dumps(sent)
    assert 'Zelda' not in blob and '555-0142' not in blob, sent
    assert 'synthetic' not in blob, sent
    assert all('Approval needed: ' in m for m in sent), sent


# --- the declined card --------------------------------------------------------

TEMPLATES = ROOT / 'templates'


def _handlers() -> str:
    return (TEMPLATES / '_review_handlers.html').read_text(encoding='utf-8')


def _function(js: str, name: str) -> str:
    start = js.index('function %s()' % name)
    return js[start:js.index('\n    }\n', start)]


def test_the_card_has_a_state_badge_to_settle():
    page = (TEMPLATES / 'action_approve.html').read_text(encoding='utf-8')
    assert re.search(r'id="review-state"[^>]*>[^<]*awaiting your answer<',
                     page), 'the badge the decline settles is gone'


def test_a_decline_settles_the_card():
    """MUTATION: drop either hide line, or the badge text, from
    settleDeclined -> red."""
    body = _function(_handlers(), 'settleDeclined')
    assert 'btn.hidden = true' in body
    assert 'declineBtn.hidden = true' in body
    assert "getElementById('review-state')" in body
    assert 'awaiting' not in body and '· declined' in body


def test_both_declined_branches_settle_the_card():
    """The click's own `declined: true`, and a status read that says
    declined (a decline recorded on another screen)."""
    js = _handlers()
    click = js[js.index("declineBtn.addEventListener('click'"):]
    ok = click[click.index('res.b.declined === true'):]
    assert ok.index('settleDeclined();') < ok.index('return;')
    poll = js[js.index("if (status === 'declined')"):]
    assert poll.index('settleDeclined();') < poll.index('return;')
