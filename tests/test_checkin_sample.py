"""The sample new-patient form (#1073): a made-up clinic's check-in page on
CareAgents itself, so a first-timer can watch the wallet answer a form
without visiting any outside site.

It is a requester like any other: it sends a real SMART Health Check-in
request to /checkin/wallet with the organizers' Verifier library, and the
answer comes back to its own tab. These tests pin the server side: the
route exists only with the wallet, it is read-only and not cached, the
links to it, the vendored library, and that the page's script keeps the
answer in the tab. The round trip in a browser:
e2e/tests/careagents-checkin-sample.spec.ts.
"""

import hashlib
import re

import pytest

from tests.test_checkin_wallet import (
    STATIC, VENDOR, _account, _app, _code, _config, _sign_in,
)

SAMPLE = "/checkin/sample"

#: sha256 of checkin.js in the v0.4.4 GitHub release
#: (smart-health-checkin/client), the Verifier side; the same bytes
#: e2e/fixtures/smart-health-checkin-client/0.4.4/checkin.js holds and
#: e2e/tests/sec-960-checkin-wallet.spec.ts pins.
CHECKIN_JS_SHA256 = (
    "ad0ad34e28b19c7a5bae6305b1be4a9b98b56f83b4309665054c33b425d4f33c")


@pytest.fixture
def wallet():
    app, svc = _app(_config(CARE_CHECKIN_WALLET="1"))
    yield app, svc
    svc.engine.dispose()


def test_flag_off_there_is_no_sample_form_and_no_link(monkeypatch):
    app, svc = _app(_config())
    try:
        client = app.test_client()
        assert client.get(SAMPLE).status_code == 404
        _sign_in(client, _account(svc, monkeypatch))
        assert "Try a sample new-patient form" not in client.get(
            "/home").get_data(as_text=True)
    finally:
        svc.engine.dispose()


def test_the_page_is_a_made_up_clinic_and_says_so(wallet):
    app, _ = wallet
    r = app.test_client().get(SAMPLE)
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    assert "Cross-Origin-Opener-Policy" not in r.headers
    html = " ".join(r.get_data(as_text=True).split())
    assert "made-up clinic" in html
    assert "Nothing is sent anywhere else" in html
    # Patient-facing words: none of ours.
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ",
                  html, flags=re.S).lower()
    for word in ("wallet", "connectathon"):
        assert word not in text, word


def test_the_page_loads_the_verifier_library_from_this_site(wallet):
    app, _ = wallet
    html = app.test_client().get(SAMPLE).get_data(as_text=True)
    assert re.search(r'<script type="module" src="/static/checkin_sample\.js'
                     r'\?v=[^"]+"', html)
    page = _code(STATIC / "checkin_sample.js")
    # Imported up front, so the tap that opens the answer tab is still the
    # person's own tap when it does.
    assert ('from "./vendor/smart-health-checkin-client/0.4.4/checkin.js"'
            in page)
    assert "/checkin/wallet" in page


def test_the_vendored_verifier_is_the_0_4_4_release():
    assert hashlib.sha256((VENDOR / "checkin.js").read_bytes()
                          ).hexdigest() == CHECKIN_JS_SHA256


def test_the_answer_stays_in_the_tab():
    page = _code(STATIC / "checkin_sample.js")
    for banned in ("fetch(", "localStorage", "sessionStorage", "indexedDB",
                   "sendBeacon", "console.", "XMLHttpRequest", "WebSocket",
                   "EventSource", "innerHTML", "document.cookie"):
        assert banned not in page, banned


def test_a_visit_writes_nothing(wallet):
    app, svc = wallet
    rules = [r for r in app.url_map.iter_rules() if r.rule == SAMPLE]
    assert len(rules) == 1
    assert rules[0].methods <= {"GET", "HEAD", "OPTIONS"}


def test_the_hub_and_the_empty_wallet_link_to_it(wallet, monkeypatch):
    app, svc = wallet
    client = app.test_client()
    html = " ".join(client.get("/checkin/wallet").get_data(as_text=True)
                    .split())
    direct = html[html.index('id="wallet-direct"'):]
    direct = direct[:direct.index("</section>")]
    assert f'href="{SAMPLE}"' in direct
    assert "Try a sample new-patient form" in direct
    _sign_in(client, _account(svc, monkeypatch))
    hub = client.get("/home").get_data(as_text=True)
    assert re.search(rf'<a[^>]*href="{SAMPLE}"[^>]*>Try a sample new-patient '
                     r"form</a>", hub)
