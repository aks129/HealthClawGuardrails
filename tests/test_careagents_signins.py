# ruff: noqa: F811  (the fixtures are imported, then named as parameters)
"""Sign-ins counted by method, and the owner's testers list
(careagents/signins.py, /admin/funnel).

Owner decision 2026-10-16: tester emails may appear on the owner-only page,
its CSV and the owner's terminal. Never in the digest, never in a log.

Synthetic data only.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import os

import pytest
from sqlalchemy import Integer, String, create_engine, inspect, text

from careagents import funnel, models, signins
from careagents.models import Account, ActivityDay, Base, UsageDay
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)
from tests.test_careagents_beta_signup import made, sent  # noqa: F401
from tests.test_careagents_funnel import _app, _svc_cfg

OWNER = "owner@example.com"


def _acct_id(svc, email):
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).one().id


def _counts(svc, account_id):
    with svc.session() as s:
        return {(r.method, r.day): r.count for r in s.query(
            signins.SignInDay).filter_by(account_id=account_id)}


def _method(svc, account_id):
    with svc.session() as s:
        return s.get(Account, account_id).last_login_method


# --- the table holds counts only -----------------------------------------------

def test_the_sign_in_table_holds_only_its_listed_columns():
    allowed = {"id": String, "account_id": String, "day": String,
               "method": String, "count": Integer}
    table = Base.metadata.tables["ca_sign_in_days"]
    assert set(table.columns.keys()) == set(allowed)
    for col in table.columns:
        assert isinstance(col.type, allowed[col.name]), col.name
        assert not isinstance(col.type, String) or col.type.length <= 32
    # No address, agent string or device: the account's own row gains the
    # method name and nothing else.
    assert "last_login_method" in Base.metadata.tables["ca_accounts"].columns


def test_an_unknown_method_is_refused(svc):
    with pytest.raises(ValueError):
        signins.count(svc.session, "acct_x", "magic_link")


# --- every sign-in is recorded with its method ---------------------------------

def test_an_email_code_sign_in_is_counted_as_email_code(app, svc,
                                                        monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch, email="ann@example.com")
    _login(app.test_client(), svc, monkeypatch, email="ann@example.com")
    aid = _acct_id(svc, "ann@example.com")
    assert _counts(svc, aid) == {("email_code", funnel.utc_day()): 2}
    assert _method(svc, aid) == "email_code"


def test_a_wrong_code_is_not_a_sign_in(app, svc, monkeypatch):
    _login(app.test_client(), svc, monkeypatch, email="ann@example.com")
    aid = _acct_id(svc, "ann@example.com")
    c = app.test_client()
    c.post("/api/auth/email", json={"email": "ann@example.com"})
    r = c.post("/api/auth/verify", json={"email": "ann@example.com",
                                         "code": "00000000"})
    assert r.status_code == 400
    assert sum(_counts(svc, aid).values()) == 1


def _passkey(app, svc, monkeypatch, email):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)

    class RegV:
        credential_id = b"cred-signins"
        credential_public_key = b"pk"
        sign_count = 0

    class AuthV:
        new_sign_count = 1
    monkeypatch.setattr("webauthn.verify_registration_response",
                        lambda **k: RegV())
    monkeypatch.setattr("webauthn.verify_authentication_response",
                        lambda **k: AuthV())
    c.post("/webauthn/register/options")
    assert c.post("/webauthn/register/verify", json={
        "id": "a", "rawId": "a", "type": "public-key",
        "response": {}}).status_code == 200
    return c


def test_a_passkey_sign_in_is_counted_as_passkey(app, svc, monkeypatch):
    from webauthn.helpers import bytes_to_base64url
    _passkey(app, svc, monkeypatch, "pat@example.com")
    c2 = app.test_client()
    c2.post("/webauthn/login/options")
    r = c2.post("/webauthn/login/verify", json={
        "rawId": bytes_to_base64url(b"cred-signins"), "id": "x",
        "type": "public-key", "response": {}})
    assert r.status_code == 200
    aid = _acct_id(svc, "pat@example.com")
    day = funnel.utc_day()
    assert _counts(svc, aid) == {("email_code", day): 1, ("passkey", day): 1}
    assert _method(svc, aid) == "passkey"


def test_the_consent_passkey_check_is_not_a_sign_in(svc, monkeypatch):
    """The consent screen asks for a fresh passkey to prove the person is
    present. It makes no session, so it is not counted."""
    from webauthn.helpers import bytes_to_base64url

    class AuthV:
        new_sign_count = 1
    app = _app(svc)
    _passkey(app, svc, monkeypatch, "pat@example.com")
    aid = _acct_id(svc, "pat@example.com")
    monkeypatch.setattr("webauthn.verify_authentication_response",
                        lambda **k: AuthV())
    svc.finish_authentication(
        {"rawId": bytes_to_base64url(b"cred-signins")},
        bytes_to_base64url(b"challenge"), require_uv=True)
    assert _counts(svc, aid) == {("email_code", funnel.utc_day()): 1}
    assert _method(svc, aid) == "email_code"


def test_approving_on_the_consent_screen_is_not_a_sign_in(monkeypatch):
    """Through the real /authorize/decide route, only the WebAuthn crypto
    faked: the approval succeeds and no passkey sign-in is counted."""
    from webauthn.helpers import bytes_to_base64url

    from careagents.accounts import AccountService
    from careagents.app import create_app
    from tests.test_careagents_consent import _approve, _cfg, _connect

    class AuthV:
        new_sign_count = 1
    cfg_ = _cfg()
    svc_ = AccountService(cfg_)
    try:
        app = create_app(config=cfg_, client=FakeClient(), accounts=svc_)
        app.config["TESTING"] = True
        c = _passkey(app, svc_, monkeypatch, "pat@example.com")
        aid = _acct_id(svc_, "pat@example.com")
        conn = _connect(svc_, svc_.get_account(aid))
        monkeypatch.setattr("webauthn.verify_authentication_response",
                            lambda **k: AuthV())
        with c.session_transaction() as sess:
            sess["wa_consent_challenge"] = bytes_to_base64url(b"c")
        r = _approve(c, conn, passkey={
            "rawId": bytes_to_base64url(b"cred-signins")})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert _counts(svc_, aid) == {("email_code", funnel.utc_day()): 1}
        assert _method(svc_, aid) == "email_code"
    finally:
        svc_.engine.dispose()


def test_a_beta_confirm_link_is_counted_as_signup_confirm(svc):
    acct = svc.first_run_account("new@example.com")
    assert _counts(svc, acct.id) == {("signup_confirm", funnel.utc_day()): 1}
    assert _method(svc, acct.id) == "signup_confirm"


def test_the_confirm_route_counts_signup_confirm(made, sent):
    """End to end through /beta and the confirm page, as a tester joins."""
    import re

    from tests.test_careagents_beta_signup import _confirm_token, _post
    app, svc_ = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _post(c, email="avery@example.com")
    t = _confirm_token(sent[0][3])
    page = c.get(f"/beta/confirm?t={t}").get_data(as_text=True)
    n = re.search(r'name="n" value="([^"]+)"', page).group(1)
    c.post("/beta/confirm", data={"t": t, "n": n})
    aid = _acct_id(svc_, "avery@example.com")
    assert set(_counts(svc_, aid)) == {("signup_confirm", funnel.utc_day())}
    assert _method(svc_, aid) == "signup_confirm"
    row = signins.testers(svc_.session)[0]
    assert (row["joined_via"], row["joined_label"]) == ("beta",
                                                        "/beta request")


def test_a_confirm_link_opened_while_signed_in_is_not_a_sign_in(
        made, sent, monkeypatch):
    """Already signed in here as the same address: the confirm link keeps
    that session (app.py _first_run, no _login), so nothing is counted and
    the last sign-in and its method stay as they were."""
    import re

    from tests.test_careagents_beta_signup import _confirm_token, _post
    app, svc_ = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _login(c, svc_, monkeypatch, email="avery@example.com")
    aid = _acct_id(svc_, "avery@example.com")
    with svc_.session() as s:
        s.get(Account, aid).last_login_at = 1_000_000.0
    _post(c, email="avery@example.com")
    t = _confirm_token(sent[-1][3])
    page = c.get(f"/beta/confirm?t={t}").get_data(as_text=True)
    n = re.search(r'name="n" value="([^"]+)"', page).group(1)
    c.post("/beta/confirm", data={"t": t, "n": n})
    assert _counts(svc_, aid) == {("email_code", funnel.utc_day()): 1}
    assert _method(svc_, aid) == "email_code"
    with svc_.session() as s:
        assert s.get(Account, aid).last_login_at == 1_000_000.0


def test_an_invite_link_join_is_a_confirm_sign_in_joined_by_invite(
        made, sent, monkeypatch):
    """An invite link is not its own sign-in method: the link opens /beta
    and the sign-in is the confirm. The list says who invited them."""
    from tests.test_careagents_invite_links import _inviter, _join_via
    app, svc_ = made(RESEND_API_KEY="re_test")
    _, code, inviter = _inviter(app, svc_, monkeypatch)
    _join_via(app, sent, code, "robin@example.com")
    aid = _acct_id(svc_, "robin@example.com")
    assert set(_counts(svc_, aid)) == {("signup_confirm", funnel.utc_day())}
    row = next(r for r in signins.testers(svc_.session)
               if r["email"] == "robin@example.com")
    assert row["joined_via"] == "invite"
    assert row["joined_label"].startswith("Invite")


def test_a_failed_count_never_fails_the_sign_in(app, svc, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(signins, "count", boom)
    assert _login(app.test_client(), svc, monkeypatch)["ok"] is True


def test_racing_counts_land_on_one_row(tmp_path):
    import threading

    from careagents.accounts import AccountService
    from careagents.config import Config
    svc_ = AccountService(Config(env={
        "CARE_DATABASE_URL": f"sqlite:///{tmp_path}/race.db",
        "OPENAI_API_KEY": "k"}))
    try:
        acct = svc_.first_run_account("race@example.com")
        start = threading.Barrier(6)

        def go():
            start.wait()
            signins.count(svc_.session, acct.id, "email_code")
        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert _counts(svc_, acct.id)[("email_code", funnel.utc_day())] == 6
    finally:
        svc_.engine.dispose()


def test_the_rows_go_with_the_account(svc):
    acct = svc.first_run_account("gone@example.com")
    signins.count(svc.session, acct.id, "email_code")
    assert svc.delete_account(acct.id)
    with svc.session() as s:
        assert s.query(signins.SignInDay).count() == 0


# --- the testers list ------------------------------------------------------------

NOW = dt.datetime(2026, 10, 16, 12, tzinfo=dt.timezone.utc)


def _day(days_ago):
    return funnel.utc_day(NOW - dt.timedelta(days=days_ago))


def _ts(days_ago):
    return (NOW - dt.timedelta(days=days_ago)).timestamp()


def _make(svc, email, joined_days_ago):
    acct = svc.first_run_account(email)
    with svc.session() as s:
        row = s.get(Account, acct.id)
        row.created_at = _ts(joined_days_ago)
        row.last_login_at = _ts(joined_days_ago)
        f = s.get(funnel.FunnelAccount, acct.id)
        f.joined = _ts(joined_days_ago)
        # Each test sets the sign-ins it reads.
        s.query(signins.SignInDay).filter_by(account_id=acct.id).delete()
    return acct.id


def test_testers_are_newest_first_with_how_they_joined(svc, monkeypatch):
    from careagents import invites
    old = _make(svc, "old@example.com", 9)
    new = _make(svc, "new@example.com", 1)
    funnel.mark(svc.session, old, "confirmed", at=_ts(9))
    funnel.set_inviter(svc.session, new, old)
    monkeypatch.setattr(invites, "inviter_first_name",
                        lambda s, a: "Olive" if a == old else None)
    rows = signins.testers(svc.session, moment=NOW)
    assert [r["email"] for r in rows] == ["new@example.com",
                                           "old@example.com"]
    assert rows[0]["joined_via"] == "invite"
    assert rows[0]["inviter"] == "Olive"
    assert rows[0]["joined_label"] == "Invite from Olive"
    assert rows[1]["joined_via"] == "beta"
    assert rows[1]["joined_label"] == "/beta request"


def test_an_email_code_account_joined_through_get_started(svc, monkeypatch):
    from tests.test_careagents import _make_account
    _make_account(svc, monkeypatch, "gs@example.com")
    row = signins.testers(svc.session)[0]
    assert row["joined_via"] == "get_started"
    assert row["joined_label"] == "Get started"
    assert row["last_method"] == "email_code"


def test_sign_ins_last_seven_days_and_last_active(svc):
    aid = _make(svc, "busy@example.com", 20)
    for days_ago, method in ((0, "passkey"), (6, "email_code"),
                             (7, "email_code")):
        signins.count(svc.session, aid, method,
                      moment=NOW - dt.timedelta(days=days_ago))
    with svc.session() as s:
        s.add(UsageDay(account_id=aid, day=_day(5), turns=3))
        s.add(ActivityDay(account_id=aid, day=_day(2), asked=0, approved=1))
    funnel.mark(svc.session, aid, "first_answer", at=_ts(5))
    row = signins.testers(svc.session, moment=NOW)[0]
    assert row["sign_ins_7d"] == 2
    assert row["last_active"] == _day(2)
    assert row["idle_days"] == 2
    assert row["furthest"] == "first_answer"
    assert "first_answer" in row["milestones"]
    assert row["stuck"] is False


def test_joined_but_never_asked_is_stuck(svc):
    _make(svc, "quiet@example.com", 0)
    row = signins.testers(svc.session, moment=NOW)[0]
    assert row["last_active"] is None
    assert row["stuck"] is True
    assert row["stuck_reason"] == "never asked"


def test_three_days_away_is_stuck(svc):
    aid = _make(svc, "away@example.com", 10)
    with svc.session() as s:
        s.add(UsageDay(account_id=aid, day=_day(3), turns=1))
    funnel.mark(svc.session, aid, "first_answer", at=_ts(3))
    row = signins.testers(svc.session, moment=NOW)[0]
    assert row["idle_days"] == 3
    assert row["stuck"] is True
    assert row["stuck_reason"] == "3 days away"


def test_the_list_is_capped(svc):
    for i in range(4):
        _make(svc, f"t{i}@example.com", i)
    assert len(signins.testers(svc.session, moment=NOW, limit=3)) == 3


# --- the owner page and its CSV --------------------------------------------------

def test_the_owner_sees_tester_emails_on_the_page(svc, monkeypatch):
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    _login(app.test_client(), svc, monkeypatch, email="tess@example.com")
    c = app.test_client()
    _login(c, svc, monkeypatch, email=OWNER)
    r = c.get("/admin/funnel")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    assert "Testers" in html and "tess@example.com" in html
    assert "email code" in html


@pytest.mark.parametrize("path", ["/admin/funnel",
                                  "/admin/funnel/testers.csv"])
def test_non_owners_get_404(svc, monkeypatch, path):
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    assert app.test_client().get(path).status_code == 404
    c = app.test_client()
    _login(c, svc, monkeypatch, email="tess@example.com")
    assert c.get(path).status_code == 404
    # Unset: closed for the owner too.
    closed = _app(svc)
    c = closed.test_client()
    _login(c, svc, monkeypatch, email=OWNER)
    assert c.get(path).status_code == 404


def test_a_confirm_link_session_is_not_enough_for_the_csv(svc, monkeypatch):
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    acct = svc.first_run_account(OWNER)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["account_id"] = acct.id
        from careagents.app import _VIA_LINK
        sess[_VIA_LINK] = True
    assert c.get("/admin/funnel/testers.csv").status_code == 404


def test_the_csv_is_owner_only_no_store_and_has_the_rows(svc, monkeypatch):
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    _login(app.test_client(), svc, monkeypatch, email="tess@example.com")
    c = app.test_client()
    _login(c, svc, monkeypatch, email=OWNER)
    r = c.get("/admin/funnel/testers.csv")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    assert r.mimetype == "text/csv"
    assert "attachment" in r.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    assert {row["email"] for row in rows} == {"tess@example.com", OWNER}
    tess = next(row for row in rows if row["email"] == "tess@example.com")
    assert tess["last_method"] == "email_code"
    assert tess["sign_ins_7d"] == "1"


def test_the_csv_neutralises_spreadsheet_formulas(svc, monkeypatch):
    from careagents import invites
    old = _make(svc, "old@example.com", 9)
    new = _make(svc, "new@example.com", 1)
    funnel.set_inviter(svc.session, new, old)
    monkeypatch.setattr(invites, "inviter_first_name",
                        lambda s, a: "=HYPERLINK(1)")
    body = signins.testers_csv(signins.testers(svc.session, moment=NOW))
    assert "'=HYPERLINK(1)" in body
    assert ",=HYPERLINK" not in body


# --- never in the digest or the logs ------------------------------------------

CANARY = "canary-tester-7731@example.com"


def test_the_digest_counts_sign_ins_by_method_and_names_nobody(svc,
                                                               monkeypatch):
    from careagents import mail
    aid = _make(svc, CANARY, 3)
    other = _make(svc, "second@example.com", 3)
    yesterday = NOW - dt.timedelta(days=1)
    signins.count(svc.session, aid, "email_code", moment=yesterday)
    signins.count(svc.session, aid, "email_code", moment=yesterday)
    signins.count(svc.session, other, "passkey", moment=yesterday)
    signins.count(svc.session, other, "passkey", moment=NOW)
    sent = []
    monkeypatch.setattr(
        mail, "send_message",
        lambda _c, to, subject, html, text, reply_to=None:
        sent.append((subject, html, text)) or mail.SENT)
    cfg_ = _svc_cfg(svc, CARE_DIGEST_TO=OWNER, RESEND_API_KEY="re_x")
    assert funnel.send_digest_if_due(svc.session, cfg_, NOW) == mail.SENT
    subject, html, body = sent[0]
    assert "Sign-ins" in body
    assert "email_code: 2" in body
    assert "passkey: 1" in body
    assert "signup_confirm: 0" in body
    assert "accounts signed in: 2" in body
    for part in (subject, html, body):
        assert CANARY not in part
        assert "second@example.com" not in part
        assert aid not in part


def test_no_tester_email_reaches_the_logs(svc, monkeypatch, caplog):
    from careagents import mail
    caplog.set_level(logging.DEBUG)
    app = _app(svc, CARE_OWNER_EMAILS=OWNER, CARE_DIGEST_TO=OWNER,
               RESEND_API_KEY="re_x")
    monkeypatch.setattr(mail, "send_message", lambda *a, **k: mail.SENT)
    _login(app.test_client(), svc, monkeypatch, email=CANARY)
    c = app.test_client()
    _login(c, svc, monkeypatch, email=OWNER)
    caplog.clear()
    assert CANARY in c.get("/admin/funnel").get_data(as_text=True)
    assert CANARY in c.get("/admin/funnel/testers.csv").get_data(as_text=True)
    funnel.send_digest_if_due(svc.session, _svc_cfg(
        svc, CARE_DIGEST_TO=OWNER, RESEND_API_KEY="re_x"))
    app.test_cli_runner().invoke(args=["testers", "list"])
    assert CANARY not in caplog.text


# --- the owner CLI ---------------------------------------------------------------

def test_flask_testers_list_prints_the_table(svc, monkeypatch):
    app = _app(svc)
    _login(app.test_client(), svc, monkeypatch, email="tess@example.com")
    out = app.test_cli_runner().invoke(args=["testers", "list"]).output
    assert "tess@example.com" in out
    assert "email_code" in out
    assert "Get started" in out


# --- the startup migration -------------------------------------------------------

@pytest.fixture
def url(tmp_path):
    env = os.environ.get("CARE_TEST_DATABASE_URL", "")
    if env and not env.startswith("sqlite"):
        engine = create_engine(env)
        models.Base.metadata.drop_all(engine)
        engine.dispose()
        return env
    return f"sqlite:///{tmp_path / 'legacy.db'}"


def test_an_existing_account_gains_an_empty_method_column(url):
    legacy = create_engine(url)
    with legacy.begin() as conn:
        conn.execute(text(
            "CREATE TABLE ca_accounts (id VARCHAR(32) PRIMARY KEY, "
            "email VARCHAR(255) NOT NULL UNIQUE, email_verified_at FLOAT, "
            "created_at FLOAT, last_login_at FLOAT)"))
        conn.execute(text(
            "INSERT INTO ca_accounts (id, email, created_at) "
            "VALUES ('acct_legacy', 'legacy@example.com', 1.0)"))
    engine = models.make_engine(url)
    insp = inspect(engine)
    assert "last_login_method" in {
        c["name"] for c in insp.get_columns("ca_accounts")}
    assert "ca_sign_in_days" in insp.get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT last_login_method FROM ca_accounts "
            "WHERE id = 'acct_legacy'")).one()[0] is None
    # A second boot (the worker, after the web) changes nothing.
    models.make_engine(url).dispose()
    engine.dispose()
    legacy.dispose()
