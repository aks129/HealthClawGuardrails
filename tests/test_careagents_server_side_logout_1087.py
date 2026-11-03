"""Signing out ends the session on the server, not just in the browser (#1087).

Each account carries a session generation; a sign-in stamps the current value
into the session, and /logout adds one. A cookie from before the sign-out (a
copy, an older tab, another device) then reads as signed out everywhere a
page looks at `session["account_id"]`, not only behind login_required.

The schema test uses the calm hub schema tests' fixture, so it runs on
Postgres when CARE_TEST_DATABASE_URL names one.

Synthetic addresses only.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from careagents import models
from careagents.app import _SESSION_GEN, _VIA_LINK
from tests.test_careagents import _login
from tests.test_careagents_beta_signup import made  # noqa: F401  (fixture)

pytestmark = pytest.mark.extension

EMAIL = "logout1087@example.test"


def _signed_in(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    assert c.get("/home").status_code == 200
    return c


def _copy(app, c):
    other = app.test_client()
    other.set_cookie("session", c.get_cookie("session").value)
    return other


def test_a_pre_logout_cookie_copy_is_signed_out_on_home(made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    copy = _copy(app, c)
    assert copy.get("/home").status_code == 200
    assert c.post("/logout").status_code == 302
    r = copy.get("/home")
    assert r.status_code == 302 and r.headers["Location"].endswith("/auth")
    # Signed out for the API, too.
    assert copy.get("/api/labs/timeline").status_code == 401


def test_sign_out_ends_the_other_devices_too(made, monkeypatch):  # noqa: F811
    app, svc = made()
    phone = _signed_in(app, svc, monkeypatch)
    laptop = app.test_client()
    _login(laptop, svc, monkeypatch, email=EMAIL)
    assert laptop.post("/logout").status_code == 302
    assert phone.get("/home").status_code == 302


def test_signing_in_again_after_sign_out_works(made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    c.post("/logout")
    _login(c, svc, monkeypatch, email=EMAIL)
    assert c.get("/home").status_code == 200


def test_a_session_from_before_the_generation_signs_in_again(
        made, monkeypatch):  # noqa: F811
    """The pre-deploy choice: a session with no generation is signed out,
    so a cookie copied before this shipped cannot outlive it."""
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    with c.session_transaction() as s:
        s.pop(_SESSION_GEN)
    assert c.get("/home").status_code == 302
    with c.session_transaction() as s:
        assert "account_id" not in s


def test_a_confirm_link_sign_out_ends_the_other_sessions(made, monkeypatch):  # noqa: F811
    """A confirm-link session's sign-out ends every session, as any other
    does: sparing them would leave its own copied cookies working (#1094)."""
    app, svc = made()
    phone = _signed_in(app, svc, monkeypatch)
    with phone.session_transaction() as s:
        aid, gen = s["account_id"], s[_SESSION_GEN]
    link = app.test_client()
    with link.session_transaction() as s:
        s["account_id"], s[_SESSION_GEN], s[_VIA_LINK] = aid, gen, True
    assert link.get("/home").status_code == 200
    assert link.post("/logout").status_code == 302
    assert link.get("/home").status_code == 302
    assert svc.get_account(aid).session_generation == gen + 1
    assert phone.get("/home").status_code == 302


def test_a_static_file_reads_no_account(made, monkeypatch):  # noqa: F811
    """No lookup, so a database that is down cannot fail a stylesheet."""
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)

    def down(_aid):
        raise AssertionError("a static file read an account")
    monkeypatch.setattr(svc, "get_account", down)
    r = c.get("/static/auth.js")
    assert r.status_code == 200
    r.close()


