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


# --- 4b. no number while the import is still writing (QA940-F1) --------------
# The engine writes a Fasten export in a background thread, ten resources
# per commit, and a Patient lands first. The first poll used to store the
# count at that moment, and the card read "29 records" for good while the
# engine went on to hold 150. A count is stored only once the import is done.

def _stored(svc, conn):
    from careagents.models import Connection
    with svc.session() as s:
        row = s.get(Connection, conn)
        return row.status, row.last_count


def test_the_poll_shows_no_count_until_the_import_is_done(cfg, svc,
                                                          monkeypatch):
    """MUTATION: drop the import check in poll_connection -> the first poll
    stores 29 and the card reads "29 records" with the engine at 150."""
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert c.post("/api/connections/sample").status_code == 200
    conn, tenant = _start_fasten(c)
    fake.importing = True
    for n in (29, 80):
        fake.counted = n
        d = c.get(f"/api/connections/{tenant}/poll").get_json()
        assert d == {"status": "pending"}, d
        assert _stored(svc, conn) == ("pending", None)
        html = c.get("/home").get_data(as_text=True)
        assert f"{n} records" not in html
        assert 'id="switch-prompt"' not in html
    fake.importing = False
    fake.counted = 150
    assert c.get(f"/api/connections/{tenant}/poll").get_json()[
        "status"] == "active"
    assert _stored(svc, conn) == ("active", 150)
    html = c.get("/home").get_data(as_text=True)
    assert "150 records" in _card(html, conn)
    assert "150 records arrived." in html


def test_the_chat_page_shows_no_count_until_the_import_is_done(cfg, svc,
                                                               monkeypatch):
    """MUTATION: drop the import check on the chat-settle path -> the first
    chat load stores 29 for good."""
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    own = c.post("/api/agents", json={
        "name": "Rowan", "persona": "calm",
        "connection_id": conn}).get_json()["id"]
    fake.search = lambda t, rt, params=None: {"total": {
        "Condition": 2, "Observation": 5}.get(rt, 0)}
    fake.importing = True
    for n in (29, 80):
        fake.counted = n
        page = c.get(f"/chat?agent={own}").get_data(as_text=True)
        assert _stored(svc, conn) == ("pending", None)
        # Nor in the greeting: no count while the import runs.
        assert "I found" not in page
        assert "still arriving" in page
    fake.importing = False
    fake.counted = 150
    page = c.get(f"/chat?agent={own}").get_data(as_text=True)
    assert _stored(svc, conn) == ("active", 150)
    assert "I found <strong>2 conditions</strong>" in page


def test_the_chat_page_settles_nothing_when_it_cannot_ask(cfg, svc,
                                                         monkeypatch):
    """MUTATION: settle on a failed import look -> a count stored while the
    import may still be running."""
    from careagents.healthclaw import HealthClawError
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    own = c.post("/api/agents", json={
        "name": "Rowan", "persona": "calm",
        "connection_id": conn}).get_json()["id"]
    fake.search = lambda t, rt, params=None: {"total": {
        "Condition": 2, "Observation": 5}.get(rt, 0)}

    def down(tenant):
        raise HealthClawError("down")
    fake.fasten_import_running = down
    page = c.get(f"/chat?agent={own}").get_data(as_text=True)
    assert _stored(svc, conn) == ("pending", None)
    assert "I found" not in page


def test_a_refresh_reports_no_new_records_until_the_import_is_done(
        cfg, svc, monkeypatch):
    """The refresh watcher stops at the first "N new records", so a delta
    taken mid-import froze on the page the same way."""
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    c.get(f"/api/connections/{tenant}/poll")
    assert c.post(f"/api/connections/{conn}/refresh").status_code == 200
    fake.importing = True
    fake.counted = 112
    d = c.get(f"/api/connections/{tenant}/poll").get_json()
    assert d == {"status": "active"}, d
    fake.importing = False
    fake.counted = 150
    assert c.get(f"/api/connections/{tenant}/poll").get_json()[
        "new_records"] == 50


def test_an_unknown_import_state_is_said_never_counted(cfg, svc, monkeypatch):
    """Could not ask whether the import is done: the same "we couldn't
    reach your records" as any other failed look (#403), never a count."""
    from careagents.healthclaw import HealthClawError
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)

    def down(tenant):
        raise HealthClawError("down")
    fake.fasten_import_running = down
    r = c.get(f"/api/connections/{tenant}/poll")
    assert r.status_code == 503
    assert r.get_json()["status"] == "unavailable"
    assert _stored(svc, conn) == ("pending", None)


