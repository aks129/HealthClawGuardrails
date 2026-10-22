"""A link tapped while signed out comes back after sign-in.

Beta feedback: a tester tapped a link to their own page (a texted /chat,
/brief or /review link opens in whatever browser the phone picks), was sent
to sign in, typed the emailed code, and landed on the hub, not the page
they tapped. They had to find it again. login_required sent them to /auth
with no record of where they were going, and every sign-in ends on /home.

Now the page asked for is parked in the session, carried across the
session clear in _login (as consent_req is), and /home sends the person
there once. Only a path on this site is ever followed.
"""
import pytest

from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, _make_account, app, cfg, svc)

KEY = "after_sign_in"


def _tap_signed_out(client, target):
    r = client.get(target)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/auth")
    return r


@pytest.mark.parametrize("target", [
    "/review/agent_abc/act_123",
    "/chat?agent=agent_abc&chart=blood%20pressure",
    "/brief?agent=agent_abc",
])
def test_signing_in_by_email_code_returns_to_the_page_tapped(
        app, svc, monkeypatch, target):  # noqa: F811
    """MUTATION: drop the carry in _login, or the resume in /home -> red."""
    client = app.test_client()
    _tap_signed_out(client, target)
    _login(client, svc, monkeypatch, "pat@example.com")
    # auth.js sends every sign-in to /home; /home sends it on.
    home = client.get("/home")
    assert home.status_code == 302
    assert home.headers["Location"].endswith(target)
    # Once: the hub is the hub again afterwards.
    assert client.get("/home").status_code == 200


def test_only_what_was_tapped_survives_the_session_clear(app, svc,  # noqa: F811
                                                         monkeypatch):
    client = app.test_client()
    _tap_signed_out(client, "/review/agent_abc/act_123")
    with client.session_transaction() as s:
        s["something_else"] = "planted-before-sign-in"
    _login(client, svc, monkeypatch, "pat@example.com")
    with client.session_transaction() as s:
        assert s[KEY] == "/review/agent_abc/act_123"
        assert "something_else" not in s


def test_a_session_whose_account_is_gone_still_returns_to_the_page(
        app, svc, monkeypatch):  # noqa: F811
    """The ended-session branch clears the session; the target is parked
    after that clear, not wiped by it."""
    client = app.test_client()
    with client.session_transaction() as s:
        s["account_id"] = "acct_deleted_long_ago"
    _tap_signed_out(client, "/chat?agent=agent_abc")
    _login(client, svc, monkeypatch, "pat@example.com")
    home = client.get("/home")
    assert home.headers["Location"].endswith("/chat?agent=agent_abc")


def test_a_page_with_no_query_is_parked_without_a_trailing_question_mark(
        app, svc, monkeypatch):  # noqa: F811
    client = app.test_client()
    _tap_signed_out(client, "/settings")
    with client.session_transaction() as s:
        assert s[KEY] == "/settings"


@pytest.mark.parametrize("planted", [
    "//evil.example/x",
    "/\\evil.example/x",
    "https://evil.example/x",
    "javascript:alert(1)",
    "evil.example/x",
    "/" + "a" * 2048,
    "/chat\n?agent=x",
    "",
    ["/chat"],
    7,
])
def test_a_target_off_this_site_is_never_followed(app, svc, monkeypatch,  # noqa: F811
                                                   planted):
    """The session is signed, so only this app writes the key; this is the
    second wall, should anything ever park a value it did not build.
    MUTATION: follow any string -> red."""
    client = app.test_client()
    with client.session_transaction() as s:
        s[KEY] = planted
    _login(client, svc, monkeypatch, "pat@example.com")
    with client.session_transaction() as s:
        assert KEY not in s
    assert client.get("/home").status_code == 200


def test_a_planted_off_site_target_is_not_followed_by_home_either(
        app, svc, monkeypatch):  # noqa: F811
    """Even in a signed-in session, /home follows only a local path."""
    client = app.test_client()
    _login(client, svc, monkeypatch, "pat@example.com")
    with client.session_transaction() as s:
        s[KEY] = "//evil.example/x"
    assert client.get("/home").status_code == 200


def test_an_api_call_or_a_post_signed_out_parks_nothing(app):  # noqa: F811
    client = app.test_client()
    assert client.get("/api/approvals/count").status_code == 401
    assert client.post("/review/agent_abc/act_123/submit",
                       data={}).status_code in (302, 401)
    with client.session_transaction() as s:
        assert KEY not in s


def test_a_waiting_consent_request_still_comes_first(app, svc, monkeypatch):  # noqa: F811
    """The consent handoff (#846) and the texted iMessage link resume ahead
    of an ordinary page: they are a step the person has to finish."""
    client = app.test_client()
    _tap_signed_out(client, "/chat?agent=agent_abc")
    _login(client, svc, monkeypatch, "pat@example.com")
    with client.session_transaction() as s:
        s["imessage_link"] = "link_123"
    home = client.get("/home")
    assert "/chat?agent=agent_abc" not in home.headers["Location"]
    with client.session_transaction() as s:
        # Still waiting for its turn.
        assert s[KEY] == "/chat?agent=agent_abc"


def test_signing_out_forgets_the_page(app, svc, monkeypatch):  # noqa: F811
    client = app.test_client()
    _make_account(svc, monkeypatch, "pat@example.com")
    _tap_signed_out(client, "/chat?agent=agent_abc")
    client.post("/logout")
    with client.session_transaction() as s:
        assert KEY not in s
