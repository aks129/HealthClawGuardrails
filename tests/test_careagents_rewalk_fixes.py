"""What the patient re-walk on #979 found (2026-10-16): Delete after
joining from the email link was a dead end, the code screen's wait froze,
one home Wi-Fi could lock a family out of /beta (tested beside the rate
limit in test_careagents_beta_signup.py), one hub line read as being left
out, and the /beta tab title.

Synthetic data only.
"""
# ruff: noqa: F811  (pytest fixtures imported by name)

from __future__ import annotations

import pathlib
import re

from careagents import beta_signup
from tests.test_careagents import _login
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _confirm_token, _post, made)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_HOME_JS = (_ROOT / "careagents" / "static" / "home.js").read_text()
_AUTH_JS = (_ROOT / "careagents" / "static" / "auth.js").read_text()


def _link_session(app, monkeypatch, email="robin@example.com"):
    """Join from /beta and tap Confirm on our page: a confirm-link session.
    The confirm email is read from the fake provider."""
    from careagents import mail
    out = []

    def fake_post(url, headers=None, json=None, timeout=None):
        out.append(json)

        class R:
            status_code = 200
        return R()
    monkeypatch.setattr(mail.requests, "post", fake_post)
    c = app.test_client()
    assert _post(c, email=email, first_name="Robin").status_code == 200
    token = _confirm_token(out[-1]["text"])
    page = c.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    n = re.search(r'name="n" value="([^"]+)"', page).group(1)
    assert c.post("/beta/confirm", data={"t": token, "n": n}
                  ).status_code == 303
    return c


def _code_for(monkeypatch, client, email):
    from careagents import mail
    seen = {}
    monkeypatch.setattr(
        mail, "send_code",
        lambda cfg, e, code, purpose: (seen.setdefault("c", code)
                                       and mail.SENT))
    r = client.post("/api/auth/email", json={"email": email})
    assert r.status_code == 200 and r.get_json()["sent"] is True
    return seen["c"]


# --- 1. delete after the email link -------------------------------------------

def test_a_link_session_sees_the_code_step_in_the_delete_dialog(
        made, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    c = _link_session(app, monkeypatch)
    page = c.get("/settings").get_data(as_text=True)
    step = re.search(r'<div class="delete-code-step".*?</div>\s*<p class='
                     r'"form-error" id="delete-code-err"', page, re.S)
    assert step, page
    assert 'data-email="robin@example.com"' in step.group(0)
    words = " ".join(page.split())
    assert "To delete, we'll email you a code first." in words
    assert 'id="delete-code-send"' in page and 'id="delete-code"' in page


def test_a_full_sign_in_sees_no_code_step(made, monkeypatch):
    app, svc = made()
    c = app.test_client()
    _login(c, svc, monkeypatch, email="full@example.com")
    page = c.get("/settings").get_data(as_text=True)
    assert 'id="delete-code-step"' not in page
    assert 'id="delete-input"' in page


def test_the_hub_delete_dialog_has_no_code_step(made, monkeypatch):
    """Deleting records is not behind a full sign-in; only the account is."""
    app, svc = made(RESEND_API_KEY="re_test")
    c = _link_session(app, monkeypatch)
    assert 'id="delete-code-step"' not in c.get("/home").get_data(
        as_text=True)


def test_delete_still_needs_the_code_then_works(made, monkeypatch):
    """The rule stands: a confirm-link session cannot delete. The code
    sign-in the dialog runs is what lets it, and then the delete works."""
    app, svc = made(RESEND_API_KEY="re_test")
    c = _link_session(app, monkeypatch)
    refused = c.post("/api/account/delete", json={"confirm": "DELETE"})
    assert refused.status_code == 403
    assert refused.get_json()["error"] == "sign_in_again"
    code = _code_for(monkeypatch, c, "robin@example.com")
    # A wrong code changes nothing.
    assert c.post("/api/auth/verify", json={
        "email": "robin@example.com", "code": "00000000"}).status_code == 400
    assert c.post("/api/account/delete",
                  json={"confirm": "DELETE"}).status_code == 403
    assert c.post("/api/auth/verify", json={
        "email": "robin@example.com", "code": code}).status_code == 200
    done = c.post("/api/account/delete", json={"confirm": "DELETE"})
    assert done.status_code == 200, done.get_json()
    from careagents.models import Account
    with svc.session() as s:
        assert s.query(Account).filter_by(
            email="robin@example.com").count() == 0


def test_the_dialog_gates_delete_on_the_code_step():
    ask = _HOME_JS.split("function askToDelete(")[1].split("\n  }")[0]
    assert "const gate = codeGate(" in ask
    assert "if (gate && !gate.done()) ok.disabled = true;" in ask
    assert "ok.onclick = () => { if (gate.done()) typed(); };" in ask
    gate = _HOME_JS.split("function codeGate(")[1].split("\n  }\n")[0]
    # The code goes to the account's own address, through the same two
    # endpoints as the sign-in page; never markup.
    assert 'post("/api/auth/email", { email })' in gate
    assert 'post("/api/auth/verify"' in gate
    assert "step.dataset.email" in gate
    assert "innerHTML" not in gate
    assert "res.d.sent === true" in gate


# --- 2. the code screen's wait ------------------------------------------------

def test_the_code_wait_counts_down_and_brings_the_button_back():
    count = _AUTH_JS.split("function countdown(")[1].split("\n  }\n")[0]
    assert "setInterval(" in count and "left -= 1;" in count
    assert '$("resend-code").hidden = left > 0;' in count
    assert "You can ask for a new code now." in count
    assert "countdown(res.d.retry_after);" in _AUTH_JS


# --- 5 and 6. words ---------------------------------------------------------

def test_the_beta_tab_title_is_plain(made):
    app, _ = made()
    page = app.test_client().get("/beta").get_data(as_text=True)
    assert "<title>Join the beta — CareAgents</title>" in page
    assert "Test CareAgents —" not in page


def test_no_hub_line_says_records_are_not_open_to_you():
    src = (_ROOT / "careagents" / "app.py").read_text()
    assert "isn't open to you" not in src
    assert ("For now you're using made-up records. Connecting your "
            "own records is coming later in the beta.") in " ".join(
                src.replace('"\n', "").replace('                    "', "")
                .split())


def test_the_family_limit_is_one_constant():
    src = (_ROOT / "careagents" / "beta_signup.py").read_text()
    assert src.count("SUBMITS_PER_WINDOW = ") == 1
    assert beta_signup.SUBMITS_PER_WINDOW == 20