def test_only_a_fasten_connection_asks_about_an_import(cfg, svc,
                                                       monkeypatch):
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    asked = []
    fake.fasten_import_running = lambda t: asked.append(t) or False
    sample = c.post("/api/connections/sample").get_json()["id"]
    sample_tenant = {x["id"]: x for x in svc.list_home(
        _account(svc))["connections"]}[sample]["tenant_id"]
    c.get(f"/api/connections/{sample_tenant}/poll")
    assert asked == []
    _, tenant = _start_fasten(c)
    c.get(f"/api/connections/{tenant}/poll")
    assert asked == [tenant]


def _account(svc, email="gene@example.com"):
    from careagents.models import Account
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).one().id


# --- the client: what "still importing" means at the engine ------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def _client_answering(status, body):
    from careagents.healthclaw import HealthClawClient
    seen = {}

    class _Session:
        def get(self, url, params=None, headers=None, timeout=None, **_):
            seen.update(url=url, headers=headers)
            return _Resp(status, body)

        def post(self, url, json=None, headers=None, timeout=None, **_):
            return _Resp(200, {"token": "tok"})

    client = HealthClawClient(base="http://engine", mint_secret="s")
    client.http = _Session()
    client.mint_token = lambda tenant: "tok"
    return client, seen


def test_any_job_not_finished_is_an_import_running():
    for statuses, running in ((["complete", "failed"], False),
                              ([], False),
                              (["complete", "ingesting"], True),
                              (["pending"], True),
                              (["downloading"], True),
                              ([None], True)):
        client, seen = _client_answering(
            200, {"jobs": [{"status": s} for s in statuses]})
        assert client.fasten_import_running("ca-1") is running, statuses
    assert seen["url"] == "http://engine/fasten/jobs"
    assert seen["headers"]["X-Tenant-Id"] == "ca-1"


def test_a_failed_or_malformed_job_answer_raises():
    import pytest

    from careagents.healthclaw import HealthClawError
    for status, body in ((401, {"error": "x"}), (503, {}),
                         (200, {"jobs": "nope"}), (200, ["x"])):
        client, _ = _client_answering(status, body)
        with pytest.raises(HealthClawError):
            client.fasten_import_running("ca-1")


def test_cross_layer_the_engine_answers_the_import_question(monkeypatch):
    """The real client on the real engine WSGI: the route, the tenant
    credential and the job statuses line up."""
    import requests as _requests

    from careagents.healthclaw import HealthClawClient
    from main import create_app as engine_create_app
    from models import db

    secret = "cross-layer-internal-secret"
    tenant = "ca-xl-import"
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", secret)
    # Production's read gate, so the tenant credential is really checked.
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    engine_app = engine_create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "LEGACY_BOOT_ON_CREATE": False,
    })
    with engine_app.app_context():
        db.create_all()
    engine = engine_app.test_client()

    def _to_requests(resp):
        r = _requests.Response()
        r.status_code = resp.status_code
        r._content = resp.get_data() or b""
        r.headers.update(resp.headers.to_wsgi_list())
        return r

    class _RelaySession:
        def post(self, url, json=None, headers=None, timeout=None, data=None):
            return _to_requests(engine.post(url.replace("http://engine", ""),
                                            json=json, data=data,
                                            headers=headers or {}))

        def get(self, url, params=None, headers=None, timeout=None):
            return _to_requests(engine.get(url.replace("http://engine", ""),
                                           query_string=params or {},
                                           headers=headers or {}))

    real = HealthClawClient(base="http://engine", mint_secret=secret)
    real.http = _RelaySession()
    assert real.fasten_import_running(tenant) is False
    # Without the credential the engine refuses, and that is not "idle".
    assert engine.get("/fasten/jobs",
                      headers={"X-Tenant-Id": tenant}).status_code == 401

    from r6.fasten.models import FastenJob
    with engine_app.app_context():
        db.session.add(FastenJob(task_id="t-xl-1", org_connection_id="oc-1",
                                 tenant_id=tenant, status="ingesting"))
        db.session.commit()
    assert real.fasten_import_running(tenant) is True
    with engine_app.app_context():
        FastenJob.query.filter_by(task_id="t-xl-1").update(
            {"status": "complete"})
        db.session.commit()
    assert real.fasten_import_running(tenant) is False


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
    # Signed in already: one more check, not "Sign in" (patient tester,
    # 2026-10-16). The link itself still waits for it (#928 F6).
    assert "Email me a code for my invite link" in section
    assert "Sign in" not in section
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


