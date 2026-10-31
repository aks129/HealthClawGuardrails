"""Security round on the sample new-patient form (#1073, PR #1076).

Server-side pins the PR's own tests leave out: the page carries the full
security header set (CSP, no framing, nosniff, referrer policy), the
Verifier library hashes to the pinned release *as served*, and the hub
link is gone for a paused, signed-out or deleted account.
"""

import hashlib
import re

import pytest

from careagents.security_headers import CONTENT_SECURITY_POLICY
from tests.test_checkin_sample import CHECKIN_JS_SHA256, SAMPLE
from tests.test_checkin_wallet import _account, _app, _config, _sign_in

pytestmark = pytest.mark.extension

LINK = "Try a sample new-patient form"


@pytest.fixture
def wallet():
    app, svc = _app(_config(CARE_CHECKIN_WALLET="1",
                            CARE_CHECKIN_TEST_PATIENT="1"))
    yield app, svc
    svc.engine.dispose()


def test_the_sample_page_carries_the_full_header_set(wallet):
    app, _ = wallet
    r = app.test_client().get(SAMPLE)
    assert r.status_code == 200
    assert r.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "connect-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert r.headers["Cache-Control"] == "no-store"
    # No Set-Cookie: a visit starts no session.
    assert "Set-Cookie" not in r.headers


def test_head_is_no_store_and_post_is_refused(wallet):
    app, _ = wallet
    c = app.test_client()
    assert c.head(SAMPLE).headers["Cache-Control"] == "no-store"
    assert c.post(SAMPLE).status_code == 405


def test_the_page_echoes_nothing_from_the_request(wallet):
    app, _ = wallet
    html = app.test_client().get(
        SAMPLE + "?next=https://evil.example/&q=SEC1076CANARY",
        headers={"Referer": "https://evil.example/SEC1076CANARY"},
    ).get_data(as_text=True)
    assert "evil.example" not in html
    assert "SEC1076CANARY" not in html
    # Its only same-site link goes home; no redirect target is read.
    assert re.findall(r'<a [^>]*href="/[^"]*"', html) and all(
        'href="/home"' in a or 'href="/"' in a
        for a in re.findall(r'<a [^>]*href="/[^"]*"', html))


def test_the_verifier_library_hashes_to_the_release_as_served(wallet):
    app, _ = wallet
    r = app.test_client().get(
        "/static/vendor/smart-health-checkin-client/0.4.4/checkin.js")
    assert r.status_code == 200
    assert r.mimetype in ("text/javascript", "application/javascript")
    assert hashlib.sha256(r.get_data()).hexdigest() == CHECKIN_JS_SHA256


def test_paused_account_sees_no_sample_link(wallet, monkeypatch):
    app, svc = wallet
    client = app.test_client()
    acct = _account(svc, monkeypatch, "sec1076-paused@example.test")
    _sign_in(client, acct)
    assert LINK in client.get("/home").get_data(as_text=True)
    assert svc.set_paused("sec1076-paused@example.test", True)
    hub = client.get("/home")
    assert hub.status_code == 200
    assert LINK not in hub.get_data(as_text=True)
    assert 'id="sample-form-section"' not in hub.get_data(as_text=True)
    # The page itself is public by design; the relay is the gate.
    assert client.post("/checkin/wallet/records",
                       json={"resource_types": ["AllergyIntolerance"]}
                       ).status_code == 403


def test_signed_out_and_deleted_accounts_get_no_hub(wallet, monkeypatch):
    app, svc = wallet
    client = app.test_client()
    r = client.get("/home")
    assert r.status_code in (302, 303) and "/auth" in r.headers["Location"]
    acct = _account(svc, monkeypatch, "sec1076-deleted@example.test")
    _sign_in(client, acct)
    assert LINK in client.get("/home").get_data(as_text=True)
    assert svc.delete_account(acct.id)
    r = client.get("/home")
    assert r.status_code in (302, 303) and "/auth" in r.headers["Location"]
    # A deleted account's old cookie is not signed in for the relay either.
    assert client.post("/checkin/wallet/records",
                       json={"resource_types": ["AllergyIntolerance"]}
                       ).status_code == 401

