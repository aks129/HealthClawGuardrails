"""Attacks on the guest wallet, GET /checkin/try-wallet (#1108).

Security review pins. Each test is an attempt to make the guest page reach
something real; all of them are expected to fail to do so:

- the inlined records cannot break out of their <script> block;
- a signed-in visitor's page is byte-identical to a stranger's, so no
  email, account id or record of theirs reaches it;
- a stranger's visit runs no database statement at all, and nobody's
  visit writes one;
- no header, cookie or query string changes what the page carries;
- the route answers GET only.

The browser side (no call to the relay, test patient, sign-in or /api,
with a signed-in cookie present) was checked against a running server;
the e2e spec covers it without the cookie. Synthetic data only.
"""
from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import event

from careagents import checkin_wallet
from tests.test_checkin_wallet import _account, _app, _config, _sign_in

pytestmark = pytest.mark.extension

PAGE = "/checkin/try-wallet"
BOTH = {"CARE_CHECKIN_WALLET": "1", "CARE_CHECKIN_GUEST_WALLET": "1"}
EMAIL = "guest-attack-canary@example.test"
BLOCK = re.compile(r'<script type="application/json" '
                   r'id="wallet-guest-records">(.*?)</script>', re.S)


@pytest.fixture
def guest():
    app, svc = _app(_config(**BOTH))
    yield app, svc
    svc.engine.dispose()


def _statements(svc):
    seen = []
    event.listen(svc.engine, "before_cursor_execute",
                 lambda conn, cur, stmt, *a: seen.append(stmt.strip()))
    return seen


def test_inlined_records_cannot_close_their_script_block(guest, monkeypatch):
    app, _ = guest
    hostile = checkin_wallet.test_patient_bundle()
    hostile["entry"][0]["resource"]["id"] = (
        "</script><script>window.pwned=1</script><!-- & ' \" <x>")
    monkeypatch.setattr(checkin_wallet, "test_patient_bundle",
                        lambda: json.loads(json.dumps(hostile)))
    html = app.test_client().get(PAGE).get_data(as_text=True)
    assert "window.pwned=1</script>" not in html
    assert "<script>window.pwned" not in html
    assert html.count('id="wallet-guest-records"') == 1
    assert json.loads(BLOCK.search(html).group(1)) == hostile


def test_signed_in_visitor_page_is_byte_identical_to_a_strangers(
        guest, monkeypatch):
    app, svc = guest
    stranger = app.test_client().get(PAGE).get_data()
    c = app.test_client()
    acct = _account(svc, monkeypatch, email=EMAIL)
    _sign_in(c, acct)
    assert c.get("/checkin/wallet").get_data(as_text=True).count(
        'data-signed-in="1"') == 1
    mine = c.get(PAGE).get_data()
    assert mine == stranger
    text = mine.decode()
    assert EMAIL not in text and acct.id not in text


def test_a_strangers_visit_runs_no_database_statement(guest):
    app, svc = guest
    seen = _statements(svc)
    r = app.test_client().get(PAGE)
    assert r.status_code == 200
    assert seen == []
    assert "Set-Cookie" not in r.headers


def test_a_signed_in_visit_writes_nothing(guest, monkeypatch):
    app, svc = guest
    c = app.test_client()
    _sign_in(c, _account(svc, monkeypatch, email=EMAIL))
    seen = _statements(svc)
    assert c.get(PAGE).status_code == 200
    writes = [s for s in seen
              if s.split(None, 1)[0].upper() in ("INSERT", "UPDATE", "DELETE")]
    assert writes == []


@pytest.mark.parametrize("headers", [
    {"Cookie": "guest=0; signed_in=1"},
    {"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https"},
    {"Referer": "https://evil.example/?records=/checkin/wallet/records"},
    {"X-Original-URL": "/checkin/wallet", "X-Rewrite-URL": "/checkin/wallet"},
])
def test_no_header_changes_the_guest_page(guest, headers):
    app, _ = guest
    plain = app.test_client().get(PAGE).get_data()
    assert app.test_client().get(PAGE, headers=headers).get_data() == plain


@pytest.mark.parametrize("q", [
    "?records=/checkin/wallet/records",
    "?test_patient=1&guest_records=%3C/script%3E",
    "?signed_in=1&guest=0&wait=1",
])
def test_no_query_changes_what_the_page_carries(guest, q):
    app, _ = guest
    plain = app.test_client().get(PAGE).get_data()
    assert app.test_client().get(PAGE + q).get_data() == plain


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_the_guest_route_is_get_only(guest, method):
    app, _ = guest
    assert getattr(app.test_client(), method)(PAGE, json={}).status_code == 405
