"""SMART Health Check-in web wallet skeleton (FHI-54, careagents/checkin_wallet.py).

What these pin:

- Off by default, and off means absent: no route, a 404.
- The page is not behind a redirect to /auth (a redirect would lose the
  check-in page's request); it signs in where it stands, and a session a
  confirm link opened does not count as signed in.
- No PHI reaches CareAgents: the routes are GET-only, a visit writes no
  row and logs no record content, and the browser code's only network calls
  are the sign-in endpoints and a GET of the synthetic test patient.
- The hand-off survives: no Cross-Origin-Opener-Policy, the standard CSP,
  the library is the vendored 0.4.4 release byte for byte with its license.
- "No known allergies" is not sent as an allergy: the browser's code list is
  the chat's own (careagents/agent.py).

The answer rules themselves (unavailable, declined, unsupported, fulfilled)
run in a browser: e2e/tests/careagents-checkin-wallet.spec.ts.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select, table

from careagents.config import Config

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "careagents" / "static"
VENDOR = STATIC / "vendor" / "smart-health-checkin-client" / "0.4.4"
FIXTURE = ROOT / "careagents" / "checkin" / "us_core_test_patient.json"

#: sha256 of wallet.js in the v0.4.4 GitHub release
#: (smart-health-checkin/client), identical to the hosted
#: https://smart-health-checkin.org/client/lib/0.4.4/wallet.js.
WALLET_JS_SHA256 = (
    "891064eda255cb593a538e6bceff96fc66bbf91a6b8bda4b6af6d97e39c6e732")

ROUTES = ("/checkin/wallet", "/checkin/wallet/test-patient.json")


class _NoClient:
    """The wallet never calls HealthClaw; anything that tries fails loudly."""

    def __getattr__(self, name):
        raise AssertionError(f"checkin wallet called HealthClaw: {name}")


def _config(**extra):
    return Config(env={"CARE_DATABASE_URL": "sqlite:///:memory:",
                       "CARE_RP_ID": "localhost",
                       "CARE_ORIGIN": "http://localhost",
                       "OPENAI_API_KEY": "k",
                       "HEALTHCLAW_MINT_SECRET": "mint-secret",
                       **extra})


def _app(cfg):
    from careagents.accounts import AccountService
    from careagents.app import create_app
    svc = AccountService(cfg)
    app = create_app(config=cfg, client=_NoClient(), accounts=svc)
    app.config["TESTING"] = True
    return app, svc


@pytest.fixture
def wallet():
    app, svc = _app(_config(CARE_CHECKIN_WALLET="1"))
    yield app, svc
    svc.engine.dispose()


def _account(svc, monkeypatch, email="wallet-test@example.test"):
    import careagents.mail as mailmod
    got = {}
    monkeypatch.setattr(mailmod, "send_code", lambda cfg, e, code, purpose: (
        got.setdefault("c", code) and mailmod.SENT))
    svc.start_email_code(email)
    return svc.verify_email_code(email, got["c"])


def _sign_in(client, acct, via_link=False):
    with client.session_transaction() as s:
        s["account_id"] = acct.id
        if via_link:
            s["via_link"] = True


# --- the flag ----------------------------------------------------------------

def test_flag_is_off_by_default_and_reads_the_usual_words():
    assert _config().checkin_wallet_enabled is False
    for on in ("1", "true", "yes", "on", "TRUE"):
        assert _config(CARE_CHECKIN_WALLET=on).checkin_wallet_enabled is True
    for off in ("", "0", "false", "no"):
        assert _config(CARE_CHECKIN_WALLET=off).checkin_wallet_enabled is False


def test_flag_off_every_wallet_route_is_a_404():
    app, svc = _app(_config())
    try:
        assert not [r for r in app.url_map.iter_rules()
                    if r.rule.startswith("/checkin")]
        client = app.test_client()
        for path in ROUTES:
            assert client.get(path).status_code == 404, path
    finally:
        svc.engine.dispose()


# --- signing in where it stands ------------------------------------------------

def test_signed_out_the_page_renders_inline_sign_in_not_a_redirect(wallet):
    app, _ = wallet
    r = app.test_client().get("/checkin/wallet")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'data-signed-in="0"' in html
    assert 'id="wallet-signin"' in html
    assert app.test_client().get(ROUTES[1]).status_code == 401


def test_signed_in_the_page_starts_and_the_test_patient_is_served(wallet,
                                                                monkeypatch):
    app, svc = wallet
    client = app.test_client()
    _sign_in(client, _account(svc, monkeypatch))
    html = client.get("/checkin/wallet").get_data(as_text=True)
    assert 'data-signed-in="1"' in html
    r = client.get(ROUTES[1])
    assert r.status_code == 200
    assert r.mimetype == "application/fhir+json"
    assert r.get_data() == FIXTURE.read_bytes()


def test_a_confirm_link_session_is_not_signed_in_here(wallet, monkeypatch):
    app, svc = wallet
    client = app.test_client()
    _sign_in(client, _account(svc, monkeypatch), via_link=True)
    assert 'data-signed-in="0"' in client.get(
        "/checkin/wallet").get_data(as_text=True)
    assert client.get(ROUTES[1]).status_code == 401


# --- the hand-off's headers ------------------------------------------------------

def test_no_opener_policy_standard_csp_and_nothing_cached(wallet):
    from careagents.security_headers import CONTENT_SECURITY_POLICY
    app, _ = wallet
    for path in ROUTES:
        r = app.test_client().get(path)
        # window.opener must survive into this tab, or there is no hand-off.
        assert "Cross-Origin-Opener-Policy" not in r.headers, path
        assert r.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        assert r.headers["Cache-Control"] == "no-store"


def test_the_page_loads_the_wallet_as_a_same_origin_module(wallet):
    app, _ = wallet
    html = app.test_client().get("/checkin/wallet").get_data(as_text=True)
    assert re.search(r'<script type="module" src="/static/checkin_wallet\.js',
                     html)
    for path in (STATIC / "checkin_wallet.js", STATIC / "checkin_answer.js"):
        imports = re.findall(r'from\s+"([^"]+)"', path.read_text())
        assert imports and all(i.startswith("./") for i in imports), imports


# --- no PHI in CareAgents ---------------------------------------------------------

def test_every_wallet_route_is_read_only(wallet):
    app, _ = wallet
    rules = [r for r in app.url_map.iter_rules()
             if r.rule.startswith("/checkin")]
    assert sorted(r.rule for r in rules) == sorted(ROUTES)
    for r in rules:
        assert r.methods <= {"GET", "HEAD", "OPTIONS"}, (r.rule, r.methods)


def test_a_visit_writes_no_row_and_logs_no_record(wallet, monkeypatch, caplog):
    app, svc = wallet
    client = app.test_client()
    _sign_in(client, _account(svc, monkeypatch))

    def counts():
        names = inspect(svc.engine).get_table_names()
        with svc.engine.connect() as c:
            return {n: c.execute(select(func.count()).select_from(table(n)))
                    .scalar() for n in names}

    before = counts()
    caplog.set_level(logging.DEBUG)
    for _ in range(2):
        for path in ROUTES:
            assert client.get(path).status_code == 200
    assert counts() == before
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for marker in ("Testpatient", "FAKE-MEMBER", "FAKE-MRN", "checkin-test-"):
        assert marker not in logged


def _code(path):
    """Browser code without its comments."""
    src = path.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def test_the_browser_code_sends_nothing_but_sign_in(wallet):
    page = _code(STATIC / "checkin_wallet.js")
    answer = _code(STATIC / "checkin_answer.js")
    for src in (page, answer):
        for banned in ("localStorage", "sessionStorage", "indexedDB",
                       "sendBeacon", "console.", "XMLHttpRequest",
                       "WebSocket", "EventSource"):
            assert banned not in src, banned
    assert "fetch(" not in answer
    # Two fetches: the sign-in POST helper, and the GET of the test patient.
    assert page.count("fetch(") == 2
    assert set(re.findall(r'post\("([^"]+)"', page)) == {
        "/webauthn/login/options", "/webauthn/login/verify",
        "/api/auth/email", "/api/auth/verify"}


# --- the library, vendored -----------------------------------------------------------

def test_the_vendored_library_is_the_0_4_4_release_with_its_license():
    assert hashlib.sha256(
        (VENDOR / "wallet.js").read_bytes()).hexdigest() == WALLET_JS_SHA256
    lic = (VENDOR / "LICENSE").read_text()
    assert "Apache License" in lic and "Version 2.0" in lic


# --- what the page says ---------------------------------------------------------------

def test_the_requester_is_a_website_never_a_clinic(wallet):
    app, _ = wallet
    html = app.test_client().get("/checkin/wallet").get_data(as_text=True)
    assert "A website is asking for your health information" in html
    text = re.sub(r"<style.*?</style>|<[^>]+>", " ", html, flags=re.S)
    strings = " ".join(re.findall(r'"[^"\n]*"|`[^`]*`', _code(STATIC / "checkin_wallet.js")))
    shown = (text + strings).lower()
    for word in ("clinic", "provider", "doctor", "practice"):
        assert word not in shown, word


def test_no_known_allergy_codes_match_the_chat():
    from careagents.agent import _NKA_CODES
    src = (STATIC / "checkin_answer.js").read_text()
    block = re.search(r"NKA_CODES = \[(.*?)\]", src, re.S).group(1)
    js = set(re.findall(r'"(\d+)"', block))
    assert js == {code for _, code in _NKA_CODES}
