# ruff: noqa: F811  (the fixtures are imported, then named as parameters)
"""The owner's Morning block on /admin/funnel (careagents/signins.py):
the loop counts, the "Needs you today" rows and their fixed-rule nudges,
and the Testers table's ref, "you" mark and milestone legend.

Owner decision 2026-10-16: tester emails may appear on the owner-only page.
Synthetic data only.
"""

from __future__ import annotations

import datetime as dt

from careagents import beta_signup, funnel, signins
from careagents.models import Account, UsageDay
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)
from tests.test_careagents_funnel import _app
from tests.test_careagents_signins import NOW, _day, _make, _ts

OWNER = "owner@example.com"


def _rows(svc, **kw):
    return signins.testers(svc.session, moment=NOW, **kw)


def _asked(svc, aid, days_ago):
    with svc.session() as s:
        s.add(UsageDay(account_id=aid, day=_day(days_ago), turns=1))
    funnel.mark(svc.session, aid, "first_answer", at=_ts(days_ago))


def _request(svc, email, first_name="Pat", ref=None, days_ago=1,
             status="pending", confirmed_at=None):
    with svc.session() as s:
        s.add(beta_signup.BetaRequest(
            email=email, first_name=first_name, ref=ref, status=status,
            created_at=_ts(days_ago), updated_at=_ts(days_ago),
            confirmed_at=confirmed_at))


def _need(rows, pending=(), can_connect=lambda e: False):
    return {n["email"]: n for n in signins.needs_you(
        rows, list(pending), can_connect=can_connect)}


# --- the loop line ----------------------------------------------------------

def test_the_loop_counts_each_step_in_order_and_leaves_the_owner_out(svc):
    me = _make(svc, OWNER, 9)
    a = _make(svc, "a@example.com", 5)
    _make(svc, "b@example.com", 5)
    for m in ("first_answer", "records_connected", "action_approved"):
        funnel.mark(svc.session, a, m, at=_ts(4))
        funnel.mark(svc.session, me, m, at=_ts(4))
    _asked(svc, a, 2)        # active after the join day: returned
    _asked(svc, me, 2)
    loop = signins.loop_counts(_rows(svc, owner_emails=frozenset({OWNER})))
    assert [s["label"] for s in loop["chain"]] == [
        "signed in", "first answer", "first action"]
    assert [s["count"] for s in loop["chain"]] == [2, 1, 1]
    assert [(s["label"], s["count"]) for s in loop["side"]] == [
        ("connected their own records", 1), ("came back another day", 1)]


def test_the_arrow_chain_never_goes_up_on_a_realistic_beta(svc):
    # Most testers stay on the made-up records: they ask, some act, few
    # connect their own. One acted with no first_answer stamp (a lost
    # count): it must not lift "first action" above "first answer".
    _make(svc, "new@example.com", 0)
    for i in range(4):
        aid = _make(svc, f"sample{i}@example.com", 6)
        _asked(svc, aid, 3)
    for i in range(2):
        aid = _make(svc, f"acted{i}@example.com", 8)
        _asked(svc, aid, 1)
        funnel.mark(svc.session, aid, "action_approved", at=_ts(1))
    aid = _make(svc, "real@example.com", 9)
    funnel.mark(svc.session, aid, "records_connected", at=_ts(8))
    _asked(svc, aid, 2)
    odd = _make(svc, "odd@example.com", 7)
    funnel.mark(svc.session, odd, "action_approved", at=_ts(2))
    loop = signins.loop_counts(_rows(svc))
    counts = [s["count"] for s in loop["chain"]]
    assert counts == [9, 7, 2]
    assert all(a >= b for a, b in zip(counts, counts[1:]))
    side = {s["key"]: s["count"] for s in loop["side"]}
    assert side == {"records_connected": 1, "returned": 7}


def test_signed_in_counts_an_account_without_a_funnel_row(svc):
    aid = _make(svc, "old@example.com", 9)
    with svc.session() as s:
        s.query(funnel.FunnelAccount).filter_by(account_id=aid).delete()
    assert signins.loop_counts(_rows(svc))["chain"][0]["count"] == 1


def test_active_only_on_the_join_day_is_not_returned(svc):
    aid = _make(svc, "same@example.com", 2)
    _asked(svc, aid, 2)
    assert _rows(svc)[0]["returned"] is False


# --- the nudge rules ---------------------------------------------------------

def test_joined_never_asked_gets_the_first_question_after_a_day(svc):
    _make(svc, "new@example.com", 0)
    _make(svc, "day@example.com", 1)
    need = _need(_rows(svc))
    assert "new@example.com" not in need       # joined today: give it time
    n = need["day@example.com"]
    assert n["rule"] == "never_asked"
    assert n["days"] == 1 and n["since"] == "joined"
    assert n["nudge"] == "Try asking: what's due for me this year?"


def test_asked_never_connected_only_when_invited_to_real_records(svc):
    aid = _make(svc, "ask@example.com", 6)
    _asked(svc, aid, 2)
    rows = _rows(svc)
    invited = _need(rows, can_connect=lambda e: e == "ask@example.com")
    assert invited["ask@example.com"]["rule"] == "not_connected"
    assert "walk you through connecting" in invited[
        "ask@example.com"]["nudge"]
    # Not invited: the connect nudge would point at a closed door, so the
    # next rule answers instead.
    assert _need(rows)["ask@example.com"]["rule"] == "never_acted"


def test_answered_never_acted_gets_the_prep_list(svc):
    aid = _make(svc, "ans@example.com", 6)
    funnel.mark(svc.session, aid, "records_connected", at=_ts(5))
    _asked(svc, aid, 2)
    n = _need(_rows(svc), can_connect=lambda e: True)["ans@example.com"]
    assert n["rule"] == "never_acted"
    assert n["nudge"] == "Ask it to write your next-visit prep list."


