"""Security delta check of #920 (8dcb2f5 -> 92ca29b): the /auth `agent`
way-back, enrolment from a link session, the used-link page, Sign out.
Synthetic data only."""
# ruff: noqa: F811
from __future__ import annotations

import re

import pytest

from tests.test_careagents import _login
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _confirm_token, _post, made, sent)
from tests.test_careagents_first_run import _ask, _signed_in_as


def _back(page):
    m = re.search(r'data-back="([^"]*)"', page)
    return m.group(1) if m else None


def _link_session(app, sent, email="avery@example.com"):
    c = app.test_client()
    token, nonce, _ = _ask(c, sent, email=email)
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 303
    return c, r.headers["Location"].split("agent=")[1]


@pytest.mark.parametrize("agent", [
    "//evil.example/x", "https://evil.example", "/\\evil.example",
    "x\"><script>alert(1)</script>", "../home", "%2F%2Fevil.example",
    "javascript:alert(1)", ""])
def test_agent_param_never_becomes_a_redirect(made, sent, agent):
    app, _ = made(RESEND_API_KEY="re_test")
    c, _own = _link_session(app, sent)
    page = c.get("/auth", query_string={"enroll": "1", "agent": agent}
                 ).get_data(as_text=True)
    assert _back(page) is None
    assert "<script>alert" not in page


def test_another_accounts_agent_is_not_a_way_back(made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    _victim, victim_agent = _link_session(app, sent, "victim@example.com")
    me = app.test_client()
    _login(me, svc, monkeypatch, email="mallory@example.net")
    me.post("/api/connections/sample")
    page = me.get(f"/auth?enroll=1&agent={victim_agent}").get_data(
        as_text=True)
    assert _back(page) is None
    # Own agent works, and is only ever a same-origin chat path.
    with svc.session() as s:
        from careagents.models import Account, Agent
        mid = s.query(Account).filter_by(email="mallory@example.net").one().id
        own = s.query(Agent).filter_by(account_id=mid).first().id
    page = me.get(f"/auth?enroll=1&agent={own}").get_data(as_text=True)
    assert _back(page) == f"/chat?agent={own}"


def test_a_link_session_reaching_enroll_gets_the_code_step_not_enrolment(
        made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c, agent = _link_session(app, sent)
    page = c.get(f"/auth?enroll=1&agent={agent}").get_data(as_text=True)
    assert "confirm it's you with a code" in page
    assert 'id="passkey-btn"' not in page
    # Still refused at the API, whatever the page draws.
    for path in ("/webauthn/register/options", "/webauthn/register/verify"):
        assert c.post(path, json={}).status_code == 403
    with c.session_transaction() as s:
        assert s.get("via_link") is True


def test_passkey_added_is_not_claimed_without_a_passkey(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    c, agent = _link_session(app, sent)
    page = c.get(f"/chat?agent={agent}&passkey=added").get_data(as_text=True)
    assert "Passkey added" not in page


def test_the_used_link_page_names_no_one(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    token, nonce, _ = _ask(c, sent)
    c.post("/beta/confirm", data={"t": token, "n": nonce})
    stranger = app.test_client()
    page = stranger.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    assert "avery@example.com" not in page and 'href="/auth"' in page
    auth = stranger.get("/auth").get_data(as_text=True)
    assert "avery@example.com" not in auth


def test_a_cross_site_logout_post_carries_no_session(made, sent):
    """/logout has no CSRF token; under SameSite=Lax a cross-site POST
    arrives without the cookie, so it clears an empty session. Pinned
    here as: a POST with no cookie signs nobody out elsewhere."""
    app, _ = made(RESEND_API_KEY="re_test")
    c, _a = _link_session(app, sent)
    app.test_client().post("/logout")
    assert _signed_in_as(c) is not None
