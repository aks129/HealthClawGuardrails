"""Security review of #993: exploit tests for inbox-keyed real-record invites.

Each test asserts the safe behaviour. F1 (a non-Gmail +tag join undoing a
revoke or taking a second stage 1 place) and F2 (a trailing-dot invite that
could not be revoked) were pinned as strict xfails and are fixed. Synthetic
addresses only.
"""

from __future__ import annotations

import pytest

from careagents import invites
from careagents.models import RealRecordInvite
from tests.test_careagents import _login, cfg, svc  # noqa: F401
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _age_caps, made, sent)
from tests.test_careagents_invite_links import _code, _join_via


def _live_rows(svc) -> list[str]:  # noqa: F811
    with svc.session() as s:
        return [r.email for r in s.query(RealRecordInvite)
                if r.revoked_at is None]


def test_a_link_join_through_a_plus_alias_does_not_undo_an_operator_revoke(
        made, sent, monkeypatch):  # noqa: F811
    """The operator revoked carol@. Carol joins a friend's link as
    carol+x@ (same inbox on most providers). On main the join saw the
    revoked alias (`account_key`) and granted nothing; it must still."""
    app, svc = made(RESEND_API_KEY="re_test", CARE_REAL_RECORDS="allowlist",  # noqa: F811
                    CARE_REAL_RECORDS_ALLOWLIST="friend@example.com")
    svc.invite_real_records("carol@example.net", "operator")
    assert svc.revoke_real_records_invite("carol@example.net")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="friend@example.com")
    code = _code(c)
    _join_via(app, sent, code, "carol+x@example.net", first_run=False)
    assert invites.join_outcome(svc.session, "carol+x@example.net") != "granted"
    assert not svc.real_records_invited("carol+x@example.net"), (
        "an operator-revoked inbox got real records back through +tag")


def test_a_link_join_through_a_plus_alias_takes_no_second_stage1_place(
        made, sent, monkeypatch):  # noqa: F811
    """carol@ holds a live operator invite. Joining as carol+x@ must not
    spend a second stage 1 place on the same inbox."""
    app, svc = made(RESEND_API_KEY="re_test", CARE_REAL_RECORDS="allowlist",  # noqa: F811
                    CARE_REAL_RECORDS_ALLOWLIST="friend@example.com")
    svc.invite_real_records("carol@example.net", "operator")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="friend@example.com")
    code = _code(c)
    _join_via(app, sent, code, "carol+x@example.net", first_run=False)
    assert len(_live_rows(svc)) == 1, _live_rows(svc)


@pytest.mark.parametrize("typed", [
    "bob@gmail.com.", "bob@ｇｍａｉｌ.com", "bob@GMAIL.COM",
    "b.o.b+z@googlemail.com", "BOB@xn--gmail-.com"])
def test_revoke_by_any_alias_or_reports_nothing_revoked(svc, typed):  # noqa: F811
    """After inviting bob@gmail.com, a revoke through a lookalike either
    revokes the inbox or says nothing was revoked; it never leaves a
    second live row."""
    from careagents.accounts import AuthError
    svc.invite_real_records("bob@gmail.com", "owner")
    try:
        revoked = svc.revoke_real_records_invite(typed)
    except AuthError:
        revoked = False
    assert revoked == (not svc.real_records_invited("bob@gmail.com"))


@pytest.mark.parametrize("attacker", [
    "bob@gmail.com.", "bob@gmail。com", "bоb@gmail.com",  # Cyrillic o
    "bob​@gmail.com", '"bob"@gmail.com', "bob@gmai1.com",
    "bob@gmail.com@evil.example", "bob%gmail.com@evil.example",
    "bob@evil.example", "bob@gmail.co", "bob@gmail.com.evil.example",
    "Karen@corp.example"])
def test_lookalikes_do_not_match(svc, attacker):  # noqa: F811
    svc.invite_real_records("bob@gmail.com", "owner")
    svc.invite_real_records("aren@corp.example", "owner")
    assert svc.real_records_invited(attacker) is False, attacker


def test_a_malformed_invite_is_refused_or_never_matches_a_real_inbox(svc):  # noqa: F811
    """An invite typed with a trailing dot is keyed as typed; the revoke of
    the plain address must not report success while it stays live."""
    from careagents.accounts import AuthError
    try:
        svc.invite_real_records("dot@gmail.com.", "owner")
    except AuthError:
        return
    assert svc.revoke_real_records_invite("dot@gmail.com") is True or (
        not svc.real_records_invited("dot@gmail.com."))


def test_deleting_an_account_deletes_its_inbox_invite_under_another_alias(
        svc):  # noqa: F811
    """Pins the delete path to the invite key (a mutation to the typed
    address survived the PR's suite)."""
    from careagents.models import Account
    svc.invite_real_records("Jane.Doe+beta@googlemail.com", "owner")
    with svc.session() as s:
        acct = Account(email="j.anedoe@gmail.com")
        s.add(acct)
        s.flush()
        account_id = acct.id
    assert svc.real_records_invited("j.anedoe@gmail.com")
    assert svc.delete_account(account_id)
    with svc.session() as s:
        assert s.query(RealRecordInvite).count() == 0
