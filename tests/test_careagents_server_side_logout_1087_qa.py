"""QA for #1094: sign-out ends the session on the server (#1087).

The PR's own tests drive only /home and /api/labs/timeline, which are both
behind login_required. The check sits in a before_request hook *because*
pages outside that gate read session["account_id"] too, and nothing pinned
that: moving the check into login_required kept every one of the PR's tests
green. These do, each with a positive control before sign-out so a refusal
cannot pass merely because the forged or copied session never worked.

Synthetic addresses only.
"""
from __future__ import annotations

import pytest

from careagents.models import Account, Connection
from tests.test_careagents import _login
from tests.test_careagents_beta_signup import made  # noqa: F401  (fixture)

pytestmark = pytest.mark.extension

EMAIL = "qa1094-owner@example.test"


def _copy(app, c):
    other = app.test_client()
    other.set_cookie("session", c.get_cookie("session").value)
    return other


def _generation(svc, email):
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).one().session_generation


def test_a_copied_owner_cookie_loses_the_owner_page(made, monkeypatch):  # noqa: F811
    """/admin/funnel is owner_only, not login_required."""
    app, svc = made(CARE_OWNER_EMAILS=EMAIL)
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    copy = _copy(app, c)
    assert copy.get("/admin/funnel").status_code == 200
    assert c.post("/logout").status_code == 302
    assert copy.get("/admin/funnel").status_code == 404
    assert copy.get("/admin/funnel/testers.csv").status_code == 404


def test_a_copied_cookie_is_signed_out_on_the_checkin_wallet(
        made, monkeypatch):  # noqa: F811
    """The wallet page and its records relay sign in where they stand,
    outside login_required (careagents/checkin_wallet.py)."""
    app, svc = made(CARE_CHECKIN_WALLET="1")
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    copy = _copy(app, c)
    assert 'data-signed-in="1"' in copy.get(
        "/checkin/wallet").get_data(as_text=True)
    c.post("/logout")
    assert 'data-signed-in="0"' in copy.get(
        "/checkin/wallet").get_data(as_text=True)
    assert copy.post("/checkin/wallet/records",
                     json={"resource_types": ["Patient"]}).status_code == 401


def test_a_copied_cookie_cannot_write_after_sign_out(made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    copy = _copy(app, c)
    c.post("/logout")
    assert copy.post("/api/connections/sample").status_code == 401
    with svc.session() as s:
        assert s.query(Connection).count() == 0


def test_a_stale_copy_cannot_sign_out_the_fresh_session(made, monkeypatch):  # noqa: F811
    """A cookie that is already signed out posts /logout: it ends nothing,
    so an old copy cannot keep signing its owner out of the new sign-in."""
    app, svc = made()
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    stale = _copy(app, c)
    c.post("/logout")
    _login(c, svc, monkeypatch, email=EMAIL)
    gen = _generation(svc, EMAIL)
    assert stale.post("/logout").status_code == 302
    assert _generation(svc, EMAIL) == gen
    assert c.get("/home").status_code == 200


def test_signing_in_again_does_not_revive_an_old_copy(made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    copy = _copy(app, c)
    c.post("/logout")
    _login(c, svc, monkeypatch, email=EMAIL)
    assert c.get("/home").status_code == 200
    assert copy.get("/home").status_code == 302


def test_two_sign_outs_both_count(made):  # noqa: F811
    app, svc = made()
    with svc.session() as s:
        s.add(Account(id="acct_qa1094", email="qa1094-twice@example.test"))
    svc.end_sessions("acct_qa1094")
    svc.end_sessions("acct_qa1094")
    assert svc.get_account("acct_qa1094").session_generation == 2
