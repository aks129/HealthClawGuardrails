"""What a patient tester's beta walk found (2026-10-16), besides the lost
confirm email (tests/test_careagents_beta_resend.py): the in-app browser
hint, a spent confirm link, the wrong-code way on, and screens that
promised what they did not keep or spoke in jargon.

Synthetic data only.
"""
# ruff: noqa: F811  (pytest fixtures imported by name)

from __future__ import annotations

import pathlib
import re

import pytest

from careagents import beta_signup
from careagents.app import RECORDS_DOWN_TEXT
from tests.test_careagents import (  # noqa: F401  (fixtures)
    FakeClient, _login, app, cfg, svc)
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _confirm_token, _post, made, sent)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_STATIC = _ROOT / "careagents" / "static"
_TEMPLATES = _ROOT / "careagents" / "templates"

SAFARI = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
          "Mobile/15E148 Safari/604.1")
LINKEDIN = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
            "[LinkedInApp]/9.30.1")


def _visible(body):
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


# --- 2. an app's built-in browser -----------------------------------------

@pytest.mark.parametrize("ua", [
    LINKEDIN,
    # Gmail's links through Google's app on iPhone.
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/"
    "605.1.15 (KHTML, like Gecko) GSA/350.0.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/"
    "605.1.15 (KHTML, like Gecko) Mobile/15E148 [FBAN/FBIOS;FBAV/480.0]",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/"
    "605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 350.0",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP2A; wv) AppleWebKit/"
    "537.36 (KHTML, like Gecko) Version/4.0 Chrome/129.0 Mobile Safari/537.36",
])
def test_an_apps_built_in_browser_is_recognised(ua):
    assert beta_signup.in_app_browser(ua)


@pytest.mark.parametrize("ua", [
    SAFARI,
    # Chrome and Firefox on iPhone.
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/"
    "605.1.15 (KHTML, like Gecko) CriOS/129.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/"
    "605.1.15 (KHTML, like Gecko) FxiOS/131.0 Mobile/15E148 Safari/605.1.15",
    # Chrome on Android, and the web app added to an iPhone home screen.
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, "
    "like Gecko) Chrome/129.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/"
    "605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "", None,
])
def test_a_real_browser_never_sees_the_hint(ua):
    assert not beta_signup.in_app_browser(ua)


