"""The connect journey after the 2026-10-12 beta shakeout (#937).

- A pending Fasten card links to its connect page, so a blocked popup is
  never a dead end; home.js opens the window on the tap itself.
- The first records on a connection store the count the card shows.
- "Switch to your records" says the records arrived, with the count.
- Settings in a confirm-link session says how to get the invite link.
- The confirm page and the welcome email say a code is coming elsewhere.

Synthetic data only. The engine half (the patient connect page) is in
tests/test_fasten_connect_patient_view.py.
"""

from __future__ import annotations

# ruff: noqa: F811

import pathlib
import re

from careagents import beta_signup
from tests.test_careagents import (  # noqa: F401  (fixtures)
    FakeClient, _login, cfg, consented, svc)
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _confirm_token, _post, made, sent)


def _app(cfg, svc):
    from careagents.app import create_app
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    return app, fake


def _start_fasten(c):
    created = c.post("/api/connections/fasten", json=consented()).get_json()
    return created["id"], created["connect_url"].rsplit("/connect/", 1)[1]


def _card(html, conn_id):
    start = html.index(f'data-conn="{conn_id}"')
    return html[start:html.index("conn-refresh-msg", start)]


# --- 4. the count, and the switch -------------------------------------------

def test_first_records_store_the_count_the_card_shows(cfg, svc, monkeypatch):
    """MUTATION: drop the mark_synced in poll_connection -> red."""
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    fake.counted = 37
    d = c.get(f"/api/connections/{tenant}/poll").get_json()
    assert d["status"] == "active"
    # First activation: no refresh baseline, so no "new records" delta.
    assert "new_records" not in d
    assert "37 records" in _card(c.get("/home").get_data(as_text=True), conn)


def test_a_failed_count_on_activation_is_left_unset(cfg, svc, monkeypatch):
    from careagents.healthclaw import HealthClawError
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)

    def down(tenant):
        raise HealthClawError("down")
    fake.record_count = down
    assert c.get(f"/api/connections/{tenant}/poll").get_json()[
        "status"] == "active"
    card = _card(c.get("/home").get_data(as_text=True), conn)
    assert "0 records" not in card and "records ·" not in card


def test_activation_never_overwrites_a_refresh_baseline(cfg, svc, monkeypatch):
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    assert c.post(f"/api/connections/{conn}/refresh").status_code == 200
    fake.counted = 112
    d = c.get(f"/api/connections/{tenant}/poll").get_json()
    assert d["new_records"] == 12


def test_the_switch_prompt_says_the_records_arrived(cfg, svc, monkeypatch):
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert c.post("/api/connections/sample").status_code == 200
    _, tenant = _start_fasten(c)
    fake.counted = 37
    c.get(f"/api/connections/{tenant}/poll")
    html = c.get("/home").get_data(as_text=True)
    prompt = html[html.index('id="switch-prompt"'):]
    prompt = prompt[:prompt.index("</section>")]
    assert "Your records are here." in prompt
    assert "37 records arrived." in prompt
    assert re.search(r'class="btn-primary" id="switch-yes">Switch \w+ to '
                     r'your records</button>', prompt), prompt


# --- 2. a blocked popup is never a dead end ----------------------------------

def test_a_connecting_fasten_card_links_to_its_connect_page(cfg, svc,
                                                            monkeypatch):
    app, fake = _app(cfg, svc)
    fake.tenant_has_records = lambda tenant: False
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    card = _card(c.get("/home").get_data(as_text=True), conn)
    assert "Connecting" in card
    assert (f'class="open-connect-page" href="{fake.base}/connect/{tenant}"'
            in card)
    assert 'rel="noopener"' in card


def test_a_connected_card_has_no_connect_link(cfg, svc, monkeypatch):
    app, _ = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    c.get(f"/api/connections/{tenant}/poll")
    assert "open-connect-page" not in _card(
        c.get("/home").get_data(as_text=True), conn)


def _js():
    return (pathlib.Path(__file__).resolve().parents[1] / "careagents" /
            "static" / "home.js").read_text()


def test_home_js_opens_the_window_on_the_tap_not_after_the_await():
    """iOS blocks a window.open that runs after an await. The only
    window.open is the empty one opened inside the tap; every connect and
    reauth page goes through sendTo, which offers a link when blocked."""
    js = _js()
    assert js.count("window.open(") == 1
    assert 'window.open("", "_blank")' in js
    assert "agree.onclick = () => { if (onAgree) onAgree(); done(true); };" in js
    assert "sendTo(page, res.d.connect_url, tile" in js
    assert "sendTo(page, res.d.reauth_url, null, msg)" in js
    assert '"Open the connect page"' in js


# --- 3. Settings in a confirm-link session -----------------------------------

def test_settings_in_a_link_session_says_how_to_get_the_invite_link(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c, _ = _join_plain(app, sent, "linky@example.com")
    html = c.get("/settings").get_data(as_text=True)
    section = html[html.index('id="invite-section"'):]
    section = section[:section.index("</section>")]
    assert 'href="/auth?invite=1"' in section
    assert "Sign in to get your invite link" in section
    assert "/i?c=" not in html          # the link itself still waits
    page = c.get("/auth?invite=1").get_data(as_text=True)
    assert 'data-back="/settings#invite-section"' in page
    assert 'id="invite-why"' in page
    assert 'value="linky@example.com"' in page


def test_invite_param_changes_nothing_for_a_full_session(made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = app.test_client()
    _login(c, svc, monkeypatch, email="full@example.com")
    r = c.get("/auth?invite=1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/home")
    html = c.get("/settings").get_data(as_text=True)
    assert "invite-signin" not in html and "/i?c=" in html


def _join_plain(app, sent, email):  # noqa: F811
    """Join from /beta and confirm on our page in this browser: a
    confirm-link session (no invite code needed)."""
    c = app.test_client()
    assert _post(c, email=email, first_name="Robin").status_code == 200
    token = _confirm_token([m for m in sent if m[0] == email][-1][3])
    page = c.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    n = re.search(r'name="n" value="([^"]+)"', page).group(1)
    r = c.post("/beta/confirm", data={"t": token, "n": n})
    assert r.status_code == 303, r.get_data(as_text=True)
    return c, r


# --- 5. a code is coming, said up front --------------------------------------

def test_the_confirm_page_and_welcome_email_say_a_code_is_coming(
        made, sent):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    assert _post(c, email="ahead@example.com").status_code == 200
    token = _confirm_token([m for m in sent if m[0] == "ahead@example.com"][-1][3])
    page = c.get(f"/beta/confirm?t={token}").get_data(as_text=True)
    assert 'id="code-is-coming"' in page
    assert "browser only" in page
    n = re.search(r'name="n" value="([^"]+)"', page).group(1)
    c.post("/beta/confirm", data={"t": token, "n": n})
    welcome = [m for m in sent if m[1].startswith("You're in the beta")]
    assert welcome, [m[1] for m in sent]
    text = " ".join(welcome[-1][3].split())
    assert beta_signup.CODE_IS_COMING in text
