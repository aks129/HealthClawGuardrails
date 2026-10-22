"""Personal invite links (careagents/invites.py): one per account, capped,
revocable, unguessable, and a join through one gets a real-record invite
within the stage 1 cap.

Synthetic data only.
"""

from __future__ import annotations

import re
import threading

import pytest

from careagents import beta, invites
from careagents.config import Config
from careagents.models import RealRecordInvite
from tests.test_careagents import _login
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _confirm_token, _post, made, sent)


def _code(client) -> str:
    html = client.get("/settings").get_data(as_text=True)
    m = re.search(r"/i\?c=([0-9a-f]{32})", html)
    assert m, html
    return m.group(1)


def _inviter(app, svc, monkeypatch, email="inviter@example.com"):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    return c, _code(c), _account_id(svc, email)


def _account_id(svc, email):
    from careagents.models import Account
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).one().id


def _join_via(app, sent, code, email, ip="203.0.113.9", first_run=True):  # noqa: F811
    """Open the link, submit the form with its code, then confirm from the
    email, on the confirm page in this browser (so it signs in)."""
    c = app.test_client()
    assert c.get(f"/i?c={code}", headers={"X-Real-IP": ip}).status_code == 200
    r = _post(c, ip=ip, email=email, first_name="Robin", invite=code)
    assert r.status_code == 200
    token = _confirm_token([m for m in sent if m[0] == email][-1][3])
    data = {"t": token}
    if first_run:
        page = c.get(f"/beta/confirm?t={token}").get_data(as_text=True)
        data["n"] = re.search(r'name="n" value="([^"]+)"', page).group(1)
    return c, c.post("/beta/confirm", data=data)


def _link(svc, code):
    with svc.session() as s:
        row = s.query(invites.InviteLink).filter_by(code=code).one()
        return {"used": row.used, "cap": row.cap,
                "revoked": row.revoked_at is not None}


def _invite_row(svc, email):
    """The invite the inbox of `email` holds (keyed by `invite_key`)."""
    with svc.session() as s:
        row = s.get(RealRecordInvite, invites.invite_key(email))
        return None if row is None else (row.invited_by, row.revoked_at)


# --- one link per account -------------------------------------------------

