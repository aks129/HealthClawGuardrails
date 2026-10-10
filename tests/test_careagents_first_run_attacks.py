"""Security review of #920 (confirm link signs in): exploit tests.

Each test is written as the attack, and asserts the safe outcome. A test
that was marked xfail(strict=True) pinned an open finding; F1, F1b and F2
are fixed (sessions a link opens are limited, and the confirm page names
the account), so the markers are off.

All data is synthetic. The canary clinic label never belongs to anyone.
"""

# The fixtures `made` and `sent` are imported, so each test's arguments
# read to ruff as redefinitions.
# ruff: noqa: F811
from __future__ import annotations

import re

from careagents.models import Account
from tests.test_careagents import _login
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _confirm_token, _post, made, sent)
from tests.test_careagents_first_run import _account, _ask, _signed_in_as

EMAIL = "avery@example.com"
CANARY = "Canary Clinic Zephyrine Q7"


def test_a_forwarded_link_session_does_not_see_records_connected_later(
        made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    # The owner requests a spot; the confirm mail is forwarded or leaked.
    owner = app.test_client()
    assert _post(owner, email=EMAIL).status_code == 200
    token = _confirm_token([m for m in sent if m[0] == EMAIL][-1][3])
    # Whoever holds the link opens it and presses Confirm in their browser.
    stranger = app.test_client()
    page = stranger.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    nonce = re.search(r'name="n" value="([^"]+)"', page).group(1)
    r = stranger.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 303
    acct_id = _account(svc).id
    assert _signed_in_as(stranger) == acct_id
    # Later the owner signs in with an email code and connects real
    # records (stand-in: a real-kind connection on the account).
    _login(owner, svc, monkeypatch, email=EMAIL)
    svc.add_connection(acct_id, "fasten", "t-canary-real", CANARY,
                       consent_version="v-test")
    # The stranger's cookie, untouched since the confirm, must not see it.
    home = stranger.get("/home")
    assert CANARY not in home.get_data(as_text=True)


def test_a_forwarded_link_session_cannot_enrol_a_passkey(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    stranger = app.test_client()
    token, nonce, _ = _ask(stranger, sent)
    assert stranger.post("/beta/confirm",
                         data={"t": token, "n": nonce}).status_code == 303
    r = stranger.post("/webauthn/register/options")
    assert r.status_code in (401, 403)


def test_the_confirm_page_says_whose_account_it_signs_in_to(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    attacker = app.test_client()
    # The attacker requests a spot with the victim's first name.
    assert _post(attacker, email="mallory@example.net",
                 first_name="Avery").status_code == 200
    token = _confirm_token(
        [m for m in sent if m[0] == "mallory@example.net"][-1][3])
    # The victim opens the link the attacker sent and reads the page.
    victim = app.test_client()
    page = victim.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    assert "Avery" in page
    nonce = re.search(r'name="n" value="([^"]+)"', page).group(1)
    # One tap and the victim is in the attacker's account.
    r = victim.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 303
    with svc.session() as s:
        mallory = s.query(Account).filter_by(
            email="mallory@example.net").first()
    assert _signed_in_as(victim) == mallory.id
    # The safe outcome: the page said whose account this is before the tap.
    assert "mallory@example.net" in page


def test_a_cross_site_post_plants_only_the_attackers_own_email(made, sent):
    """Recorded, not a finding: a nonce-less cross-site POST of the
    attacker's token stores the attacker's address as the victim's /auth
    prefill. A code typed there goes to the attacker; it signs nobody in
    and shows the victim nothing new."""
    app, svc = made(RESEND_API_KEY="re_test")
    attacker = app.test_client()
    token, _n, _ = _ask(attacker, sent, email="mallory@example.net")
    victim = app.test_client()
    victim.post("/beta/confirm", data={"t": token})
    assert _signed_in_as(victim) is None
    page = victim.get("/auth").get_data(as_text=True)
    assert 'value="mallory@example.net"' in page
    assert EMAIL not in page


def test_an_idn_link_makes_a_second_empty_account_not_the_one_with_records(
        made, sent, monkeypatch):
    """Recorded, Low: an account made at /auth keeps a Unicode domain as
    typed; the beta request keeps it in ASCII (ascii_email). The confirm
    looks up the ASCII form, misses the account with records, and makes a
    second, empty one. Same inbox, so nothing crosses people, and the
    records stay out of the link session."""
    app, svc = made(RESEND_API_KEY="re_test")
    owner = app.test_client()
    _login(owner, svc, monkeypatch, email="avery@b\u00fccher.de")
    owner.post("/api/connections/sample")
    c = app.test_client()
    assert _post(c, email="avery@b\u00fccher.de").status_code == 200
    token = _confirm_token(sent[-1][3])
    page = c.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    nonce = re.search(r'name="n" value="([^"]+)"', page).group(1)
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 303
    with svc.session() as s:
        emails = sorted(a.email for a in s.query(Account).all())
    assert emails == ["avery@bücher.de", "avery@xn--bcher-kva.de"]
    assert _signed_in_as(c) != _account(svc, "avery@bücher.de").id


def test_a_gmail_alias_link_makes_a_second_empty_account(
        made, sent, monkeypatch):
    """Recorded, Low: the same inbox under a Gmail alias gets a second,
    empty account rather than the one with records. Not cross-person (the
    mail reached that inbox), but `has_connections` is not keyed by the
    mailbox the rest of beta_signup uses."""
    app, svc = made(RESEND_API_KEY="re_test")
    owner = app.test_client()
    _login(owner, svc, monkeypatch, email="ave.ry@gmail.com")
    owner.post("/api/connections/sample")
    c = app.test_client()
    token, nonce, _ = _ask(c, sent, email="avery+beta@gmail.com")
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 303
    with svc.session() as s:
        n = s.query(Account).count()
    assert n == 2
    assert CANARY not in c.get("/home").get_data(as_text=True)
