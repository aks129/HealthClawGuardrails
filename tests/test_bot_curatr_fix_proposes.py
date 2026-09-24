"""/curatr_fix proposes through the action rail; /approve is not an approval.

The bot's /approve used to call the fix tool with `human_confirmed=True` and
reply "Fix applied." A chat message cannot prove a person read what will
change, and since #413 the fix tool only proposes. Both commands now say
exactly that (#738: /approve was a demo-environment path).

A draft nobody submits is invisible: "Waiting for you" lists only
`awaiting_confirmation`, and the review page refuses anything else. The MCP
path has a model to call action_commit; the bot has none, so /curatr_fix
submits the draft itself. Submitting is not approving — the person still
approves on their own review page, and the chat never reaches that step.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token-for-curatr-fix')
sys.path.insert(0, str(Path(__file__).parent.parent / "openclaw"))

import bot  # noqa: E402

BOT_SOURCE = (Path(__file__).parent.parent / "openclaw" / "bot.py").read_text()


def _update():
    return SimpleNamespace(
        effective_message=SimpleNamespace(text="/curatr_fix"),
        effective_chat=SimpleNamespace(id=5151),
        effective_user=SimpleNamespace(username="advisor", id=99),
        message=SimpleNamespace(text="/curatr_fix"),
    )


PROPOSED = {'id': 'act-77', 'status': 'proposed'}
COMMITTED = {'id': 'act-77', 'status': 'awaiting_confirmation'}


def _run_with(state, results, commands=('curatr_fix', 'approve')):
    """Drive the named commands; `results` maps tool name -> result (or an
    Exception to raise)."""
    bot._chat_state[5151] = dict(state)
    replies = []

    async def fake_reply(update, text, agent_id=None, **kw):
        replies.append(text)

    calls = []

    def fake_rpc(tool, *, headers=None, **params):
        calls.append((tool, params, headers))
        result = results.get(tool, {})
        if isinstance(result, Exception):
            raise result
        return result

    handlers = {'curatr_fix': bot._curatr_fix, 'approve': bot.cmd_approve}
    with patch.object(bot, '_reply', fake_reply), \
            patch.object(bot, '_rpc', fake_rpc), \
            patch.object(bot, '_get_step_up_token', lambda: 'stepup-xyz'), \
            patch.object(bot, '_log_incoming', _async_value('agent-x')):
        for name in commands:
            asyncio.run(handlers[name](_update(), None))
    return calls, replies


def _async_value(value):
    async def _f(*a, **k):
        return value
    return _f


LAST = {'resource_type': 'Condition', 'resource_id': 'c-1',
        'issues': [{'field_path': 'Condition.clinicalStatus.coding[0].code',
                    'suggested_value': 'resolved', 'title': 'Status is stale'}]}
OK = {'curatr_apply_fix': PROPOSED, 'action_commit': COMMITTED}


def test_curatr_fix_proposes_and_says_nothing_changed():
    calls, replies = _run_with({'last_curatr': LAST}, OK)
    tool, params, headers = calls[0]
    assert (tool, params) == ('curatr_apply_fix', {
        'resource_type': 'Condition', 'resource_id': 'c-1',
        'fixes': [{'field_path': 'Condition.clinicalStatus.coding[0].code',
                   'new_value': 'resolved'}],
        'patient_intent': 'requested from Telegram /curatr_fix',
        'reason': 'Status is stale'})
    # Proposing needs no credential; nothing about it is an approval.
    assert headers is None
    assert 'act-77' in replies[0]
    assert 'Nothing has changed' in replies[0]
    assert 'applied' not in replies[0].lower()


def test_curatr_fix_submits_the_draft_so_it_reaches_waiting_for_you():
    calls, replies = _run_with({'last_curatr': LAST}, OK)
    assert [c[0] for c in calls] == ['curatr_apply_fix', 'action_commit']
    _tool, params, headers = calls[1]
    assert params == {'action_id': 'act-77'}
    # The commit route's gate is the tenant WRITE step-up; the bot supplies
    # it the way it does for every other write, bound to its own tenant.
    assert headers == {'X-Step-Up-Token': 'stepup-xyz',
                       'X-Tenant-Id': bot.TENANT_ID}
    assert 'Waiting for you' in replies[0]
    assert 'Nothing has changed' in replies[0]
    assert bot._chat_state[5151]['pending_action'] == 'act-77'


def test_a_failed_submit_is_reported_and_never_called_waiting():
    calls, replies = _run_with(
        {'last_curatr': LAST},
        {'curatr_apply_fix': PROPOSED,
         'action_commit': {'error': 'action_commit failed with status 401'}})
    assert [c[0] for c in calls] == ['curatr_apply_fix', 'action_commit']
    assert 'not submitted' in replies[0].lower()
    assert 'Waiting for you' not in replies[0]
    assert 'pending_action' not in bot._chat_state[5151]
    # /approve must not claim a request is waiting when none is.
    assert 'act-77' not in replies[1]
    assert 'is waiting' not in replies[1]


def test_a_submit_that_raises_is_reported_as_not_submitted():
    calls, replies = _run_with(
        {'last_curatr': LAST},
        {'curatr_apply_fix': PROPOSED,
         'action_commit': RuntimeError('bridge down')},
        commands=('curatr_fix',))
    assert 'not submitted' in replies[0].lower()
    assert 'pending_action' not in bot._chat_state[5151]


def test_a_failed_proposal_is_never_submitted():
    calls, _replies = _run_with(
        {'last_curatr': LAST},
        {'curatr_apply_fix': {'error': 'rail disabled'}},
        commands=('curatr_fix',))
    assert [c[0] for c in calls] == ['curatr_apply_fix']


def test_approve_never_calls_a_tool_and_points_at_waiting_for_you():
    calls, replies = _run_with({'last_curatr': LAST}, OK)
    # Only the proposal and its submission; /approve adds nothing.
    assert [c[0] for c in calls] == ['curatr_apply_fix', 'action_commit']
    assert 'act-77' in replies[1]
    assert 'Waiting for you' in replies[1]
    assert 'applied' not in replies[1].lower()
    # DASHBOARD_BASE_URL is the HealthClaw site, not CareAgents; naming it
    # as the review page sent people to a page with nothing to approve.
    assert bot.DASHBOARD_BASE_URL not in replies[1]


def test_approve_alone_calls_nothing():
    calls, replies = _run_with({}, OK, commands=('approve',))
    assert calls == []
    assert 'Waiting for you' in replies[0]


def test_the_bot_source_no_longer_asserts_human_confirmation():
    assert 'human_confirmed=True' not in BOT_SOURCE
    assert 'Fix applied' not in BOT_SOURCE
    # The chat submits; it never approves or executes.
    assert 'X-Human-Confirmed' not in BOT_SOURCE
    assert 'approval-token' not in BOT_SOURCE
    assert 'action_confirm' not in BOT_SOURCE
