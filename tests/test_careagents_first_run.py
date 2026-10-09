"""One sign-up: confirming a beta request signs a brand-new tester in and
opens a chat on made-up records.

A patient-tester walk at 375px counted ~17 taps, 10 screens and three
inbox trips from the invite to a first answer: the /beta form only listed
you, the confirmed page was seven steps to read, and /auth asked for the
same email again behind a passkey button a newcomer does not have.

The confirm POST is already proof that the mailbox's owner pressed a
button (a 256-bit, single-use, hashed token that only reached that inbox;
mail scanners only GET). So it now signs that person in, but only:
- on a new request (`join`), never on a change to one;
- when the form came from our own confirm page in this browser (a nonce
  in the session, so a cross-site form cannot sign a victim into an
  attacker's account: login CSRF);
- when the email has no account yet, or one with nothing connected, so a
  forwarded link can never open records that are already there;
- and never by swapping out somebody else who is signed in here.
Anything else confirms as before and ends with one button to /auth, the
email filled in and the email-code step first.

All data is synthetic.
"""

# The fixtures `made` and `sent` are imported, so each test's arguments
# read to ruff as redefinitions.
# ruff: noqa: F811
from __future__ import annotations

import re

import pytest

from careagents.models import Account, Connection
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _confirm_token, _post, made, sent)
from tests.test_careagents import _login

EMAIL = "avery@example.com"


def _ask(c, sent, email=EMAIL, ip="203.0.113.7"):
    """Submit /beta and open the confirm link. (token, nonce, page)."""
    assert _post(c, ip=ip, email=email).status_code == 200
    mine = [m for m in sent if m[0] == email]
    token = _confirm_token(mine[-1][3])
    page = c.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    m = re.search(r'name="n" value="([^"]+)"', page)
    return token, (m.group(1) if m else None), page


def _account(svc, email=EMAIL):
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).first()


def _signed_in_as(c):
    with c.session_transaction() as s:
        return s.get("account_id")