def test_quiet_three_days_after_an_action(svc):
    aid = _make(svc, "done@example.com", 10)
    for m in ("records_connected", "action_approved"):
        funnel.mark(svc.session, aid, m, at=_ts(5))
    _asked(svc, aid, 3)
    n = _need(_rows(svc))["done@example.com"]
    assert n["rule"] == "quiet"
    assert n["days"] == 3
    assert n["nudge"].startswith("Did something get in the way?")


def test_active_yesterday_after_an_action_needs_nothing(svc):
    aid = _make(svc, "ok@example.com", 10)
    funnel.mark(svc.session, aid, "action_approved", at=_ts(5))
    _asked(svc, aid, 1)
    assert _need(_rows(svc)) == {}


def test_the_owner_is_never_a_row_to_nudge(svc):
    _make(svc, OWNER, 5)
    assert _need(_rows(svc, owner_emails=frozenset({OWNER}))) == {}


def test_an_owner_mailbox_variant_is_marked_and_not_counted(svc):
    _make(svc, "Owner+test@Example.com", 5)
    _make(svc, "t@example.com", 5)
    rows = _rows(svc, owner_emails=frozenset({OWNER}))
    marks = {r["email"]: r["is_owner"] for r in rows}
    assert marks == {"owner+test@example.com": True, "t@example.com": False}
    assert signins.loop_counts(rows)["chain"][0]["count"] == 1
    assert set(_need(rows)) == {"t@example.com"}


def test_an_unconfirmed_beta_request_is_named(svc):
    _request(svc, "wait@example.com", first_name="Wren", days_ago=2)
    _request(svc, "yes@example.com", status="new", confirmed_at=_ts(1))
    pending = signins.pending_requests(svc.session, moment=NOW)
    assert [p["email"] for p in pending] == ["wait@example.com"]
    n = _need([], pending)["wait@example.com"]
    assert n["rule"] == "not_confirmed"
    assert n["name"] == "Wren"
    assert n["days"] == 2


def test_a_request_from_someone_already_in_is_not_waiting(svc):
    _make(svc, "in@example.com", 3)
    _request(svc, "in@example.com")
    pending = signins.pending_requests(svc.session, moment=NOW)
    assert "in@example.com" not in {
        n["email"] for n in signins.needs_you(_rows(svc), pending)
        if n["rule"] == "not_confirmed"}


# --- the Testers table gaps --------------------------------------------------

def test_ref_is_the_beta_ref_else_the_inviter(svc, monkeypatch):
    from careagents import invites
    old = _make(svc, "old@example.com", 9)
    new = _make(svc, "new@example.com", 1)
    _make(svc, "plain@example.com", 1)
    _request(svc, "old@example.com", ref="clinic-flyer", status="new",
             confirmed_at=_ts(9))
    funnel.set_inviter(svc.session, new, old)
    monkeypatch.setattr(invites, "inviter_first_name",
                        lambda s, a: "Olive" if a == old else None)
    refs = {r["email"]: r["ref"] for r in _rows(svc)}
    assert refs == {"old@example.com": "clinic-flyer",
                    "new@example.com": "Olive", "plain@example.com": ""}


def test_the_owner_page_shows_morning_ref_you_and_the_legend(svc,
                                                            monkeypatch):
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    _login(app.test_client(), svc, monkeypatch, email="tess@example.com")
    with svc.session() as s:
        acct = s.query(Account).filter_by(email="tess@example.com").one()
        acct.created_at -= 2 * 86400
    _request(svc, "wren@example.com", first_name="Wren", ref="flyer")
    c = app.test_client()
    _login(c, svc, monkeypatch, email=OWNER)
    html = c.get("/admin/funnel").get_data(as_text=True)
    assert html.index('id="funnel-morning"') < html.index(
        'id="funnel-testers"')
    # tess, not the owner
    assert '<span class="num">1</span> signed in' in html
    # Came back is beside the arrow chain, never on it.
    chain = html.split('class="morning-loop">', 1)[1].split("</p>", 1)[0]
    assert "came back" not in chain and "connected" not in chain
    assert 'class="morning-side"' in html and "came back another day" in html
    assert "Needs you today" in html
    assert "Try asking: what&#39;s due for me this year?" in html
    assert "Wren · wren@example.com" in html
    assert 'class="testers-you">you<' in html
    assert "steps the tester has reached" in html
    assert "admin_funnel.js" in html


def test_non_owners_never_see_the_morning_block(svc, monkeypatch):
    _request(svc, "secret-wren@example.com", first_name="Wren")
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    anon = app.test_client().get("/admin/funnel")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="tess@example.com")
    signed_in = c.get("/admin/funnel")
    for r in (anon, signed_in):
        assert r.status_code == 404
        body = r.get_data(as_text=True)
        assert "secret-wren" not in body and "Needs you today" not in body


def test_morning_rows_carry_no_more_than_the_owner_page_needs(svc):
    _make(svc, "q@example.com", 4)
    _request(svc, "p@example.com")
    for n in signins.needs_you(
            _rows(svc), signins.pending_requests(svc.session, moment=NOW)):
        assert set(n) == {"name", "email", "rule", "stuck_at", "days",
                          "since", "nudge"}
        assert n["nudge"] in signins.NUDGES.values()


def test_moment_default_is_now(svc):
    # A request made just now is 0 days old.
    with svc.session() as s:
        s.add(beta_signup.BetaRequest(email="now@example.com",
                                      first_name="N", status="pending"))
    # 0, or 1 across a UTC midnight between the write and the read.
    assert signins.pending_requests(svc.session)[0]["days"] in (0, 1)
    assert dt.datetime.now(dt.timezone.utc) > NOW