def test_a_lookup_that_fails_does_not_let_a_signed_out_copy_in(
        made, monkeypatch):  # noqa: F811
    """Fails closed. The hook's lookup fails once, as on a database hiccup;
    the copy taken before sign-out gets an error page, not the hub. Caught
    and skipped instead, the next lookup (login_required's) would succeed
    and trust the stale cookie."""
    from sqlalchemy.exc import OperationalError
    app, svc = made()
    app.config["PROPAGATE_EXCEPTIONS"] = False
    c = _signed_in(app, svc, monkeypatch)
    copy = _copy(app, c)
    c.post("/logout")
    real = svc.get_account
    calls = {"n": 0}

    def hiccup(aid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("SELECT ca_accounts", {}, Exception("down"))
        return real(aid)
    monkeypatch.setattr(svc, "get_account", hiccup)
    r = copy.get("/home")
    body = r.get_data(as_text=True)
    assert 500 <= r.status_code < 600
    assert "Your home page" not in body and "invite-section" not in body


def test_a_sign_out_everywhere_logs_a_count_and_nothing_else(
        made, monkeypatch, caplog):  # noqa: F811
    import logging
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    with c.session_transaction() as s:
        aid = s["account_id"]
    with caplog.at_level(logging.INFO, logger="careagents.app"):
        c.post("/logout")
    lines = [r.getMessage() for r in caplog.records
             if r.name == "careagents.app" and "sign-out" in r.getMessage()]
    assert lines == ["sign-out ended every session (1 account)"]
    assert aid not in caplog.text and EMAIL not in caplog.text


def test_sign_out_without_a_session_is_harmless(made):  # noqa: F811
    app, _ = made()
    assert app.test_client().post("/logout").status_code == 302


# --- schema ------------------------------------------------------------------

@pytest.fixture
def url(tmp_path):
    env = os.environ.get("CARE_TEST_DATABASE_URL", "")
    if env and not env.startswith("sqlite"):
        engine = create_engine(env)
        models.Base.metadata.drop_all(engine)
        engine.dispose()
        return env
    return f"sqlite:///{tmp_path / 'legacy.db'}"


def test_an_existing_account_gains_generation_zero(url):
    legacy = create_engine(url)
    with legacy.begin() as conn:
        conn.execute(text(
            "CREATE TABLE ca_accounts (id VARCHAR(32) PRIMARY KEY, "
            "email VARCHAR(255) NOT NULL UNIQUE, email_verified_at FLOAT, "
            "created_at FLOAT, last_login_at FLOAT)"))
        conn.execute(text(
            "INSERT INTO ca_accounts (id, email, created_at) "
            "VALUES ('acct_legacy', 'legacy1087@example.test', 1.0)"))
    engine = models.make_engine(url)
    cols = {c["name"] for c in inspect(engine).get_columns("ca_accounts")}
    assert "session_generation" in cols
    with engine.connect() as conn:
        gen = conn.execute(text(
            "SELECT session_generation FROM ca_accounts "
            "WHERE id = 'acct_legacy'")).scalar_one()
    assert gen == 0
    # A peer process that added it first is not an error.
    models._add_column(engine, "ca_accounts", "session_generation",
                       "INTEGER NOT NULL DEFAULT 0")
    engine.dispose()
    legacy.dispose()


# --- concurrency -------------------------------------------------------------

def test_sign_outs_at_once_each_count(made, tmp_path):  # noqa: F811
    """end_sessions is one UPDATE, so N at once on one account add N. A
    read-then-write would let two read the same value and lose one. A file
    database, so every thread sees the same rows; Postgres when
    CARE_TEST_DATABASE_URL names one."""
    import threading

    from careagents.models import Account
    env = os.environ.get("CARE_TEST_DATABASE_URL", "")
    extra = ({} if env and not env.startswith("sqlite")
             else {"CARE_DATABASE_URL": f"sqlite:///{tmp_path / 'gen.db'}"})
    _app, svc = made(**extra)
    with svc.session() as s:
        s.add(Account(id="acct_gen_race", email="genrace1087@example.test"))
    n = 8
    barrier = threading.Barrier(n)
    errors = []

    def out():
        barrier.wait()
        try:
            svc.end_sessions("acct_gen_race")
        except Exception as exc:              # pragma: no cover - reported
            errors.append(repr(exc))
    ts = [threading.Thread(target=out) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert svc.get_account("acct_gen_race").session_generation == n