def test_confirming_signs_a_new_tester_in_and_opens_the_sample_chat(
        made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    token, nonce, _ = _ask(c, sent)
    assert nonce
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 303
    assert re.fullmatch(r"/chat\?agent=[\w-]+", r.headers["Location"])
    acct = _account(svc)
    assert acct is not None and acct.email_verified_at
    assert _signed_in_as(c) == acct.id
    with svc.session() as s:
        kinds = [x.kind for x in
                 s.query(Connection).filter_by(account_id=acct.id)]
    assert kinds == ["sample"]
    # The chat it lands on: made-up records, suggested questions visible.
    page = c.get(r.headers["Location"]).get_data(as_text=True)
    assert 'class="beta-banner sample-banner"' in page
    assert 'id="starters"' in page and "hidden" not in re.search(
        r'<div class="starters"[^>]*>', page).group(0)
    # The welcome email still goes, with the steps for coming back.
    assert any(m[1].startswith("You're in") for m in sent)


def test_the_confirm_page_shows_the_terms_before_the_button(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    _, _, page = _ask(app.test_client(), sent)
    terms = page.index("By confirming you agree to the")
    assert terms < page.index('type="submit">Confirm')
    assert re.search(r'href="[^"]*/terms"', page)
    assert re.search(r'href="[^"]*/privacy"', page)


def test_a_cross_site_form_does_not_sign_anyone_in(made, sent):
    """Login CSRF: an attacker's page posts the attacker's own token from
    the victim's browser. No nonce from our page, so no session; the
    request is still confirmed, as before."""
    app, svc = made(RESEND_API_KEY="re_test")
    attacker = app.test_client()
    token, _nonce, _ = _ask(attacker, sent)
    victim = app.test_client()
    r = victim.post("/beta/confirm", data={"t": token})
    assert r.status_code == 200
    assert _signed_in_as(victim) is None
    assert _account(svc) is None
    body = r.get_data(as_text=True)
    assert "You're confirmed" in body


@pytest.mark.parametrize("nonce", ["", "x" * 22, "wrong", "ñønce-✓"])
def test_a_wrong_nonce_does_not_sign_in(made, sent, nonce):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    token, _real, _ = _ask(c, sent)
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 200
    assert _signed_in_as(c) is None and _account(svc) is None


def test_the_nonce_is_spent_with_the_first_post(made, sent):
    """A nonce signs in once: any POST spends it, even one whose token
    fails, so a replayed form cannot sign in later."""
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    token, nonce, _ = _ask(c, sent)
    assert c.post("/beta/confirm",
                  data={"t": "not-the-token", "n": nonce}).status_code == 410
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 200
    assert _signed_in_as(c) is None and _account(svc) is None
    # And a spent token is gone.
    assert c.post("/beta/confirm",
                  data={"t": token, "n": nonce}).status_code == 410


def test_an_account_with_records_is_not_signed_in_by_a_link(
        made, sent, monkeypatch):
    """A forwarded confirm link must not open records that are already
    there. The account signs in the usual way."""
    app, svc = made(RESEND_API_KEY="re_test")
    other = app.test_client()
    _login(other, svc, monkeypatch, email=EMAIL)
    other.post("/api/connections/sample")
    c = app.test_client()
    token, nonce, _ = _ask(c, sent)
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 200
    assert _signed_in_as(c) is None
    body = r.get_data(as_text=True)
    assert 'href="/auth"' in body


def test_an_empty_account_is_signed_in(made, sent, monkeypatch):
    """Signed up at /auth earlier and never connected anything: nothing to
    expose, so the confirm signs in to that same account."""
    app, svc = made(RESEND_API_KEY="re_test")
    _login(app.test_client(), svc, monkeypatch, email=EMAIL)
    before = _account(svc).id
    c = app.test_client()
    token, nonce, _ = _ask(c, sent)
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 303
    assert _signed_in_as(c) == before


def test_someone_else_signed_in_here_is_not_swapped_out(
        made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="sam@example.com")
    sam = _signed_in_as(c)
    token, nonce, _ = _ask(c, sent)
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 200
    assert _signed_in_as(c) == sam
    assert _account(svc) is None
    # The one button must not open Sam's hub for Avery: it signs Sam out.
    body = r.get_data(as_text=True)
    assert 'href="/home"' not in body.split('class="setup-card')[1]
    assert re.search(r'<form method="post" action="/logout">\s*<button '
                     r'class="btn-primary btn-block"', body)
    assert "Someone else is signed in on this browser" in body


def test_a_change_confirmation_never_signs_in(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    first = app.test_client()
    token, _n, _ = _ask(first, sent)
    first.post("/beta/confirm", data={"t": token})      # joined, no sign-in
    # A change to the confirmed request, a day later.
    from tests.test_careagents_beta_signup import _age_caps
    _age_caps(svc)
    c = app.test_client()
    assert _post(c, ip="203.0.113.8", first_name="Ave").status_code == 200
    token = _confirm_token([m for m in sent if m[0] == EMAIL][-1][3])
    page = c.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    assert "A change to your request" in page
    assert 'name="n"' not in page
    r = c.post("/beta/confirm", data={"t": token, "n": "anything"})
    assert r.status_code == 200
    assert _signed_in_as(c) is None and _account(svc) is None


def test_a_records_service_outage_still_signs_in_and_lands_on_the_hub(
        made, sent, monkeypatch):
    from careagents.healthclaw import HealthClawError
    from tests.test_careagents import FakeClient

    def down(self, tenant):
        raise HealthClawError("down")
    monkeypatch.setattr(FakeClient, "seed", down)
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    token, nonce, _ = _ask(c, sent)
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 303 and r.headers["Location"] == "/home"
    assert _signed_in_as(c) == _account(svc).id


def test_a_paused_email_is_signed_in_without_new_records(
        made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    _login(app.test_client(), svc, monkeypatch, email=EMAIL)
    svc.set_paused(EMAIL, True)
    c = app.test_client()
    token, nonce, _ = _ask(c, sent)
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.headers["Location"] == "/home"
    with svc.session() as s:
        assert s.query(Connection).count() == 0


# --- the fallback: one button, email filled in, code step first ---------------

def test_the_fallback_page_ends_with_one_button_to_sign_in(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    token, _n, _ = _ask(c, sent)
    body = c.post("/beta/confirm", data={"t": token}).get_data(as_text=True)
    card = body[body.index('class="setup-card'):body.index("</main>")]
    buttons = re.findall(r'class="btn-primary[^"]*"', card)
    assert len(buttons) == 1
    assert card.rstrip().endswith("</div>")
    last = card[card.rindex("<a "):]
    assert 'href="/auth"' in last and "btn-primary" in last


def test_auth_opens_on_the_email_step_with_the_email_filled_in(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    token, _n, _ = _ask(c, sent)
    c.post("/beta/confirm", data={"t": token})
    page = c.get("/auth").get_data(as_text=True)
    assert f'value="{EMAIL}"' in page
    email_btn = re.search(r'<button[^>]*id="email-btn"[^>]*>', page).group(0)
    passkey = re.search(r'<button[^>]*id="passkey-btn"[^>]*>', page).group(0)
    assert "btn-primary" in email_btn and "btn-primary" not in passkey
    assert page.index('id="email-btn"') < page.index('id="passkey-btn"')


def test_auth_without_a_confirm_is_unchanged(made):
    app, _ = made()
    page = app.test_client().get("/auth").get_data(as_text=True)
    passkey = re.search(r'<button[^>]*id="passkey-btn"[^>]*>', page).group(0)
    assert "btn-primary" in passkey
    assert page.index('id="passkey-btn"') < page.index('id="email-btn"')
    assert 'value="' not in re.search(r'<input[^>]*id="email"[^>]*>',
                                      page).group(0)


def test_a_first_run_code_sign_in_skips_the_passkey_prompt(
        made, monkeypatch):
    """An account with nothing connected goes to its records first; the
    passkey can be added later from the chat's menu."""
    app, svc = made()
    c = app.test_client()
    data = _login(c, svc, monkeypatch, email=EMAIL)
    assert data["first_run"] is True
    c.post("/api/connections/sample")
    again = app.test_client()
    assert _login(again, svc, monkeypatch, email=EMAIL)["first_run"] is False
