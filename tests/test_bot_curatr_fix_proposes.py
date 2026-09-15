"""/curatr_fix proposes through the action rail; /approve is not an approval.

The bot's /approve used to call the fix tool with `human_confirmed=True` and
reply "Fix applied." A chat message cannot prove a person read what will
change, and since #413 the fix tool only proposes. Both commands now say
exactly that (#738: /approve was a demo-environment path).
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


def _run_with(state, rpc_result):
    bot._chat_state[5151] = dict(state)
    replies = []

    async def fake_reply(update, text, agent_id=None, **kw):
        replies.append(text)

    calls = []

    def fake_rpc(tool, **params):
        calls.append((tool, params))
        return rpc_result

    with patch.object(bot, '_reply', fake_reply), \
            patch.object(bot, '_rpc', fake_rpc), \
            patch.object(bot, '_log_incoming', _async_value('agent-x')):
        asyncio.run(bot._curatr_fix(_update(), None))
        asyncio.run(bot.cmd_approve(_update(), None))
    return calls, replies


def _async_value(value):
    async def _f(*a, **k):
        return value
    return _f


LAST = {'resource_type': 'Condition', 'resource_id': 'c-1',
        'issues': [{'field_path': 'Condition.clinicalStatus.coding[0].code',
                    'suggested_value': 'resolved', 'title': 'Status is stale'}]}


def test_curatr_fix_proposes_and_says_nothing_changed():
    calls, replies = _run_with({'last_curatr': LAST}, {'id': 'act-77', 'status': 'proposed'})
    assert calls == [('curatr_apply_fix', {
        'resource_type': 'Condition', 'resource_id': 'c-1',
        'fixes': [{'field_path': 'Condition.clinicalStatus.coding[0].code',
                   'new_value': 'resolved'}],
        'patient_intent': 'requested from Telegram /curatr_fix',
        'reason': 'Status is stale'})]
    assert 'act-77' in replies[0]
    assert 'Nothing has changed' in replies[0]
    assert 'applied' not in replies[0].lower()


def test_approve_never_calls_the_fix_tool_and_points_at_the_review_page():
    calls, replies = _run_with({'last_curatr': LAST}, {'id': 'act-78'})
    assert [c[0] for c in calls] == ['curatr_apply_fix']   # only the proposal
    assert 'act-78' in replies[1]
    assert 'review page' in replies[1]
    assert 'applied' not in replies[1].lower()


def test_the_bot_source_no_longer_asserts_human_confirmation():
    assert 'human_confirmed=True' not in BOT_SOURCE
    assert 'Fix applied' not in BOT_SOURCE
