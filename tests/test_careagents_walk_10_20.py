"""What the 10-20 patient walk found.

1. Signing in again after deleting the account silently made a brand-new,
   empty one, and the code email said "Verify your email" while the page
   said sign in. The email now names the action, and the person is told
   plainly, after the code, that a new account was made. Not before: the
   answer to "email me a code" stays the same with or without an account.
2. The chat menu: Settings is there, and "Faster sign-in (passkey)" in
   plain words. Approvals keep the hub's "Waiting for your OK".
3. The delete dialog's code step counts down the resend wait, as /auth does,
   rather than offering "Send a new code" at once.
4. A long email in the Settings header no longer breaks mid-word.

Synthetic data only.
"""
# ruff: noqa: F811  (pytest fixtures imported by name)

from __future__ import annotations

import logging
import pathlib
import re
from types import SimpleNamespace

from careagents import accounts, mail
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _past_the_resend_wait, app, cfg, svc)
from tests.test_careagents_beta_signup import made  # noqa: F401  (fixture)
from tests.test_careagents_rewalk_fixes import _link_session

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_STATIC = _ROOT / "careagents" / "static"
_AUTH_JS = (_STATIC / "auth.js").read_text()
_HOME_JS = (_STATIC / "home.js").read_text()
_CSS = (_STATIC / "careagents.css").read_text()


def _sign_in(client, svc, monkeypatch, email):
    """The real email-code path; returns (verify answer, mail purposes)."""
    seen = []
    monkeypatch.setattr(
        mail, "send_code",
        lambda cfg, e, code, purpose: (seen.append((purpose, code))
                                       or mail.SENT))
    _past_the_resend_wait(svc, email)
    r = client.post("/api/auth/email", json={"email": email})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = client.post("/api/auth/verify",
                    json={"email": email, "code": seen[-1][1]})
    assert r.status_code == 200
    return r.get_json(), [p for p, _c in seen]


# --- 1. a new account is said out loud ---------------------------------------

def test_a_first_code_is_a_new_account_and_says_so(app, svc, monkeypatch):
    c = app.test_client()
    data, purposes = _sign_in(c, svc, monkeypatch, "new.walk@example.com")
    assert purposes == ["verify"]
    assert data["new_account"] is True


def test_a_returning_sign_in_is_a_sign_in(app, svc, monkeypatch):
    c = app.test_client()
    _sign_in(c, svc, monkeypatch, "back.walk@example.com")
    data, purposes = _sign_in(app.test_client(), svc, monkeypatch,
                              "Back.Walk@example.com ")
    assert purposes == ["login"]
    assert data["new_account"] is False


def test_signing_in_after_a_delete_says_it_is_a_new_account(
        app, svc, monkeypatch):
    email = "gone.walk@example.com"
    c = app.test_client()
    _sign_in(c, svc, monkeypatch, email)
    r = c.post("/api/account/delete", json={"confirm": "DELETE"})
    assert r.status_code == 200, r.get_data(as_text=True)
    data, purposes = _sign_in(app.test_client(), svc, monkeypatch, email)
    assert purposes == ["verify"]
    assert data["new_account"] is True


def test_the_code_request_answer_does_not_say_whether_an_account_exists(
        app, svc, monkeypatch):
    _sign_in(app.test_client(), svc, monkeypatch, "has.walk@example.com")
    monkeypatch.setattr(mail, "send_code", lambda *a: mail.SENT)
    answers = []
    for email in ("has.walk@example.com", "none.walk@example.com"):
        _past_the_resend_wait(svc, email)
        r = app.test_client().post("/api/auth/email", json={"email": email})
        answers.append((r.status_code, r.get_data()))
    assert answers[0] == answers[1]


def test_the_code_email_names_the_action(caplog, monkeypatch):
    def _no_network(*a, **kw):
        raise AssertionError("the dev stub must not call Resend")
    monkeypatch.setattr(mail.requests, "post", _no_network)
    cfg = SimpleNamespace(resend_api_key=None, resend_from="x@example.test")
    with caplog.at_level(logging.WARNING, logger="careagents.mail"):
        mail.send_code(cfg, "a@example.test", "01234567", "verify")
        mail.send_code(cfg, "a@example.test", "01234567", "login")
    lines = [r.getMessage() for r in caplog.records
             if r.name == "careagents.mail"]
    assert "Verify your email" not in " ".join(lines)
    assert "new account" in lines[0]
    assert "sign-in code" in lines[1]


