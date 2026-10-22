# ruff: noqa: F811  (pytest fixtures shadowed by parameters)
"""Security-tester attacks on PR #983: the owner Testers table, its CSV,
the digest block and sign-in method tracking. Synthetic data only."""

from __future__ import annotations

import csv
import io
import time

import pytest

from careagents import beta_signup, funnel
from careagents.models import Account
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, cfg, svc)
from tests.test_careagents_funnel import _app
from tests.test_careagents_signins import _passkey

OWNER = "owner@example.com"
XSS = '<img src=x onerror="alert(1)">'


def _acct(svc, email):
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).one().id


def _owner_client(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=OWNER)
    return c


def _hostile_inviter(svc, first_name):
    """A confirmed beta request whose first name the tester typed, and an
    account invited by that tester."""
    inviter = svc.first_run_account("inviter@example.com")
    with svc.session() as s:
        req = s.query(beta_signup.BetaRequest).filter_by(
            email="inviter@example.com").first()
        if req is None:
            req = beta_signup.BetaRequest(
                email="inviter@example.com", first_name=first_name,
                confirmed_at=time.time())
            s.add(req)
        else:
            req.first_name, req.confirmed_at = first_name, time.time()
    invited = svc.first_run_account("invited@example.com")
    funnel.set_inviter(svc.session, invited.id, inviter.id)
    return inviter.id, invited.id


# --- the owner gate ---------------------------------------------------------

@pytest.mark.parametrize("path", ["/admin/funnel",
                                  "/admin/funnel/testers.csv"])
@pytest.mark.parametrize("email", ["owner+x@example.com",
                                   "owner@example.com.evil.test",
                                   "xowner@example.com"])
def test_alias_and_lookalike_owner_addresses_get_404(svc, monkeypatch,
                                                     path, email):
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    assert c.get(path).status_code == 404


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_a_non_owner_with_a_csrf_token_cannot_reach_the_csv(svc, monkeypatch,
                                                           method):
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    c = app.test_client()
    _login(c, svc, monkeypatch, email="tess@example.com")
    with c.session_transaction() as sess:
        token = sess.get("csrf_token") or sess.get("_csrf_token") or "x"
    r = getattr(c, method)("/admin/funnel/testers.csv",
                           headers={"X-CSRF-Token": token})
    assert r.status_code in (403, 404, 405)
    assert b"tess@example.com" not in r.data
    r = c.get("/admin/funnel/testers.csv", headers={"X-CSRF-Token": token})
    assert r.status_code == 404 and b"@" not in r.data


def test_the_csv_is_not_sniffable_and_is_text_csv(svc, monkeypatch):
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    r = _owner_client(app, svc, monkeypatch).get("/admin/funnel/testers.csv")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/csv")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"


# --- XSS through tester-typed fields -----------------------------------------

def test_a_hostile_inviter_first_name_is_escaped_on_the_page(svc,
                                                            monkeypatch):
    _hostile_inviter(svc, XSS)
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    html = _owner_client(app, svc, monkeypatch).get(
        "/admin/funnel").get_data(as_text=True)
    assert "Invite from &lt;img" in html
    assert "<img src=x" not in html


def test_a_hostile_email_row_is_escaped_on_the_page(svc, monkeypatch):
    acct = svc.first_run_account("plain@example.com")
    with svc.session() as s:
        s.get(Account, acct.id).email = f"{XSS}@example.com"
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    html = _owner_client(app, svc, monkeypatch).get(
        "/admin/funnel").get_data(as_text=True)
    assert "<img src=x" not in html


# --- CSV formula injection ---------------------------------------------------

@pytest.mark.parametrize("name", ["=1+1", "+1+1", "-1+1", "@SUM(1)",
                                  "\t=1+1", "\r=1+1",
                                  '=HYPERLINK("http://evil.test","x")'])
def test_every_formula_lead_is_neutralised_in_the_csv(svc, monkeypatch,
                                                      name):
    _hostile_inviter(svc, name)
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    body = _owner_client(app, svc, monkeypatch).get(
        "/admin/funnel/testers.csv").get_data(as_text=True)
    for row in csv.DictReader(io.StringIO(body)):
        for v in row.values():
            assert (v or "")[:1] not in ("=", "+", "-", "@", "\t", "\r"), v


def test_a_formula_email_is_neutralised_in_the_csv(svc, monkeypatch):
    acct = svc.first_run_account("plain@example.com")
    with svc.session() as s:
        s.get(Account, acct.id).email = "=cmd|'/c calc'!A1@example.com"
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    body = _owner_client(app, svc, monkeypatch).get(
        "/admin/funnel/testers.csv").get_data(as_text=True)
    emails = [r["email"] for r in csv.DictReader(io.StringIO(body))]
    assert "'=cmd|'/c calc'!A1@example.com" in emails


# --- the narrowed page tests: nothing outside the Testers section ----------

def test_an_email_appears_on_the_page_only_inside_the_testers_section(
        svc, monkeypatch):
    """Tighter than the narrowed tests, which leave the page head (before
    the Testers section) unchecked."""
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    _login(app.test_client(), svc, monkeypatch, email="tess@example.com")
    html = _owner_client(app, svc, monkeypatch).get(
        "/admin/funnel").get_data(as_text=True)
    before, rest = html.split('id="funnel-testers"', 1)
    section, after = rest.split('id="funnel-days"', 1)
    for email in ("tess@example.com", OWNER):
        assert email not in before, email
        assert email not in after, email
    assert "tess@example.com" in section


# --- what the table calls the last sign-in -----------------------------------

def test_a_consent_passkey_check_does_not_move_the_last_sign_in(
        svc, monkeypatch):
    """The consent check is not a sign-in and is not counted, but it still
    stamps last_login_at, so the owner's 'Last sign-in' column shows the
    consent time labelled with the previous sign-in's method."""
    from webauthn.helpers import bytes_to_base64url

    class AuthV:
        new_sign_count = 1
    app = _app(svc)
    _passkey(app, svc, monkeypatch, "pat@example.com")
    aid = _acct(svc, "pat@example.com")
    with svc.session() as s:
        s.get(Account, aid).last_login_at = 1_000_000.0
    monkeypatch.setattr("webauthn.verify_authentication_response",
                        lambda **k: AuthV())
    svc.finish_authentication(
        {"rawId": bytes_to_base64url(b"cred-signins")},
        bytes_to_base64url(b"challenge"), require_uv=True)
    with svc.session() as s:
        row = s.get(Account, aid)
        assert row.last_login_method == "email_code"
        assert row.last_login_at == 1_000_000.0, (
            "consent check stamped last_login_at; the Testers table now "
            "shows it as an 'email code' sign-in")
