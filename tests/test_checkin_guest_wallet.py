"""The guest wallet, GET /checkin/try-wallet (CARE_CHECKIN_GUEST_WALLET).

The same wallet page and script with no sign-in, for a Verifier team to
try without an account. What these pin:

- Off unless both its flag and the wallet's own are on; off is a 404.
- Guest mode is the route's, set on the card by the server. A query
  string never turns it on for /checkin/wallet, nor off here.
- The guest page carries no relay or test-patient address, sets no
  cookie, and is never cached or indexed. No Cross-Origin-Opener-Policy,
  as on the wallet: the hand-off needs window.opener.
- Its records are the synthetic US Core test patient's file, unchanged,
  inlined in the page: the wallet script keeps its one fetch.
- The signed-in /checkin/wallet is as it was.

In a browser (never the relay, only the test patient, every item at
"Don't share"): e2e/tests/careagents-checkin-guest-wallet.spec.ts.
Synthetic data only.
"""
from __future__ import annotations

import json
import re

import pytest

from careagents import checkin_wallet
from tests.test_checkin_wallet import _account, _app, _config, _sign_in

pytestmark = pytest.mark.extension

PAGE = "/checkin/try-wallet"
BOTH = {"CARE_CHECKIN_WALLET": "1", "CARE_CHECKIN_GUEST_WALLET": "1"}


@pytest.fixture
def guest():
    app, svc = _app(_config(**BOTH))
    yield app, svc
    svc.engine.dispose()


def test_the_flag_is_off_by_default_and_reads_the_usual_words():
    assert _config().checkin_guest_wallet_enabled is False
    for on in ("1", "true", "yes", "on"):
        assert _config(CARE_CHECKIN_GUEST_WALLET=on
                       ).checkin_guest_wallet_enabled is True


@pytest.mark.parametrize("env", [
    {},
    {"CARE_CHECKIN_WALLET": "1"},
    {"CARE_CHECKIN_GUEST_WALLET": "1"},
])
def test_off_is_a_404(env):
    app, svc = _app(_config(**env))
    try:
        c = app.test_client()
        assert c.get(PAGE).status_code == 404
    finally:
        svc.engine.dispose()


def test_the_page_is_the_wallet_in_guest_mode(guest):
    app, _ = guest
    r = app.test_client().get(PAGE)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'data-guest="1"' in html
    assert "checkin_wallet.js" in html
    # No relay, no test patient, no sign-in to show.
    assert "/checkin/wallet/records" not in html
    assert "/checkin/wallet/test-patient" not in html
    assert "data-records=" not in html
    assert '<section id="wallet-signin" hidden>' in html
    # Made up, said at the top, in the existing words.
    assert "made-up\n      patient's records, not yours" in html


def test_the_page_headers(guest):
    app, _ = guest
    r = app.test_client().get(PAGE)
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Robots-Tag"] == "noindex"
    assert "Cross-Origin-Opener-Policy" not in r.headers
    # No session written for a visitor who arrived with none.
    assert "Set-Cookie" not in r.headers


def test_the_records_are_the_test_patient_file_unchanged(guest):
    app, _ = guest
    html = app.test_client().get(PAGE).get_data(as_text=True)
    got = re.search(r'<script type="application/json" '
                    r'id="wallet-guest-records">(.*?)</script>', html, re.S)
    assert json.loads(got.group(1)) == checkin_wallet.test_patient_bundle()


def test_a_signed_in_visitor_still_gets_the_guest_page(guest, monkeypatch):
    app, svc = guest
    c = app.test_client()
    _sign_in(c, _account(svc, monkeypatch))
    html = c.get(PAGE).get_data(as_text=True)
    assert 'data-guest="1"' in html
    assert "data-records=" not in html


def test_no_query_string_switches_the_mode(guest, monkeypatch):
    app, svc = guest
    c = app.test_client()
    for q in ("?guest=0", "?guest=", "?signed_in=1", "?guest=false"):
        assert 'data-guest="1"' in c.get(PAGE + q).get_data(as_text=True)
    _sign_in(c, _account(svc, monkeypatch))
    for q in ("", "?guest=1", "?guest=true", "?mode=guest"):
        html = c.get("/checkin/wallet" + q).get_data(as_text=True)
        assert "data-guest" not in html
        assert 'data-records="/checkin/wallet/records"' in html


def test_the_signed_in_wallet_is_unchanged_with_the_flag_on(monkeypatch):
    pages = []
    for env in ({"CARE_CHECKIN_WALLET": "1"}, BOTH):
        app, svc = _app(_config(**env))
        try:
            c = app.test_client()
            _sign_in(c, _account(svc, monkeypatch))
            r = c.get("/checkin/wallet")
            assert r.headers["Cache-Control"] == "no-store"
            assert "X-Robots-Tag" not in r.headers
            pages.append(r.get_data(as_text=True))
        finally:
            svc.engine.dispose()
    assert pages[0] == pages[1]
    assert 'data-signed-in="1"' in pages[1]