def test_the_subject_sent_to_resend_names_the_action(monkeypatch):
    sent = []

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(mail.requests, "post", fake_post)
    cfg = SimpleNamespace(resend_api_key="re_test", resend_from="x@e.test",
                          app_env="production")
    mail.send_code(cfg, "a@example.test", "01234567", "verify")
    mail.send_code(cfg, "a@example.test", "01234567", "login")
    assert "new account" in sent[0]["subject"]
    assert "sign-in code" in sent[1]["subject"]


def test_the_page_after_the_code_says_a_new_account_was_made():
    """auth.js carries the sentence to the hub notice home.js shows."""
    i = _AUTH_JS.index("new_account === true")
    tail = _AUTH_JS[i:i + 400]
    assert "careagents.hubNotice" in tail
    assert "No account with this email yet" in tail
    assert "careagents.hubNotice" in _HOME_JS


# --- 2. the chat menu --------------------------------------------------------

def _menu(page: str) -> str:
    m = re.search(r'<details class="chat-menu">.*?</details>', page, re.S)
    assert m, "no menu"
    return m.group(0)


def test_the_chat_menu_in_plain_words(cfg, svc, monkeypatch):
    _app, c, _fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    menu = _menu(c.get(f"/chat?agent={agent_id}").get_data(as_text=True))
    assert '<a href="/settings">Settings</a>' in menu
    assert f'href="/agents/{agent_id}/approvals"' in menu
    # The same words as the hub card and the approvals page.
    assert ">Waiting for your OK</a>" in menu
    assert (f'href="/auth?enroll=1&amp;agent={agent_id}">'
            "Faster sign-in (passkey)</a>") in menu
    assert "Add a passkey" not in menu


# --- 3. the delete dialog's resend wait --------------------------------------

def _code_gate() -> str:
    i = _HOME_JS.index("function codeGate(")
    return _HOME_JS[i:_HOME_JS.index("return { done:", i)]


def test_the_delete_code_step_waits_before_offering_a_new_code():
    gate = _code_gate()
    send = gate[gate.index("send.onclick"):gate.index("delete-code-check")]
    # No "Send a new code" the moment the code goes out: the wait first.
    assert "send.hidden = true" in send
    assert "countdown(" in send
    assert "You can request a new one in" in gate
    assert "Send a new code" in gate


def test_the_delete_wait_is_the_servers_resend_wait(made, monkeypatch):
    app, _svc = made(RESEND_API_KEY="re_test")
    c = _link_session(app, monkeypatch, email="wait.walk@example.com")
    page = c.get("/settings").get_data(as_text=True)
    step = re.search(r'<div class="delete-code-step"[^>]*>', page)
    assert step, page
    assert (f'data-resend-wait="{accounts.RESEND_COOLDOWN}"'
            in step.group(0))


# --- 4. polish ---------------------------------------------------------------

def _rule(selector: str) -> str:
    i = _CSS.index(selector + " {")
    return _CSS[i:_CSS.index("}", i)]


def test_a_long_email_in_the_header_ends_in_an_ellipsis():
    rule = _rule(".hub-sub.hub-email")
    assert "text-overflow: ellipsis" in rule
    assert "white-space: nowrap" in rule and "overflow: hidden" in rule


def test_the_chat_log_scrolls_under_an_edge_of_its_own():
    """Not cut flush against the made-up-records banner."""
    rule = _rule(".sample-banner + .chat-log")
    assert "margin-top: 8px" in rule and "border-top" in rule
    chat = (_ROOT / "careagents" / "templates" / "chat.html").read_text()
    # The log is the banner's next sibling, so the rule applies.
    between = chat[chat.index("{{ sample_line }}</p>{% endif %}"):
                   chat.index('<div class="chat-log"')]
    assert re.sub(r"\{#.*?#\}", "", between, flags=re.S).strip() == \
        "{{ sample_line }}</p>{% endif %}"
