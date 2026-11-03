"""QA on #1099 (#1093): the push's kind names, pinned in both directions.

tests/test_1093_plain_push_wording.py walks careagents' _KIND_LABELS and
looks each kind up in the push's names. That catches a renamed label and a
kind CareAgents adds, but not one it drops: the push would keep a name for a
kind the approvals page then shows as "Request". This pins the three key
sets to each other, and runs every kind through the real summary() so the
`to` each sentence is chosen by is the one an audit row carries.

Synthetic data only: 555-01xx numbers are fictional, the name is a canary.
"""

from __future__ import annotations

import pytest

from r6.actions.models import VALID_KINDS, ProposedAction
from r6.actions.routes import _PUSH_KIND_NAMES, _approval_push

CANARY = 'Zelda Canaryfield 617-555-0142'


def test_the_push_and_the_page_name_the_same_kinds():
    """MUTATION: drop any entry from careagents/app.py _KIND_LABELS, or add
    a kind to VALID_KINDS that the page has no label for -> red."""
    from careagents.app import _KIND_LABELS
    assert set(_KIND_LABELS) == set(_PUSH_KIND_NAMES) == set(VALID_KINDS)


@pytest.mark.parametrize('kind,to,sentence', [
    ('phone-call', CANARY, 'Approval needed: a phone call.'),
    # A text names no recipient, whatever the label (#1099 security F1).
    ('sms', 'your phone', 'Approval needed: a text message.'),
    ('sms', 'patient', 'Approval needed: a text message.'),
    ('sms', CANARY, 'Approval needed: a text message.'),
    ('sms', 'Patient', 'Approval needed: a text message.'),
    ('sms', 'patient ', 'Approval needed: a text message.'),
    ('insurance-call', CANARY, 'Approval needed: a call to your insurer.'),
    ('form-fill', CANARY, 'Approval needed: an intake form.'),
    ('webhook-poster', CANARY,
     'Approval needed: a message to a connected service.'),
    ('curatr-fix', CANARY,
     'Approval needed: a correction to your health record.'),
])
def test_each_sentence_through_the_real_summary(kind, to, sentence):
    action = ProposedAction('t-1093-qa', kind,
                            {'to': to, 'phone': '+15555550142',
                             'body': 'synthetic'})
    push = _approval_push(action.summary())
    assert push.startswith('\U0001f514 ' + sentence + ' '), push
    assert 'Zelda' not in push and '555-0142' not in push, push
    assert kind not in push, push


def test_patient_is_you_only_for_a_text():
    """'patient' is in SAFE_TO_LABELS for sms only: a call staged with it
    must not be announced as going to "you"."""
    for kind in VALID_KINDS:
        if kind == 'sms':
            continue
        action = ProposedAction('t-1093-qa', kind,
                                {'to': 'patient', 'body': 'synthetic'})
        push = _approval_push(action.summary())
        assert ' to you.' not in push and 'patient' not in push, (kind, push)
