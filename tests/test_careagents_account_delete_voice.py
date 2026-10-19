"""Account delete must remove the account's voice usage rows (QA on #1004).

The privacy policy (#565) says deleting your account deletes "your email
address, your passkey and the rest of your account". AccountService.
delete_account lists the account-keyed tables it clears, and
VoiceUsageDay (ca_voice_usage_days, a foreign key to ca_accounts.id) is not
among them. On SQLite the rows are orphaned; on Postgres, where the foreign
key is enforced, the account delete itself fails after every tenant has
already been purged.

Strict xfail: it goes red when delete_account is fixed, so the marker is
removed in the same change.
"""

from __future__ import annotations

import pytest

from careagents import voice
from careagents.models import Account, VoiceUsageDay
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)


def _acct(svc):  # noqa: F811
    with svc.session() as s:
        a = Account(email="voice-delete@example.test")
        s.add(a)
        s.flush()
        return a.id


@pytest.mark.xfail(strict=True, reason=(
    "AccountService.delete_account does not delete VoiceUsageDay rows "
    "(QA on #1004)"))
def test_account_delete_removes_voice_usage(svc):  # noqa: F811
    aid = _acct(svc)
    assert voice.charge(svc, aid, "stt_seconds", 5, cap=100)[0]
    # On Postgres the FK makes this raise; on SQLite it returns True and
    # leaves the row behind. Either way the account is not wholly deleted.
    assert svc.delete_account(aid) is True
    with svc.session() as s:
        assert s.query(VoiceUsageDay).filter_by(account_id=aid).count() == 0
