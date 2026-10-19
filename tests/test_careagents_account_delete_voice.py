"""Account delete must remove every row keyed to the account (QA on #1004).

The privacy policy (#565) says deleting your account deletes your email
address, your passkey and the rest of your account. AccountService.
delete_account lists the account-keyed tables it clears, and
VoiceUsageDay (ca_voice_usage_days, a foreign key to ca_accounts.id) was
not among them. On SQLite the rows were orphaned; on Postgres, where the
foreign key is enforced, the account delete itself failed after every
tenant had already been purged.

The enumeration test below makes the next such table fail here instead of
in production: a new foreign key to ca_accounts must be added to
delete_account and to ACCOUNT_KEYED.
"""

from __future__ import annotations

import importlib
import pkgutil

import careagents
from careagents import voice
from careagents.models import (Account, Base, ImessageHandleState,
                               ImessageLink, Surface, VoiceUsageDay)
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

# Every table with a foreign key to ca_accounts.id, each deleted by
# AccountService.delete_account (InviteLink through invites.delete_for_account).
ACCOUNT_KEYED = {
    "ca_activity_days", "ca_agents", "ca_connections", "ca_grants",
    "ca_invite_links", "ca_passkeys", "ca_people", "ca_refund_days",
    "ca_surfaces", "ca_usage_days", "ca_voice_usage_days",
}


def _acct(svc):  # noqa: F811
    with svc.session() as s:
        a = Account(email="voice-delete@example.test")
        s.add(a)
        s.flush()
        return a.id


def test_every_table_keyed_to_an_account_is_named():
    # Tables are registered on import; load every careagents module first.
    for mod in pkgutil.iter_modules(careagents.__path__):
        importlib.import_module(f"careagents.{mod.name}")
    keyed = {t.name for t in Base.metadata.sorted_tables
             if any(fk.target_fullname == "ca_accounts.id"
                    for fk in t.foreign_keys)}
    assert keyed == ACCOUNT_KEYED, (
        "a table's foreign keys to ca_accounts changed; delete its rows in "
        "AccountService.delete_account, then update ACCOUNT_KEYED")


def test_account_delete_removes_voice_usage(svc):  # noqa: F811
    aid = _acct(svc)
    assert voice.charge(svc, aid, "stt_seconds", 5, cap=100)[0]
    # On Postgres the FK made this raise before the fix.
    assert svc.delete_account(aid) is True
    with svc.session() as s:
        assert s.query(VoiceUsageDay).filter_by(account_id=aid).count() == 0
        assert s.get(Account, aid) is None


def test_account_delete_removes_texted_links(svc):  # noqa: F811
    aid = _acct(svc)
    number, other = "+15550100001", "+15550100002"
    with svc.session() as s:
        s.add(Surface(account_id=aid, kind="imessage", handle=number,
                      status="active"))
    assert svc.issue_imessage_link(number)
    assert svc.issue_imessage_link(other)
    assert svc.delete_account(aid) is True
    with svc.session() as s:
        # The sign-in link held the number itself: gone with the account.
        assert s.query(ImessageLink).filter_by(handle=number).count() == 0
        # Another person's link is not the account's to delete.
        assert s.query(ImessageLink).filter_by(handle=other).count() == 1
        # The per-number counters stay, keyed by a hash, not the number.
        assert all(number not in st.handle_key
                   for st in s.query(ImessageHandleState))


def test_unlinking_or_stop_removes_texted_links(svc):  # noqa: F811
    # Once a number is unbound the account no longer points at it, so its
    # links must go then, or account delete could not find them later.
    aid = _acct(svc)
    a, b = "+15550100003", "+15550100004"
    with svc.session() as s:
        s.add(Surface(account_id=aid, kind="imessage", handle=a,
                      status="active"))
    assert svc.issue_imessage_link(a)
    assert svc.issue_imessage_link(b)
    svc.disconnect_imessage(aid)
    svc.imessage_stop(b)
    with svc.session() as s:
        assert s.query(ImessageLink).count() == 0
    # The STOP itself is kept.
    assert svc.imessage_opted_out(b)