# --- QA940 round 2: Refresh mid-import, the banner, the connect link ---------

def test_a_refresh_during_the_first_import_stores_no_baseline(cfg, svc,
                                                              monkeypatch):
    """QA940-F1b. MUTATION: drop the import check in refresh_connection ->
    49 is stored, the poll never stores again, the card reads 49 for good."""
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    fake.importing = True
    fake.counted = 49
    assert c.post(f"/api/connections/{conn}/refresh").status_code == 200
    assert _stored(svc, conn) == ("pending", None)
    fake.importing = False
    fake.counted = 150
    assert c.get(f"/api/connections/{tenant}/poll").get_json()[
        "status"] == "active"
    assert _stored(svc, conn) == ("active", 150)
    assert "150 records" in _card(c.get("/home").get_data(as_text=True), conn)


def test_a_refresh_during_a_later_import_keeps_the_last_baseline(
        cfg, svc, monkeypatch):
    """A second import on an active connection: a mid-import baseline would
    make "N new records" a figure about part of an import."""
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    fake.counted = 100
    c.get(f"/api/connections/{tenant}/poll")
    assert _stored(svc, conn) == ("active", 100)
    fake.importing = True
    fake.counted = 130
    assert c.post(f"/api/connections/{conn}/refresh").status_code == 200
    assert _stored(svc, conn) == ("active", 100)
    fake.importing = False
    fake.counted = 160
    assert c.get(f"/api/connections/{tenant}/poll").get_json()[
        "new_records"] == 60


def test_a_refresh_that_cannot_ask_about_the_import_stores_nothing(
        cfg, svc, monkeypatch):
    from careagents.healthclaw import HealthClawError
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, _tenant = _start_fasten(c)

    def down(tenant):
        raise HealthClawError("down")
    fake.fasten_import_running = down
    r = c.post(f"/api/connections/{conn}/refresh")
    assert r.status_code == 503
    assert _stored(svc, conn) == ("pending", None)


def test_a_connecting_fasten_card_has_no_refresh(cfg, svc, monkeypatch):
    """Refresh on a card still connecting was the way into F1b, and the
    connect link is that card's way forward."""
    app, fake = _app(cfg, svc)
    fake.tenant_has_records = lambda tenant: False
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    assert "conn-refresh" not in _card(c.get("/home").get_data(as_text=True),
                                       conn)
    fake.tenant_has_records = lambda tenant: True
    c.get(f"/api/connections/{tenant}/poll")
    assert 'class="conn-refresh"' in _card(
        c.get("/home").get_data(as_text=True), conn)


def test_the_connect_link_goes_once_an_import_is_running(cfg, svc,
                                                         monkeypatch):
    """The person has done their part; "Not finished?" then reads as a
    mistake. A failed or unknown look keeps the link: it is the fallback."""
    from careagents.healthclaw import HealthClawError
    app, fake = _app(cfg, svc)
    fake.tenant_has_records = lambda tenant: False
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, _tenant = _start_fasten(c)
    fake.importing = True
    assert "open-connect-page" not in _card(
        c.get("/home").get_data(as_text=True), conn)
    fake.importing = False
    assert "open-connect-page" in _card(
        c.get("/home").get_data(as_text=True), conn)

    def down(tenant):
        raise HealthClawError("down")
    fake.fasten_import_state = down
    card = _card(c.get("/home").get_data(as_text=True), conn)
    assert "Not finished?" in card and "open-connect-page" in card