def test_the_confirm_page_in_linkedin_says_how_to_keep_going_in_safari(
        made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _post(c)
    token = _confirm_token(sent[-1][3])
    page = c.get(f"/beta/confirm?t={token}",
                 headers={"User-Agent": LINKEDIN}).get_data(as_text=True)
    words = _visible(page)
    assert ("To keep using CareAgents in Safari or Chrome, open "
            "http://localhost there and sign in with your email.") in words
    assert 'id="in-app-copy"' in page and "inapp.js" in page
    # The hint sits inside the form, above Confirm.
    assert page.index('id="in-app-hint"') < page.index(">Confirm</button>")
    plain = c.get(f"/beta/confirm?t={token}",
                  headers={"User-Agent": SAFARI}).get_data(as_text=True)
    assert "in-app-hint" not in plain and "inapp.js" not in plain


def test_the_hub_shows_the_hint_only_in_a_built_in_browser(
        app, svc, monkeypatch):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert 'id="in-app-hint"' in c.get(
        "/home", headers={"User-Agent": LINKEDIN}).get_data(as_text=True)
    assert 'id="in-app-hint"' not in c.get(
        "/home", headers={"User-Agent": SAFARI}).get_data(as_text=True)


def test_inapp_js_copies_the_address_with_a_fallback():
    js = (_STATIC / "inapp.js").read_text()
    assert "navigator.clipboard.writeText" in js
    assert "selectNodeContents" in js and "innerHTML" not in js


# --- 4. a spent confirm link -----------------------------------------------

def test_a_spent_link_signed_out_says_sign_in_still_works(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _post(c)
    token = _confirm_token(sent[-1][3])
    assert c.post("/beta/confirm", data={"t": token}).status_code == 200
    for r in (c.get(f"/beta/confirm?t={token}"),
              c.post("/beta/confirm", data={"t": token}),
              c.get("/beta/confirm?t=nonsense")):
        assert r.status_code == 410
        page = r.get_data(as_text=True)
        words = _visible(page)
        assert "Signing in still works" in words
        assert 'href="/auth"' in page and 'href="/beta"' in page


def test_back_after_confirming_goes_on_into_the_app(made, sent):
    """60-back-after-confirm.png: Back from the chat reopened the spent
    link and said it no longer works, as if something broke."""
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _post(c)
    token = _confirm_token(sent[-1][3])
    page = c.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    n = re.search(r'name="n" value="([^"]+)"', page).group(1)
    assert c.post("/beta/confirm", data={"t": token, "n": n}
                  ).status_code == 303
    for r in (c.get(f"/beta/confirm?t={token}"),
              c.post("/beta/confirm", data={"t": token})):
        assert r.status_code == 303
        assert r.headers["Location"].endswith("/home")


def test_a_removal_link_keeps_its_own_gone_page(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    r = app.test_client().get("/beta/remove?t=nonsense")
    assert r.status_code == 410
    assert "Signing in still works" not in r.get_data(as_text=True)


# --- 3 and 5. the sign-in page ---------------------------------------------

def test_the_sign_in_page_has_a_plain_title(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    page = c.get("/auth").get_data(as_text=True)
    assert "<h1>Sign in or sign up</h1>" in page
    assert "Your health, your keys" not in page
    # Signed in by the confirm link, asking for the invite link: already
    # in, so it is a check, not a sign-in.
    _post(c)
    token = _confirm_token(sent[-1][3])
    ask = c.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    n = re.search(r'name="n" value="([^"]+)"', ask).group(1)
    assert c.post("/beta/confirm", data={"t": token, "n": n}
                  ).status_code == 303
    page = c.get("/auth?invite=1").get_data(as_text=True)
    assert "<h1>Confirm it's you</h1>" in page


def test_a_wrong_code_offers_a_new_one():
    html = (_TEMPLATES / "auth.html").read_text()
    js = (_STATIC / "auth.js").read_text()
    assert 'id="resend-code"' in html and "Send a new code" in html
    # Right after the error box, so it reads as the way on from it.
    assert html.index('id="err"') < html.index('id="resend-code"') < \
        html.index('id="step-start"')
    verify = js.split('$("verify-btn").addEventListener')[1].split(
        '$("code").addEventListener')[0]
    assert '$("resend-code").hidden = false' in verify
    assert '$("email").value = pendingEmail' in js


def test_a_wrong_code_answer_is_the_line_the_link_sits_next_to(
        app, svc, monkeypatch):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/auth/verify", json={"email": "gene@example.com",
                                         "code": "00000000"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "That code is wrong or expired."


# --- 6. promises -------------------------------------------------------------

def test_the_landing_page_says_own_records_come_later(app):  # noqa: F811
    words = _visible(app.test_client().get("/").get_data(as_text=True))
    assert "then connect your records" not in words
    assert "Connecting your own records comes later in the beta." in words


def test_the_demo_link_says_it_opens_github(made):
    app, _ = made()
    words = _visible(app.test_client().get("/beta").get_data(as_text=True))
    assert "Watch the short demo videos on GitHub" in words
    assert "Tap a video there to play it." in words


def test_the_we_sent_screen_offers_the_other_way_in(made):
    app, _ = made()
    page = app.test_client().get("/beta").get_data(as_text=True)
    done = page[page.index('id="beta-done"'):page.index('id="beta-form"')]
    assert 'href="/auth">Get started</a>' in done
    assert 'id="beta-again"' in done


# --- 7. plain words ------------------------------------------------------------

def test_records_down_is_a_sentence_with_a_next_step(
        app, svc, monkeypatch):  # noqa: F811
    from careagents.healthclaw import HealthClawError

    def down(self, tenant):
        raise HealthClawError("down", 503)
    monkeypatch.setattr(FakeClient, "seed", down)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/sample")
    assert r.status_code == 503
    assert r.get_json()["error"] == RECORDS_DOWN_TEXT
    assert RECORDS_DOWN_TEXT.endswith("Please try again in a minute.")


def test_the_safety_page_has_no_technical_report(app):  # noqa: F811
    words = _visible(app.test_client().get("/safety").get_data(as_text=True))
    assert "Technical report" not in words
    assert "See every test we run" in words


def test_the_phone_app_row_says_how_in_a_tap(app, svc, monkeypatch):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    page = c.get("/settings").get_data(as_text=True)
    assert "install from browser" not in page
    row = re.search(r'<details class="surface phone-app">.*?</details>',
                    page, re.S).group(0)
    assert "<summary>" in row and "Add to Home Screen" in row


def test_one_name_for_what_waits_for_an_answer():
    for name in ("approvals.html", "chat.html", "home.html"):
        html = (_TEMPLATES / name).read_text()
        assert re.search(r">\s*Waiting for you\s*<", html) is None, name
    assert "<h1>Waiting for your OK</h1>" in (
        _TEMPLATES / "approvals.html").read_text()


def test_settings_names_tones_as_the_hub_does(app, svc, monkeypatch):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    c.post("/api/connections/sample")
    page = c.get("/settings").get_data(as_text=True)
    tone = re.search(r'<fieldset class="choice-group assistant-tone">.*?'
                     r'</fieldset>', page, re.S).group(0)
    labels = re.findall(r"<span>([^<]+)</span>", tone)
    assert labels == ["Calm", "Direct", "Cheerful"]
    hub = c.get("/home").get_data(as_text=True)
    assert "Tone: calm" in hub


# --- 8. the typed word ---------------------------------------------------------

def test_the_delete_label_says_capitals_do_not_matter():
    html = (_TEMPLATES / "_delete_modal.html").read_text()
    assert "Type the word delete to confirm (capitals or not):" in html
    assert "Type DELETE" not in html
    js = (_STATIC / "home.js").read_text()
    assert 'v.trim().toUpperCase() === "DELETE"' in js


def test_a_lowercase_delete_works(app, svc, monkeypatch):  # noqa: F811
    """home.js sends "DELETE" for any capitals; the server's word too."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/account/delete", json={"confirm": "nope"})
    assert r.status_code == 400
    assert r.get_json()["message"] == "Type the word delete to confirm."
