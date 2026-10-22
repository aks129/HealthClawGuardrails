"""A link tapped while signed out comes back after sign-in.

Beta feedback: a tester tapped a link to their own page (a texted /chat,
/brief or /review link opens in whatever browser the phone picks), was sent
to sign in, typed the emailed code, and landed on the hub, not the page
they tapped. They had to find it again. login_required sent them to /auth
with no record of where they were going, and every sign-in ends on /home.

Now the page asked for is parked in the session, carried across the
session clear in _login (as consent_req is), and /home sends the person
there once, within 15 minutes. Only a path on this site is ever followed.
"""
import time

import pytest

from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, _make_account, app, cfg, svc)

KEY = "after_sign_in"
AT = "after_sign_in_at"


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
        # Fresh, so the path check is what refuses it.
        s[KEY] = planted
        s[AT] = time.time()
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
        s[AT] = time.time()
    assert client.get("/home").status_code == 200


def test_an_api_call_or_a_post_signed_out_parks_nothing(app):  # noqa: F811
    client = app.test_client()
    assert client.get("/api/approvals/count").status_code == 401
    assert client.post("/review/agent_abc/act_123/submit",
                       data={}).status_code in (302, 401)
    with client.session_transaction() as s:
        assert KEY not in s


def test_a_waiting_imessage_link_comes_first_and_drops_the_page(
        app, svc, monkeypatch):  # noqa: F811
    """The texted iMessage link (and the consent handoff, #846) resume ahead
    of an ordinary page: they are a step the person has to finish. The page
    is dropped then, so it never takes over a later visit to the hub.
    MUTATION: keep the page when an earlier step wins -> red."""
    client = app.test_client()
    _tap_signed_out(client, "/chat?agent=agent_abc")
    _login(client, svc, monkeypatch, "pat@example.com")
    with client.session_transaction() as s:
        s["imessage_link"] = "link_123"
    home = client.get("/home")
    assert "/chat?agent=agent_abc" not in home.headers["Location"]
    with client.session_transaction() as s:
        assert KEY not in s and AT not in s


def test_signing_out_forgets_the_page(app, svc, monkeypatch):  # noqa: F811
    client = app.test_client()
    _make_account(svc, monkeypatch, "pat@example.com")
    _tap_signed_out(client, "/chat?agent=agent_abc")
    client.post("/logout")
    with client.session_transaction() as s:
        assert KEY not in s


def test_a_signed_out_visit_to_the_hub_parks_nothing(app):  # noqa: F811
    """/home is where every sign-in ends already; parking it would send the
    hub to itself. MUTATION: drop the request.path != "/home" check -> red."""
    client = app.test_client()
    _tap_signed_out(client, "/home")
    with client.session_transaction() as s:
        assert KEY not in s


def test_the_page_is_parked_with_the_time_it_was_tapped(app):  # noqa: F811
    client = app.test_client()
    before = time.time()
    _tap_signed_out(client, "/settings")
    with client.session_transaction() as s:
        assert before <= s[AT] <= time.time()


@pytest.mark.parametrize("age, resumes", [(14 * 60, True), (16 * 60, False)])
def test_a_page_is_honoured_for_fifteen_minutes(app, svc, monkeypatch,  # noqa: F811
                                                 age, resumes):
    """Tapped, then the code typed `age` seconds later.
    MUTATION: ignore the timestamp -> red."""
    client = app.test_client()
    _tap_signed_out(client, "/settings")
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + age)
    _login(client, svc, monkeypatch, "pat@example.com")
    home = client.get("/home")
    if resumes:
        assert home.status_code == 302
        assert home.headers["Location"].endswith("/settings")
    else:
        assert home.status_code == 200
    with client.session_transaction() as s:
        assert KEY not in s and AT not in s


def test_a_page_gone_stale_after_sign_in_is_not_followed_by_home(
        app, svc, monkeypatch):  # noqa: F811
    """Signed in in time, but the hub is first visited later."""
    client = app.test_client()
    _tap_signed_out(client, "/settings")
    _login(client, svc, monkeypatch, "pat@example.com")
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 16 * 60)
    assert client.get("/home").status_code == 200


@pytest.mark.parametrize("ts", [None, "0", True, float("nan")])
def test_a_page_with_no_usable_time_is_not_followed(app, svc, monkeypatch,  # noqa: F811
                                                     ts):
    client = app.test_client()
    _login(client, svc, monkeypatch, "pat@example.com")
    with client.session_transaction() as s:
        s[KEY] = "/settings"
        if ts is not None:
            s[AT] = ts
    assert client.get("/home").status_code == 200


def test_the_resume_keeps_the_path_percent_encoded(app, svc, monkeypatch):  # noqa: F811
    """%3F is part of the path as tapped; decoded, it would come back as a
    query. MUTATION: park request.full_path -> red."""
    client = app.test_client()
    _tap_signed_out(client, "/review/agent%3Fx/act-1?y=%2F")
    _login(client, svc, monkeypatch, "pat@example.com")
    assert (client.get("/home").headers["Location"]
            == "/review/agent%3Fx/act-1?y=%2F")


# --- what the person is told (patient walk of #1016, G7) ---------------------

RESUME_LINE = ("To keep your health information safe, we check it's you "
               "first. Enter the code "
               "we email you and we'll take you back to the page you opened.")