def test_the_chat_banner_says_records_are_still_arriving_mid_import(
        cfg, svc, monkeypatch):
    """The assistant answers from what has landed, so "Nothing below
    reflects them" was untrue mid-import. Nothing-landed keeps its copy."""
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, _tenant = _start_fasten(c)
    own = c.post("/api/agents", json={
        "name": "Rowan", "persona": "calm",
        "connection_id": conn}).get_json()["id"]
    fake.search = lambda t, rt, params=None: {"total": {
        "Observation": 5}.get(rt, 0)}
    fake.importing = True
    page = " ".join(c.get(f"/chat?agent={own}").get_data(
        as_text=True).split())
    assert ("Your records are still arriving. Answers may miss some until "
            "they finish.") in page
    assert "Nothing below reflects them" not in page
    assert "haven't arrived yet" not in page
    # Nothing landed at all: the existing sentence, unchanged.
    fake.search = lambda t, rt, params=None: {"total": 0}
    page = " ".join(c.get(f"/chat?agent={own}").get_data(
        as_text=True).split())
    assert "haven't arrived yet" in page
    assert "still arriving" not in page


# --- QA940 round 3: what a connecting card says ------------------------------

def test_a_connecting_card_says_the_records_are_on_their_way(cfg, svc,
                                                             monkeypatch):
    """A static line while the import runs: no number, no progress."""
    app, fake = _app(cfg, svc)
    fake.tenant_has_records = lambda tenant: False
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, _tenant = _start_fasten(c)
    line = ("Your records are on their way. This can take up to 45 "
            "minutes.")
    fake.importing = True
    card = " ".join(_card(c.get("/home").get_data(as_text=True),
                          conn).split())
    assert line in card
    assert "didn't work" not in card
    fake.importing = False
    card = " ".join(_card(c.get("/home").get_data(as_text=True),
                          conn).split())
    assert line not in card


def test_a_failed_import_says_so_and_offers_the_connect_page(cfg, svc,
                                                             monkeypatch):
    """The person finished their part; "Not finished?" blamed them for a
    failure on our side or the provider's (QA940 round 3)."""
    app, fake = _app(cfg, svc)
    fake.tenant_has_records = lambda tenant: False
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    fake.import_failed = True
    card = " ".join(_card(c.get("/home").get_data(as_text=True),
                          conn).split())
    assert "Bringing in your records didn't work." in card
    assert (f'class="open-connect-page" href="{fake.base}/connect/{tenant}"'
            in card)
    assert "Try connecting again" in card
    assert "Not finished?" not in card
    assert "on their way" not in card


def test_the_import_state_reads_the_newest_job():
    for statuses, state in ((["failed"], "failed"),
                            (["failed", "complete"], "failed"),
                            (["complete", "failed"], "done"),
                            (["ingesting", "failed"], "running"),
                            (["failed", "pending"], "running"),
                            ([], "none")):
        client, _ = _client_answering(
            200, {"jobs": [{"status": x} for x in statuses]})
        assert client.fasten_import_state("ca-1") == state, statuses
        assert client.fasten_import_running("ca-1") is (state == "running")


# --- patient tester on #940 ---------------------------------------------------

def _chip(card):
    import html
    return html.unescape(re.search(r'<span class="status[^"]*">([^<]*)</span>',
                                   card).group(1))


def test_a_failed_import_chip_agrees_with_its_note(cfg, svc, monkeypatch):
    """The chip said "Connecting…" under "didn't work": wait, or try again?
    MUTATION: render status_word on a failed import -> "Connecting…"."""
    app, fake = _app(cfg, svc)
    fake.tenant_has_records = lambda tenant: False
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, _tenant = _start_fasten(c)
    assert _chip(_card(c.get("/home").get_data(as_text=True),
                       conn)) == "Connecting…"
    fake.import_failed = True
    card = _card(c.get("/home").get_data(as_text=True), conn)
    assert _chip(card) == "Didn't finish"
    assert "Connecting" not in card


def test_the_doctor_card_reads_plainly(cfg, svc, monkeypatch):
    """"Reads Records from your doctor", and a subtitle repeating the title
    ("Records from your doctor" / "your doctor")."""
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn, tenant = _start_fasten(c)
    c.post("/api/agents", json={"name": "Rowan", "persona": "calm",
                                "connection_id": conn})
    c.get(f"/api/connections/{tenant}/poll")
    html = c.get("/home").get_data(as_text=True)
    assert "Reads records from your doctor" in html
    assert "Reads Records" not in html
    card = html[html.index(f'data-conn="{conn}"'):]
    card = card[:card.index("</div>", card.index("hub-card-sub")) + 6]
    assert '<div class="hub-card-sub"></div>' in card
