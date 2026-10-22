"""A STOP always wins over a sign-in link (#1005).

A STOP that lands after a link was issued, including in the window
between the claim spending the link and the bind, must leave the number
unbound and opted out. A bind never clears an opt-out newer than the
link's issue time. The Postgres interleaving is covered by the race
script in the PR; these pin the ordering.

Synthetic handles only (555 numbers, example.com addresses).
"""

from __future__ import annotations

from careagents.models import Account
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

PHONE = "+15550100123"


def _acct(svc, email="stop@example.com"):  # noqa: F811
    with svc.session() as s:
        a = Account(email=email)
        s.add(a)
        s.flush()
        return a.id


def test_stop_between_claim_and_bind_refuses_the_bind(svc, monkeypatch):  # noqa: F811
    aid = _acct(svc)
    link_id = svc.peek_imessage_link(svc.issue_imessage_link(PHONE))
    real = svc.bind_imessage_handle

    def stop_then_bind(*a, **kw):
        # The link is already spent; the STOP lands before the bind.
        svc.imessage_stop(PHONE)
        return real(*a, **kw)

    monkeypatch.setattr(svc, "bind_imessage_handle", stop_then_bind)
    assert svc.claim_imessage_link(link_id, aid) == "expired"
    assert svc.find_surface_by_handle(PHONE) is None
    assert svc.imessage_opted_out(PHONE)


def test_bind_never_clears_an_opt_out_newer_than_the_link(svc):  # noqa: F811
    aid = _acct(svc)
    svc.imessage_stop(PHONE)
    with svc.session() as s:
        from careagents.accounts import _handle_key
        from careagents.models import ImessageHandleState
        stopped = s.get(ImessageHandleState, _handle_key(PHONE)).opted_out_at
    assert svc.bind_imessage_handle(
        aid, PHONE, welcome=True, issued_at=stopped - 1) == "expired"
    # Same instant: the STOP still wins.
    assert svc.bind_imessage_handle(
        aid, PHONE, welcome=True, issued_at=stopped) == "expired"
    assert svc.find_surface_by_handle(PHONE) is None
    assert svc.imessage_opted_out(PHONE)


def test_link_issued_after_an_older_opt_out_still_binds(svc):  # noqa: F811
    aid = _acct(svc)
    svc.imessage_stop(PHONE)
    with svc.session() as s:
        from careagents.accounts import _handle_key
        from careagents.models import ImessageHandleState
        stopped = s.get(ImessageHandleState, _handle_key(PHONE)).opted_out_at
    # A link minted after the STOP is the person coming back.
    assert svc.bind_imessage_handle(
        aid, PHONE, welcome=True, issued_at=stopped + 1) == "connected"
    assert not svc.imessage_opted_out(PHONE)


def test_stop_after_link_issued_then_claim_stays_opted_out(svc):  # noqa: F811
    aid = _acct(svc)
    link_id = svc.peek_imessage_link(svc.issue_imessage_link(PHONE))
    svc.imessage_stop(PHONE)
    assert svc.claim_imessage_link(link_id, aid) == "expired"
    assert svc.find_surface_by_handle(PHONE) is None
    assert svc.imessage_opted_out(PHONE)


def test_pairing_code_bind_without_a_link_is_unchanged(svc):  # noqa: F811
    # No link, no issue time: a bind the person started by texting a code
    # after their STOP still opts them back in, as before.
    aid = _acct(svc)
    svc.imessage_stop(PHONE)
    assert svc.bind_imessage_handle(aid, PHONE) == "connected"
    assert not svc.imessage_opted_out(PHONE)
