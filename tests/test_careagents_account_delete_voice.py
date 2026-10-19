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


def _link_count(svc, handle=None):  # noqa: F811
    with svc.session() as s:
        q = s.query(ImessageLink)
        return (q.filter_by(handle=handle) if handle else q).count()


def test_a_voided_link_is_deleted(svc):  # noqa: F811
    number = "+15550100005"
    token = svc.issue_imessage_link(number)
    svc.void_imessage_link(svc.peek_imessage_link(token))
    assert _link_count(svc, number) == 0
    assert svc.peek_imessage_link(token) is None


def test_spent_and_expired_links_of_unbound_numbers_are_pruned(svc):  # noqa: F811
    from careagents.models import now
    aid = _acct(svc)
    bound, gone, live = "+15550100006", "+15550100007", "+15550100008"
    with svc.session() as s:
        s.add(Surface(account_id=aid, kind="imessage", handle=bound,
                      status="active"))
        # Made before #1004: a spent link for a bound number, a spent and
        # an expired one for a number nobody bound.
        s.add(ImessageLink(token_hash="a" * 64, handle=bound,
                           exp=now() + 60, used_at=now()))
        s.add(ImessageLink(token_hash="b" * 64, handle=gone,
                           exp=now() + 60, used_at=now()))
        s.add(ImessageLink(token_hash="c" * 64, handle=gone, exp=now() - 1))
    assert svc.issue_imessage_link(live)          # issuing prunes
    assert _link_count(svc, gone) == 0
    assert _link_count(svc, bound) == 1           # goes with its binding
    assert _link_count(svc, live) == 1            # unused and live
    assert svc.prune_imessage_links() == 0        # idempotent


def test_the_app_sweeps_old_links_at_start(cfg, svc):  # noqa: F811
    from careagents.app import create_app
    from careagents.models import now
    from tests.test_careagents import FakeClient
    with svc.session() as s:
        s.add(ImessageLink(token_hash="d" * 64, handle="+15550100009",
                           exp=now() - 1))
    create_app(config=cfg, client=FakeClient(), accounts=svc)
    assert _link_count(svc) == 0


def test_a_bind_that_loses_a_race_reads_as_expired(svc, monkeypatch):  # noqa: F811
    from sqlalchemy.orm.exc import StaleDataError

    def lost(*a, **k):
        raise StaleDataError("DELETE expected 1 row, 0 matched")
    monkeypatch.setattr(svc, "_bind_imessage_handle", lost)
    assert svc.bind_imessage_handle(_acct(svc), "+15550100010") == "expired"


def test_claiming_a_link_deleted_underneath_reads_as_expired(svc):  # noqa: F811
    aid = _acct(svc)
    number = "+15550100011"
    link_id = svc.peek_imessage_link(svc.issue_imessage_link(number))
    svc.imessage_stop(number)                     # deletes the link
    assert svc.claim_imessage_link(link_id, aid) == "expired"
