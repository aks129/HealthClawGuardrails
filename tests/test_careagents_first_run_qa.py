"""QA of #920: what the first-run tests left unpinned.

Mutation run 2026-10-09: dropping `and not session.get("account_id")` from
the fallback's prefill (careagents/beta_signup.py, beta_confirm) failed no
test. That guard keeps a confirm in a browser where someone else is signed
in from planting the confirmed address in that person's session, where
their next /auth would show it. All data is synthetic.
"""

# ruff: noqa: F811
from __future__ import annotations

from careagents import beta_signup
from tests.test_careagents import _login
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    made, sent)
from tests.test_careagents_first_run import _ask, _signed_in_as


def test_a_confirm_in_someone_elses_browser_plants_no_prefill(
        made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="sam@example.com")
    sam = _signed_in_as(c)
    token, nonce, _ = _ask(c, sent)
    r = c.post("/beta/confirm", data={"t": token, "n": nonce})
    assert r.status_code == 200
    assert _signed_in_as(c) == sam
    with c.session_transaction() as s:
        assert beta_signup.AUTH_EMAIL_KEY not in s


def test_a_signed_out_fallback_does_plant_the_prefill(made, sent):
    """The other side of the guard: no nonce, nobody signed in, so the
    fallback page's one button opens /auth with this address filled in."""
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    token, _, _ = _ask(c, sent)
    r = c.post("/beta/confirm", data={"t": token})
    assert r.status_code == 200
    with c.session_transaction() as s:
        assert s.get(beta_signup.AUTH_EMAIL_KEY) == "avery@example.com"
    page = c.get("/auth").get_data(as_text=True)
    assert 'value="avery@example.com"' in page
