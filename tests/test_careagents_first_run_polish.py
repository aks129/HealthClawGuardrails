"""Patient-tester findings on #920 (a 375px walk as a first-timer).

F1: "Add a passkey" from the chat menu opened a sign-in screen that never
said why, offered "Sign in with a passkey" to someone who has none, and
ended on the hub instead of the chat they left.
F2: the "You're in" steps left out the passkey screen a returning tester
sees, and named a button the hub does not have.
F3: an already-used confirm link was a dead end with no way to sign in.
F5: leftover copy: /beta's "Pick the made-up records", "Mobile: no
mobile", and a menu that said "Your hub" and "Safety grade" and had no
Sign out.

All data is synthetic.
"""

# ruff: noqa: F811
from __future__ import annotations

import pathlib
import re

from careagents import beta_signup
from tests.test_careagents import _login
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _post, made, sent)
from tests.test_careagents_first_run import (EMAIL, _ask, _link_session,
                                              _signed_in_as)

ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTH_JS = (ROOT / "careagents" / "static" / "auth.js").read_text()


def _agent(c):
    with c.session_transaction() as s:
        acct = s["account_id"]
    return acct


def _chat_agent(c):
    page = c.get("/home").get_data(as_text=True)
    return re.search(r'href="/chat\?agent=([\w-]+)"', page).group(1)


# --- F1 -----------------------------------------------------------------------

def _menu_passkey_href(page):
    return re.search(r'<a href="(/auth\?enroll=1[^"]*)">Faster sign-in \(passkey\)</a>',
                     page).group(1)


def test_the_menu_link_says_which_chat_to_come_back_to(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    c = _link_session(app, sent)
    agent = _chat_agent(c)
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _menu_passkey_href(page) == f"/auth?enroll=1&amp;agent={agent}"


def test_a_link_session_is_told_why_it_gets_a_code(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    c = _link_session(app, sent)
    agent = _chat_agent(c)
    page = c.get(f"/auth?enroll=1&agent={agent}").get_data(as_text=True)
    assert "To add a passkey, confirm it's you with a code we email." in page
    assert 'id="passkey-btn"' not in page
    assert "New here?" not in page
    assert f'value="{EMAIL}"' in page
    assert f'data-back="/chat?agent={agent}"' in page


def test_only_your_own_chat_is_a_way_back(made, sent, monkeypatch):
    """`agent` names a chat to return to; one that is not this account's
    is dropped, so the page can never send anyone elsewhere."""
    app, svc = made(RESEND_API_KEY="re_test")
    other = app.test_client()
    _login(other, svc, monkeypatch, email="sam@example.com")
    sams = other.post("/api/connections/sample").get_json()["agent_id"]
    c = _link_session(app, sent)
    for agent in (sams, "https://evil.example/x", "../home"):
        page = c.get(f"/auth?enroll=1&agent={agent}").get_data(as_text=True)
        assert "data-back" not in page, agent


def test_a_full_session_enrols_with_the_way_back(made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    page = c.get(f"/auth?enroll=1&agent={agent}").get_data(as_text=True)
    assert re.search(r'<div id="step-passkey"\s*>', page)
    assert f'data-back="/chat?agent={agent}"' in page
    assert "To add a passkey" not in page


def test_auth_js_returns_to_the_chat_after_adding_or_skipping():
    assert 'const back = $("auth-card").dataset.back || "";' in AUTH_JS
    enroll = AUTH_JS[AUTH_JS.index("async function enroll()"):]
    assert 'location.href = back ? back + "&passkey=added" : "/home";' \
        in enroll
    assert ('$("skip-passkey-btn").addEventListener("click", () =>\n'
            '    (location.href = back || "/home"));') in AUTH_JS


def test_the_chat_says_passkey_added_only_when_there_is_one(
        made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    url = f"/chat?agent={agent}&passkey=added"
    assert "Passkey added" not in c.get(url).get_data(as_text=True)
    monkeypatch.setattr(svc, "has_passkey", lambda account_id: True)
    page = c.get(url).get_data(as_text=True)
    assert '<div class="chat-notice" role="status">Passkey added.' in page


# --- F2 -----------------------------------------------------------------------

def test_the_steps_mention_the_passkey_screen_and_the_chat_button():
    steps = beta_signup.NEXT_STEPS
    assert any('"Skip for now"' in s and "passkey" in s for s in steps)
    assert any('"Chat"' in s for s in steps)
    assert not any("Open the chat" in s for s in steps)
    home = (ROOT / "careagents" / "templates" / "home.html").read_text()
    assert '">Chat</a>' in home
    src = (ROOT / "careagents" / "beta_signup.py").read_text()
    comment = src[:src.index("NEXT_STEPS = (")].rsplit("\n\n", 1)[-1]
    assert "skips the passkey screen" not in comment
    assert "Skip for now" in comment


# --- F3 -----------------------------------------------------------------------

def test_a_used_link_offers_sign_in(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    token, _n, _ = _ask(c, sent)
    c.post("/beta/confirm", data={"t": token})
    fresh = app.test_client()
    r = fresh.get(f"/beta/confirm?t={token}")
    body = r.get_data(as_text=True)
    assert r.status_code == 410
    assert "latest email" not in body
    assert 'class="btn-primary btn-block" href="/auth">Sign in</a>' in body
    # Never prefilled from the token: a fresh browser gets an empty box.
    page = fresh.get("/auth").get_data(as_text=True)
    assert 'value="' not in re.search(r'<input[^>]*id="email"[^>]*>',
                                      page).group(0)
    assert _signed_in_as(fresh) is None


# --- F5 -----------------------------------------------------------------------

def test_beta_step_two_says_confirm_opens_the_made_up_records(made):
    app, _ = made()
    body = app.test_client().get("/beta").get_data(as_text=True)
    step2 = body[body.index('id="beta-step-2"'):]
    step2 = step2[:step2.index("</li>")]
    assert "Pick" not in step2
    assert "Confirm" in step2 and "made-up" in step2


def test_the_confirm_page_has_no_empty_mobile_line(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    _, _, page = _ask(app.test_client(), sent)
    assert "no mobile" not in page and "Mobile:" not in page


def test_the_menu_reads_plainly_and_signs_out(made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    menu = re.search(r'<details class="chat-menu">.*?</details>', page,
                     re.S).group(0)
    assert '<a href="/home">Home</a>' in menu
    assert re.search(r'id="trust-pill"[^>]*>How we keep you safe</a>', menu)
    assert "Your hub" not in menu and "Safety grade" not in menu
    assert re.search(r'<form method="post" action="/logout">\s*'
                     r'<button type="submit">Sign out</button>', menu)
    js = (ROOT / "careagents" / "static" / "chat.js").read_text()
    assert "Safety grade" not in js
