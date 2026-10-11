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
    m = re.search(r"/i/([0-9a-f]{32})", html)
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
    assert c.get(f"/i/{code}", headers={"X-Real-IP": ip}).status_code == 200
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
    with svc.session() as s:
        row = s.get(RealRecordInvite, email)
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
    assert f"http://localhost/i/{first}" in html
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
    r = app.test_client().get(f"/i/{code}")
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
    html = app.test_client().get(f"/i/{code}").get_data(as_text=True)
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
        r = app.test_client().get(f"/i/{code}")
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
        assert c.get(f"/i/{i:032x}",
                     headers={"X-Real-IP": "198.51.100.4"}).status_code == 404
    r = c.get("/i/" + "1" * 32, headers={"X-Real-IP": "198.51.100.4"})
    assert r.status_code == 429
    # Another address is not held back by this one.
    assert c.get("/i/" + "1" * 32,
                 headers={"X-Real-IP": "198.51.100.5"}).status_code == 404
    report = funnel.daily(svc.session, days=1)
    assert report["days"][0]["rate_limited"] == 1


# --- joining through a link ---------------------------------------------------

def test_a_join_takes_a_seat_and_gets_the_real_record_invite(
        made, sent, monkeypatch):  # noqa: F811
    from careagents import funnel
    app, svc = made(RESEND_API_KEY="re_test")
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
    assert r.status_code == 200 and r.get_json() == {"ok": True}
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
    assert admitted.count("granted") == 2 and admitted.count(None) == 1
    assert app.test_client().get(f"/i/{code}").status_code == 404


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
    assert app.test_client().get(f"/i/{code}").status_code == 404


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
    for i in range(beta.STAGE1_INVITE_CAP):
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
        assert s.get(invites.InviteJoin, "stays@example.com") is not None


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
    assert app.test_client().get(f"/i/{code}").status_code == 404   # full
    r = runner.invoke(args=["invite-links", "cap", code, "default"])
    assert r.exit_code == 0 and _link(svc, code)["cap"] is None
    r = runner.invoke(args=["invite-links", "cap", code, "-1"])
    assert r.exit_code != 0
    r = runner.invoke(args=["invite-links", "revoke", code])
    assert r.exit_code == 0 and _link(svc, code)["revoked"]
    r = runner.invoke(args=["invite-links", "revoke", code])
    assert r.exit_code != 0                      # nothing left to revoke
    html = _login_settings(app, svc, monkeypatch)
    assert "turned off" in html and f"/i/{code}" not in html


def _login_settings(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch, email="inviter@example.com")
    return c.get("/settings").get_data(as_text=True)


@pytest.mark.parametrize("raw,want", [("7", 7), ("x", 5), ("-3", 0)])
def test_the_cap_setting(raw, want):
    assert Config(env={"CARE_INVITE_LINK_CAP": raw}).invite_link_cap == want