STALE_NOTE = ("That link took a while, so we brought you here. Tap Chat to "
              "pick up where you left off.")


def _later(monkeypatch, seconds):
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + seconds)


def test_auth_says_why_a_code_and_that_the_page_is_kept(app):  # noqa: F811
    """Generic: never the page, its agent or its topic."""
    client = app.test_client()
    _tap_signed_out(client, "/chat?agent=agent_abc&chart=blood%20pressure")
    page = client.get("/auth").get_data(as_text=True)
    assert RESUME_LINE in page
    assert 'data-resume="1"' in page
    assert "blood" not in page and "agent_abc" not in page


def test_auth_promises_no_page_while_a_texted_link_waits(app):  # noqa: F811
    """/home sends the texted link first and drops the page, so the line
    would promise what does not happen. MUTATION: ignore it -> red."""
    client = app.test_client()
    _tap_signed_out(client, "/settings")
    with client.session_transaction() as s:
        s["imessage_link"] = "link_123"
    page = client.get("/auth").get_data(as_text=True)
    assert 'id="resume-why"' not in page and "data-resume" not in page


def test_auth_promises_no_page_while_a_consent_request_waits(app):  # noqa: F811
    """The same for a consent request parked by /authorize (#846).
    MUTATION: ignore it -> red."""
    from tests.test_careagents_signin_return_qa import _handle
    client = app.test_client()
    _tap_signed_out(client, "/settings")
    r = client.get("/authorize", query_string={"req": _handle()})
    assert r.status_code == 302 and r.headers["Location"].endswith("/auth")
    with client.session_transaction() as s:
        assert s.get("consent_req") and s.get(KEY) == "/settings"
    page = client.get("/auth").get_data(as_text=True)
    assert 'id="resume-why"' not in page and "data-resume" not in page


def test_auth_says_nothing_of_a_page_when_none_is_waiting(app):  # noqa: F811
    page = app.test_client().get("/auth").get_data(as_text=True)
    assert 'id="resume-why"' not in page and "data-resume" not in page


def test_auth_says_nothing_of_a_page_gone_stale(app, monkeypatch):  # noqa: F811
    client = app.test_client()
    _tap_signed_out(client, "/settings")
    _later(monkeypatch, 16 * 60)
    page = client.get("/auth").get_data(as_text=True)
    assert 'id="resume-why"' not in page and "data-resume" not in page


def test_the_code_note_sits_above_send_a_new_code(app):  # noqa: F811
    """"We sent a code moments ago… check your inbox" reads before the
    button that asks for another."""
    page = app.test_client().get("/auth").get_data(as_text=True)
    assert page.index('id="code-note"') < page.index('id="resend-code"')


def test_a_page_dropped_at_the_hub_as_stale_says_so_once(
        app, svc, monkeypatch):  # noqa: F811
    client = app.test_client()
    _tap_signed_out(client, "/settings")
    _login(client, svc, monkeypatch, "pat@example.com")
    _later(monkeypatch, 16 * 60)
    home = client.get("/home")
    assert home.status_code == 200
    assert STALE_NOTE in home.get_data(as_text=True)
    # One-time.
    assert STALE_NOTE not in client.get("/home").get_data(as_text=True)


def test_a_page_gone_stale_before_the_code_says_so_at_the_hub(
        app, svc, monkeypatch):  # noqa: F811
    """Dropped at sign-in rather than at the hub: the note still shows."""
    client = app.test_client()
    _tap_signed_out(client, "/settings")
    _later(monkeypatch, 16 * 60)
    _login(client, svc, monkeypatch, "pat@example.com")
    assert STALE_NOTE in client.get("/home").get_data(as_text=True)
    assert STALE_NOTE not in client.get("/home").get_data(as_text=True)


def test_a_fresh_page_or_none_shows_no_stale_note(app, svc, monkeypatch):  # noqa: F811
    client = app.test_client()
    _login(client, svc, monkeypatch, "pat@example.com")
    assert STALE_NOTE not in client.get("/home").get_data(as_text=True)
    client = app.test_client()
    _tap_signed_out(client, "/settings")
    _login(client, svc, monkeypatch, "pat2@example.com")
    assert client.get("/home").status_code == 302
    assert STALE_NOTE not in client.get("/home").get_data(as_text=True)


def test_a_stale_page_dropped_when_imessage_wins_shows_no_note(
        app, svc, monkeypatch):  # noqa: F811
    """Dropped because another step came first, not for staleness."""
    client = app.test_client()
    _tap_signed_out(client, "/settings")
    _later(monkeypatch, 16 * 60)
    _login(client, svc, monkeypatch, "pat@example.com")
    with client.session_transaction() as s:
        s["imessage_link"] = "link_123"
    assert "/settings" not in client.get("/home").headers["Location"]
    with client.session_transaction() as s:
        s.pop("imessage_link", None)
    assert STALE_NOTE not in client.get("/home").get_data(as_text=True)


@pytest.mark.parametrize("ts", [None, "0", True, float("nan")])
def test_an_unusable_time_is_not_called_slow(app, svc, monkeypatch, ts):  # noqa: F811
    client = app.test_client()
    _login(client, svc, monkeypatch, "pat@example.com")
    with client.session_transaction() as s:
        s[KEY] = "/settings"
        if ts is not None:
            s[AT] = ts
    assert STALE_NOTE not in client.get("/home").get_data(as_text=True)
