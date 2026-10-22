"""QA additions for the sign-in return (PR #1016).

The PR's own file covers the email-code path and the redirect wall. These
pin three things it did not: the consent handoff really does resume ahead
of the parked page (the PR's test of that name sets only the iMessage
link), the passkey sign-in carries the parked page too, and a page that
belongs to someone else bounces to the hub once, with no loop.
"""
import time

from careagents import consent
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, _make_account, app, cfg, svc)

KEY = "after_sign_in"
TARGET = "/chat?agent=agent_abc&chart=blood%20pressure"


def _handle(request_id="req-qa1016"):
    exp = int(time.time()) + 600
    return (f"{request_id}.{exp}."
            f"{consent.tag('mint-secret', f'{request_id}.{exp}')}")


def test_a_consent_request_resumes_before_the_parked_page(
        app, svc, monkeypatch):  # noqa: F811
    """Both parked while signed out: the consent step comes first, and the
    tapped page is dropped then, so it never takes over a later visit to
    the hub (fix round on #1016). MUTATION: move the after_sign_in resume
    above the consent_req pop in /home -> red."""
    client = app.test_client()
    assert client.get(TARGET).headers["Location"].endswith("/auth")
    req = _handle()
    r = client.get("/authorize", query_string={"req": req})
    assert r.status_code == 302 and r.headers["Location"].endswith("/auth")
    _login(client, svc, monkeypatch, "pat@example.com")
    home = client.get("/home")
    assert home.status_code == 302
    assert "/authorize?req=" in home.headers["Location"]
    with client.session_transaction() as s:
        assert KEY not in s


def test_a_passkey_sign_in_returns_to_the_page_tapped(app, svc,  # noqa: F811
                                                      monkeypatch):
    """auth.js sends a passkey sign-in to /home unconditionally; the
    parked page must survive /webauthn/login/verify's _login as it does the
    code path's."""
    acct = _make_account(svc, monkeypatch, "pat@example.com")
    client = app.test_client()
    assert client.get("/brief?agent=agent_abc").status_code == 302
    with client.session_transaction() as s:
        s["wa_challenge"] = "challenge"
    monkeypatch.setattr(svc, "finish_authentication",
                        lambda cred, challenge, **kw: acct)
    assert client.post("/webauthn/login/verify", json={}).status_code == 200
    home = client.get("/home")
    assert home.status_code == 302
    assert home.headers["Location"].endswith("/brief?agent=agent_abc")
    assert client.get("/home").status_code == 200


def test_someone_elses_page_bounces_to_the_hub_once(app, svc,  # noqa: F811
                                                    monkeypatch):
    """A shared browser: the tapped page is not this account's. The page's
    own owner check sends it back to /home, which shows the hub because
    the resume was spent. No loop, no 500."""
    client = app.test_client()
    client.get(TARGET)
    _login(client, svc, monkeypatch, "someone-else@example.com")
    r = client.get("/home", follow_redirects=True)
    assert r.status_code == 200
    assert r.request.path == "/home"
    assert [h.request.path for h in r.history] == ["/home", "/chat"]