def test_settings_shows_one_link_made_on_first_view(made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = app.test_client()
    _login(c, svc, monkeypatch, email="inviter@example.com")
    first = _code(c)
    assert _code(c) == first                    # the same link every view
    html = c.get("/settings").get_data(as_text=True)
    assert "Invite friends and family" in html
    assert "0 of 5 used" in html
    assert f"http://localhost/i?c={first}" in html
    with svc.session() as s:
        assert s.query(invites.InviteLink).count() == 1


def test_the_code_is_128_random_bits(made, monkeypatch):  # noqa: F811
    app, svc = made()
    codes = set()
    for i in range(5):
        c = app.test_client()
        _login(c, svc, monkeypatch, email=f"p{i}@example.com")
        codes.add(_code(c))
    assert len(codes) == 5
    assert all(re.fullmatch(r"[0-9a-f]{32}", x) for x in codes)


def test_racing_first_views_make_one_link(tmp_path, monkeypatch):
    """Two first views at once: the unique account_id keeps one row."""
    from careagents.accounts import AccountService
    from tests.test_careagents import _make_account
    cfg = Config(env={"CARE_DATABASE_URL": f"sqlite:///{tmp_path}/r.db",
                      "OPENAI_API_KEY": "k"})
    svc = AccountService(cfg)
    try:
        acct = _make_account(svc, monkeypatch, "race@example.com")
        out, start = [], threading.Barrier(8)

        def view():
            start.wait()
            out.append(invites.link_for(svc.session, acct.id, 5)["code"])
        threads = [threading.Thread(target=view) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(out)) == 1 and len(out) == 8
        with svc.session() as s:
            assert s.query(invites.InviteLink).count() == 1
    finally:
        svc.engine.dispose()


# --- opening a link ---------------------------------------------------------

def test_a_live_link_opens_the_sign_up_with_its_code(made, monkeypatch):  # noqa: F811
    app, svc = made()
    c, code, _ = _inviter(app, svc, monkeypatch)
    r = app.test_client().get(f"/i?c={code}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'id="beta-form"' in html
    assert f'name="invite" value="{code}"' in html
    assert "Someone you know invited you" in html
    assert "inviter@example.com" not in html      # never the inviter's email
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_the_inviters_own_first_name_is_shown(made, sent, monkeypatch):  # noqa: F811
    from tests.test_careagents_beta_signup import _join
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, email="inviter@example.com", first_name="Sam")
    c, code, _ = _inviter(app, svc, monkeypatch)
    html = app.test_client().get(f"/i?c={code}").get_data(as_text=True)
    assert "Sam invited you" in html
    assert "inviter@example.com" not in html


def test_unknown_revoked_and_full_links_show_the_same_plain_page(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test", CARE_INVITE_LINK_CAP="1")
    _, full, _ = _inviter(app, svc, monkeypatch, email="a@example.com")
    _join_via(app, sent, full, "j1@example.com")
    _, revoked, _ = _inviter(app, svc, monkeypatch, email="b@example.com")
    assert invites.revoke(svc.session, revoked)
    bodies = set()
    for code in (full, revoked, "0" * 32, "not-a-code", "A" * 32):
        r = app.test_client().get(f"/i?c={code}")
        assert r.status_code == 404
        assert r.headers["Referrer-Policy"] == "no-referrer"
        bodies.add(r.get_data(as_text=True))
    assert len(bodies) == 1
    assert "This invite link isn't open" in bodies.pop()


def test_opens_are_rate_limited_per_address(made, monkeypatch):  # noqa: F811
    from careagents import funnel
    app, svc = made()
    c = app.test_client()
    for i in range(invites.OPENS_PER_WINDOW):
        assert c.get(f"/i?c={i:032x}",
                     headers={"X-Real-IP": "198.51.100.4"}).status_code == 404
    r = c.get("/i?c=" + "1" * 32, headers={"X-Real-IP": "198.51.100.4"})
    assert r.status_code == 429
    # Another address is not held back by this one.
    assert c.get("/i?c=" + "1" * 32,
                 headers={"X-Real-IP": "198.51.100.5"}).status_code == 404
    report = funnel.daily(svc.session, days=1)
    assert report["days"][0]["rate_limited"] == 1


# --- joining through a link ---------------------------------------------------

def test_a_join_takes_a_seat_and_gets_the_real_record_invite(
        made, sent, monkeypatch):  # noqa: F811
    from careagents import funnel
    app, svc = made(RESEND_API_KEY="re_test")
    svc.invite_real_records("inviter@example.com", "operator")
    _, code, _ = _inviter(app, svc, monkeypatch)
    inviter_id = _account_id(svc, "inviter@example.com")
    _post(app.test_client(), email="robin@example.com", invite=code)
    assert _link(svc, code)["used"] == 0          # a submit spends nothing
    c, r = _join_via(app, sent, code, "robin@example.com")
    assert r.status_code == 303                   # signed in, chat opened
    assert _link(svc, code)["used"] == 1
    assert _invite_row(svc, "robin@example.com") == (inviter_id, None)
    assert invites.join_outcome(svc.session, "robin@example.com") == "granted"
    joiner = _account_id(svc, "robin@example.com")
    with svc.session() as s:
        row = s.get(funnel.FunnelAccount, joiner)
        assert row.invited_by == inviter_id
        assert row.joined and row.confirmed
    html = app.test_client().get("/settings")      # signed out: no row
    assert html.status_code == 302


def test_a_join_without_a_live_code_is_an_ordinary_join(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    r = _post(c, email="plain@example.com", invite="f" * 32)
    assert r.status_code == 200 and r.get_json() == {"ok": True, "sent": True}
    # A dead code names nobody: the ordinary subject.
    assert sent[-1][1].startswith("Confirm you asked to test CareAgents")
    token = _confirm_token(sent[-1][3])
    assert c.post("/beta/confirm", data={"t": token}).status_code == 200
    assert _invite_row(svc, "plain@example.com") is None
    assert invites.join_outcome(svc.session, "plain@example.com") is None


def test_the_cap_is_enforced(made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test", CARE_INVITE_LINK_CAP="2")
    _, code, _ = _inviter(app, svc, monkeypatch)
    # Three requests made while the link still had room, then confirmed.
    for i in range(3):
        _post(app.test_client(), ip=f"203.0.113.{20 + i}",
              email=f"j{i}@example.com", invite=code)
    for i in range(3):
        mine = [m for m in sent if m[0] == f"j{i}@example.com"][-1]
        app.test_client().post("/beta/confirm",
                               data={"t": _confirm_token(mine[3])})
    assert _link(svc, code)["used"] == 2
    admitted = [invites.join_outcome(svc.session, f"j{i}@example.com")
                for i in range(3)]
    # The inviter has no real-record access, so the two admitted join on
    # made-up records ("none"); the third was not admitted at all.
    assert admitted.count("none") == 2 and admitted.count(None) == 1
    assert app.test_client().get(f"/i?c={code}").status_code == 404


def test_concurrent_joins_never_pass_the_cap(tmp_path, monkeypatch):
    """The seat is one conditional UPDATE: twelve joins racing on a link
    with five places admit exactly five."""
    from careagents.accounts import AccountService
    from tests.test_careagents import _make_account
    cfg = Config(env={"CARE_DATABASE_URL": f"sqlite:///{tmp_path}/c.db",
                      "OPENAI_API_KEY": "k"})
    svc = AccountService(cfg)
    try:
        acct = _make_account(svc, monkeypatch, "host@example.com")
        link = invites.link_for(svc.session, acct.id, 5)
        won, start = [], threading.Barrier(12)

        def join():
            start.wait()
            won.append(invites.claim_seat(svc.session, link["id"], 5))
        threads = [threading.Thread(target=join) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert won.count(True) == 5 and len(won) == 12
        assert invites.link_for(svc.session, acct.id, 5)["used"] == 5
    finally:
        svc.engine.dispose()


def test_a_revoked_link_admits_nobody_even_a_request_made_before(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    _, code, _ = _inviter(app, svc, monkeypatch)
    c = app.test_client()
    _post(c, email="late@example.com", invite=code)
    assert invites.revoke(svc.session, code)
    c.post("/beta/confirm", data={"t": _confirm_token(sent[-1][3])})
    assert _link(svc, code)["used"] == 0
    assert _invite_row(svc, "late@example.com") is None
    assert app.test_client().get(f"/i?c={code}").status_code == 404


def test_the_inviter_cannot_spend_their_own_link(made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    _, code, _ = _inviter(app, svc, monkeypatch)
    c = app.test_client()
    _post(c, email="inviter@example.com", invite=code)
    c.post("/beta/confirm", data={"t": _confirm_token(sent[-1][3])})
    assert _link(svc, code)["used"] == 0


def test_the_real_record_invite_respects_the_stage_1_cap(
        made, sent, monkeypatch):  # noqa: F811
    """With 25 live invites the joiner still joins, on made-up records, and
    the hub says why in one line."""
    app, svc = made(RESEND_API_KEY="re_test", CARE_REAL_RECORDS="allowlist",
                    FASTEN_PUBLIC_KEY="pub123")
    # The inviter's own invite is one of the 25.
    svc.invite_real_records("inviter@example.com", "operator")
    for i in range(beta.STAGE1_INVITE_CAP - 1):
        svc.invite_real_records(f"t{i}@example.com", "operator")
    _, code, _ = _inviter(app, svc, monkeypatch)
    c, r = _join_via(app, sent, code, "nine@example.com")
    assert _link(svc, code)["used"] == 1
    assert _invite_row(svc, "nine@example.com") is None
    assert invites.join_outcome(svc.session, "nine@example.com") == "full"
    hub = c.get("/home").get_data(as_text=True)
    assert "Spots for connecting your own records are full right now" in hub
    assert 'data-connector="fasten"' not in hub


def test_the_real_record_invite_waits_on_the_tester_terms(
        made, sent, monkeypatch):  # noqa: F811
    from tests.careagents_stage1_helpers import pend_terms
    app, svc = made(RESEND_API_KEY="re_test", CARE_REAL_RECORDS="allowlist")
    pend_terms(monkeypatch)
    _, code, _ = _inviter(app, svc, monkeypatch)
    _join_via(app, sent, code, "early@example.com")
    assert _invite_row(svc, "early@example.com") is None
    assert invites.join_outcome(svc.session, "early@example.com") == "closed"


def test_deleting_the_inviter_removes_their_link(made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    _, code, _ = _inviter(app, svc, monkeypatch)
    _join_via(app, sent, code, "stays@example.com")
    assert svc.delete_account(_account_id(svc, "inviter@example.com"))
    with svc.session() as s:
        assert s.query(invites.InviteLink).count() == 0
        # The join stays: it is someone else's sign-up.
        assert s.get(invites.InviteJoin,
                     invites.mailbox_hash("stays@example.com")) is not None


# --- operator commands ------------------------------------------------------

def test_cli_lists_revokes_and_sets_a_cap(made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    _, code, _ = _inviter(app, svc, monkeypatch)
    _join_via(app, sent, code, "one@example.com")
    runner = app.test_cli_runner()
    out = runner.invoke(args=["invite-links", "list"]).output
    assert code in out and "inviter@example.com" in out and "1/5" in out
    r = runner.invoke(args=["invite-links", "cap", "inviter@example.com",
                            "1"])
    assert r.exit_code == 0, r.output
    assert _link(svc, code)["cap"] == 1
    assert app.test_client().get(f"/i?c={code}").status_code == 404   # full
    r = runner.invoke(args=["invite-links", "cap", code, "default"])
    assert r.exit_code == 0 and _link(svc, code)["cap"] is None
    r = runner.invoke(args=["invite-links", "cap", code, "-1"])
    assert r.exit_code != 0
    r = runner.invoke(args=["invite-links", "revoke", code])
    assert r.exit_code == 0 and _link(svc, code)["revoked"]
    r = runner.invoke(args=["invite-links", "revoke", code])
    assert r.exit_code != 0                      # nothing left to revoke
    html = _login_settings(app, svc, monkeypatch)
    assert "turned off" in html and f"/i?c={code}" not in html


def _login_settings(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch, email="inviter@example.com")
    return c.get("/settings").get_data(as_text=True)


@pytest.mark.parametrize("raw,want", [("7", 7), ("x", 5), ("-3", 0)])
def test_the_cap_setting(raw, want):
    assert Config(env={"CARE_INVITE_LINK_CAP": raw}).invite_link_cap == want


# --- #928 security fixes ------------------------------------------------------

def test_the_path_form_of_a_link_is_gone(made, monkeypatch):  # noqa: F811
    """The code travels in the query string, which the access log drops
    (#928 F5); /i/<code> answers like any unknown page."""
    app, svc = made()
    _, code, _ = _inviter(app, svc, monkeypatch)
    assert app.test_client().get(f"/i/{code}").status_code == 404
    assert app.test_client().get(f"/i?c={code}").status_code == 200


def test_a_confirm_link_session_gets_no_invite_link(made, sent, monkeypatch):  # noqa: F811
    """A session opened by a beta confirm link is limited (#920 F1): it
    neither makes nor sees an invite link (#928 F6). Settings says how to
    get one instead of leaving the section out (#937)."""
    app, svc = made(RESEND_API_KEY="re_test")
    _, code, _ = _inviter(app, svc, monkeypatch)
    c, r = _join_via(app, sent, code, "linked@example.com")
    assert r.status_code == 303
    html = c.get("/settings").get_data(as_text=True)
    assert "/i?c=" not in html and 'id="invite-url"' not in html
    assert 'href="/auth?invite=1"' in html
    with svc.session() as s:
        assert s.query(invites.InviteLink).filter_by(
            account_id=_account_id(svc, "linked@example.com")).count() == 0


def test_removing_a_request_and_asking_again_takes_no_second_seat(
        made, sent, monkeypatch):  # noqa: F811
    from tests.test_careagents_beta_signup import _age_caps, _token
    app, svc = made(RESEND_API_KEY="re_test")
    _, code, _ = _inviter(app, svc, monkeypatch)
    _join_via(app, sent, code, "again@example.com", first_run=False)
    assert _link(svc, code)["used"] == 1
    removal = _token([m for m in sent if m[0] == "again@example.com"][-1][2])
    c = app.test_client()
    assert c.post("/beta/remove", data={"t": removal}).status_code == 200
    _age_caps(svc)
    _join_via(app, sent, code, "again@example.com", first_run=False)
    assert _link(svc, code)["used"] == 1


def test_a_link_passes_on_real_records_only_from_an_inviter_who_has_them(
        made, sent, monkeypatch):  # noqa: F811
    """Owner ruling on #928 F3: access flows from the operator, one link at
    a time; an account with none hands on none, and the joiner is told."""
    app, svc = made(RESEND_API_KEY="re_test", CARE_REAL_RECORDS="allowlist",
                    CARE_REAL_RECORDS_ALLOWLIST="root@example.com",
                    FASTEN_PUBLIC_KEY="pub123")
    _, root_code, _ = _inviter(app, svc, monkeypatch, email="root@example.com")
    _join_via(app, sent, root_code, "child@example.com", first_run=False)
    assert invites.join_outcome(svc.session, "child@example.com") == "granted"
    # The child now holds a live invite, so its link passes one on.
    _, child_code, _ = _inviter(app, svc, monkeypatch,
                                email="child@example.com")
    _join_via(app, sent, child_code, "grandchild@example.com",
              ip="203.0.113.50", first_run=False)
    assert invites.join_outcome(
        svc.session, "grandchild@example.com") == "granted"
    # Revoked by the operator: the child's link hands on nothing more.
    assert svc.revoke_real_records_invite("child@example.com")
    c, _ = _join_via(app, sent, child_code, "late@example.com",
                     ip="203.0.113.51")
    assert invites.join_outcome(svc.session, "late@example.com") == "none"
    assert _invite_row(svc, "late@example.com") is None
    hub = c.get("/home").get_data(as_text=True)
    # The kinder words the empty hub uses (patient re-walk on #979).
    assert ("For now you&#39;re using made-up records. Connecting your own "
            "records is coming later in the beta.") in hub


def test_a_confirm_link_session_of_the_owner_gets_no_funnel_page(
        made, sent, monkeypatch):  # noqa: F811
    """#928 F6: /admin/funnel wants a full sign-in, as the routes that
    widen an account do."""
    app, svc = made(RESEND_API_KEY="re_test",
                    CARE_OWNER_EMAILS="owner@example.com")
    c = app.test_client()
    assert _post(c, email="owner@example.com").status_code == 200
    token = _confirm_token([m for m in sent
                            if m[0] == "owner@example.com"][-1][3])
    page = c.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    nonce = re.search(r'name="n" value="([^"]+)"', page).group(1)
    assert c.post("/beta/confirm",
                  data={"t": token, "n": nonce}).status_code == 303
    assert c.get("/admin/funnel").status_code == 404
    full = app.test_client()
    _login(full, svc, monkeypatch, email="owner@example.com")
    assert full.get("/admin/funnel").status_code == 200


def test_racing_grants_fill_the_stage1_cap_exactly(tmp_path):
    """#928 F1: the count and the write are one statement (and, on
    Postgres, one advisory-locked transaction). Twelve racing grants at 20
    live invites make exactly five, and the operator's command shares the
    same path."""
    from careagents.accounts import AccountService
    cfg = Config(env={"CARE_DATABASE_URL": f"sqlite:///{tmp_path}/g.db",
                      "OPENAI_API_KEY": "k"})
    svc = AccountService(cfg)
    try:
        for i in range(beta.STAGE1_INVITE_CAP - 5):
            svc.invite_real_records(f"t{i}@example.com", "operator")
        out, start = [], threading.Barrier(12)

        def grant(i):
            start.wait()
            out.append(svc.grant_join_invite(f"r{i}@example.org", "acct_x"))
        threads = [threading.Thread(target=grant, args=(i,))
                   for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert out.count("created") == 5 and out.count("full") == 7
        with svc.session() as s:
            assert s.query(RealRecordInvite).filter(
                RealRecordInvite.revoked_at.is_(None)).count() == 25
        with pytest.raises(ValueError, match="full"):
            svc.invite_real_records("one-more@example.com", "operator")
    finally:
        svc.engine.dispose()


def test_a_join_never_reopens_or_rewrites_an_invite(svc_join):
    svc = svc_join
    svc.invite_real_records("x@example.com", "operator")
    assert svc.grant_join_invite("x@example.com", "acct_a") == "live"
    assert svc.revoke_real_records_invite("x@example.com")
    assert svc.grant_join_invite("x@example.com", "acct_a") == "revoked"
    assert _invite_row(svc, "x@example.com")[0] == "operator"
    assert _invite_row(svc, "x@example.com")[1] is not None


@pytest.fixture
def svc_join(tmp_path):
    from careagents.accounts import AccountService
    service = AccountService(Config(env={
        "CARE_DATABASE_URL": f"sqlite:///{tmp_path}/j.db",
        "OPENAI_API_KEY": "k"}))
    yield service
    service.engine.dispose()


def test_a_paused_inviter_passes_on_nothing(made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    svc.invite_real_records("inviter@example.com", "operator")
    _, code, _ = _inviter(app, svc, monkeypatch)
    svc.set_paused("inviter@example.com", True)
    _join_via(app, sent, code, "p@example.com", first_run=False)
    assert invites.join_outcome(svc.session, "p@example.com") == "none"


# --- #934 QA: the connect step in a confirm-link session --------------------

def test_a_link_session_is_sent_to_sign_in_before_connecting(
        made, sent, monkeypatch):  # noqa: F811
    """Every new joiner is in a confirm-link session when the card first
    shows (#934 QA F1). The card and the chat say "Sign in again", /auth
    asks for a code with the address filled in and returns to the card,
    and the connect refusal carries a sentence, never only a code."""
    from careagents import agent as agent_mod, tester_terms
    from tests.test_careagents import _turn

    class _Turn:
        text, tool_calls, raw_tool_calls = "an answer", [], []
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: _Turn())
    app, svc = made(RESEND_API_KEY="re_test", CARE_REAL_RECORDS="allowlist",
                    CARE_REAL_RECORDS_ALLOWLIST="inviter@example.com",
                    FASTEN_PUBLIC_KEY="pub123")
    _, code, _ = _inviter(app, svc, monkeypatch)
    c, r = _join_via(app, sent, code, "newbie@example.com")
    assert r.status_code == 303
    agent = re.search(r"agent=(\w+)", r.headers["Location"]).group(1)
    assert invites.join_outcome(svc.session, "newbie@example.com") == "granted"
    _turn(c, agent, "what do my labs say?")

    hub = c.get("/home").get_data(as_text=True)
    card = hub[hub.index('id="connect-records"'):]
    card = card[:card.index("</section>")]
    assert 'href="/auth?connect=1"' in card
    assert "Sign in again to connect your records" in card
    # The code is said on the card, before the tap (#937).
    assert 'id="connect-code-note"' in card
    assert "We'll email you a code to sign in." in " ".join(card.split())
    assert "connector-tile" not in card
    chat = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert 'href="/auth?connect=1"' in chat

    # A tap that reaches the server anyway is refused with a sentence.
    refused = c.post("/api/connections/fasten", json={
        "consent": True, "consent_version": tester_terms.CONSENT_VERSION})
    assert refused.status_code == 403
    assert " " in refused.get_json()["message"]

    page = c.get("/auth?connect=1").get_data(as_text=True)
    assert 'value="newbie@example.com"' in page
    assert 'data-back="/home#connect-records"' in page
    assert 'id="connect-why"' in page

    # The code sign-in ends the link session; the card is the real tile.
    _login(c, svc, monkeypatch, email="newbie@example.com")
    hub = c.get("/home").get_data(as_text=True)
    card = hub[hub.index('id="connect-records"'):]
    assert 'data-connector="fasten"' in card[:card.index("</section>")]
    ok = c.post("/api/connections/fasten", json={
        "consent": True, "consent_version": tester_terms.CONSENT_VERSION})
    assert ok.status_code == 200
    assert "/connect/" in ok.get_json()["connect_url"]


def test_connect_for_a_full_session_changes_nothing(made, monkeypatch):  # noqa: F811
    """`?connect=1` matters only in a link session."""
    app, svc = made()
    c = app.test_client()
    _login(c, svc, monkeypatch, email="full@example.com")
    r = c.get("/auth?connect=1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/home")


def _static(name):
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[1] / "careagents" /
            "static" / name).read_text()


def test_home_js_never_shows_a_bare_refusal_code():
    js = _static("home.js")
    assert "res.d.error ||" not in js
    assert 'refusal(res.d, "Couldn\'t connect that source.")' in js


def test_the_invite_page_says_records_can_follow_when_they_can(
        made, monkeypatch):  # noqa: F811
    app, svc = made(CARE_REAL_RECORDS="allowlist",
                    CARE_REAL_RECORDS_ALLOWLIST="inviter@example.com")
    _, code, _ = _inviter(app, svc, monkeypatch)
    html = app.test_client().get(f"/i?c={code}").get_data(as_text=True)
    assert 'id="beta-records-line"' in html and "not your own" not in html
    _, other, _ = _inviter(app, svc, monkeypatch, email="plain@example.com")
    html = app.test_client().get(f"/i?c={other}").get_data(as_text=True)
    assert "not your own" in html and 'id="beta-records-line"' not in html


def test_the_invite_banner_agrees_with_the_page(made, monkeypatch):  # noqa: F811
    """The banner said "made-up records only" above a page that offered
    your own records next (#934 QA)."""
    app, svc = made(CARE_REAL_RECORDS="allowlist",
                    CARE_REAL_RECORDS_ALLOWLIST="inviter@example.com")
    _, code, _ = _inviter(app, svc, monkeypatch)
    html = app.test_client().get(f"/i?c={code}").get_data(as_text=True)
    assert "made-up records first, then your own" in html
    assert "made-up records only" not in html
    _, other, _ = _inviter(app, svc, monkeypatch, email="plain@example.com")
    html = app.test_client().get(f"/i?c={other}").get_data(as_text=True)
    assert "made-up records only" in html


def test_the_connect_next_step_is_drawn_as_a_button():
    """`.connector-tile` resets colour and weight; the hub's primary next
    step must not render as a plain list row (#934 QA M1)."""
    css = _static("careagents.css")
    rule = css[css.index(".connector-tile.btn-primary {"):]
    rule = rule[:rule.index("}")]
    assert "background: var(--accent)" in rule
    assert "color: var(--on-accent)" in rule


def test_link_copied_is_not_drawn_as_an_error():
    assert 'classList.add("is-ok")' in _static("invite.js")
