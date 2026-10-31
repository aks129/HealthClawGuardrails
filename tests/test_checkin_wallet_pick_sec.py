"""Security review of the record-set pick (PR #1040): what the PR's own tests
leave open.

- The relay-body pin in test_checkin_wallet.py now accepts any body whose
  only *dot* assignment is `body.connection_id`. `body["request"] = request`,
  `Object.assign(body, {...})` and a changed value for connection_id all
  pass it (mutation-tested). The e2e pick spec catches them, but only in a
  browser; this pins the exact body construction without one.
- A confirm-link session or a paused account sending an id, and an engine
  failure on a picked set: each refused or answered with a fixed code, no
  engine call where none is due, no tenant id in the body.

Synthetic data only.
"""
from __future__ import annotations

import re

from careagents.healthclaw import HealthClawError
from tests.test_checkin_relay import _careagents, _FakeClient, _post
from tests.test_checkin_wallet import STATIC, _account, _code, _sign_in
import pytest

pytestmark = pytest.mark.extension

TYPES = ["Patient", "Coverage"]


def _two(monkeypatch, hc=None):
    hc = hc or _FakeClient()
    app, svc = _careagents(hc)
    acct = _account(svc, monkeypatch)
    a = svc.add_connection(acct.id, "direct", "ca-sec-one", "Uploaded records",
                           provider="Direct upload")
    b = svc.add_connection(acct.id, "fasten", "ca-sec-two",
                           "Records from your doctor", provider="your doctor")
    return app, svc, hc, acct, a, b


# --- the browser builds the relay body one way only ---------------------------

def test_the_relay_body_is_types_and_only_the_picked_id():
    page = _code(STATIC / "checkin_wallet.js")
    # Built in one place, from the types, then at most the picked id.
    assert page.count("const body = { resource_types: types };") == 1
    assert page.count(
        "if (connectionId) body.connection_id = connectionId;") == 1
    # No other way to put a key on it.
    assert re.findall(r"\bbody\s*\[", page) == []
    assert re.findall(r"Object\.assign\(\s*body\b", page) == []
    assert re.findall(r"\.\.\.\s*body\b", page) == []
    assert re.findall(r"\bbody\.(\w+)\s*=", page) == ["connection_id"]
    # connectionId is loadRecords' own parameter, and the only call that
    # passes one passes the picker's answer.
    assert re.findall(r"loadRecords\(([^)]*)\)", page) == [
        "request, unsupportedItems, connectionId",   # the definition
        "request, unsupportedItems",                 # the test patient
        "request, unsupportedItems",                 # the first ask
        "request, unsupportedItems, picked",         # after the pick
    ]
    assert page.count("const picked = await pickRecords(choices);") == 1
    # The picker resolves to a choice's id from the server, or null.
    assert re.findall(r"resolve\(([^)]*)\)", page).count("c.id") == 1


# --- sessions that must not pick ----------------------------------------------

def test_a_confirm_link_session_cannot_pick_its_own_set(monkeypatch):
    app, svc, hc, acct, a, b = _two(monkeypatch)
    try:
        browser = app.test_client()
        _sign_in(browser, acct, via_link=True)
        for body in ({"resource_types": TYPES},
                     {"resource_types": TYPES, "connection_id": a}):
            r = _post(browser, body)
            assert r.status_code == 401
            assert "choices" not in (r.get_json() or {})
        assert hc.calls == []
    finally:
        svc.engine.dispose()


def test_a_paused_account_gets_no_list_and_no_pick(monkeypatch):
    app, svc, hc, acct, a, b = _two(monkeypatch)
    try:
        svc.set_paused(acct.email, True)
        browser = app.test_client()
        _sign_in(browser, acct)
        for body in ({"resource_types": TYPES},
                     {"resource_types": TYPES, "connection_id": a}):
            r = _post(browser, body)
            assert r.status_code == 403
            assert r.get_json() == {"error": "paused"}
        assert hc.calls == []
    finally:
        svc.engine.dispose()


# --- failures on a picked set fail closed ------------------------------------

def test_an_engine_failure_on_a_picked_set_is_a_fixed_code(monkeypatch):
    err = HealthClawError("checkin token mint failed (500) ca-sec-two", 500)
    app, svc, hc, acct, a, b = _two(monkeypatch, _FakeClient(error=err))
    try:
        browser = app.test_client()
        _sign_in(browser, acct)
        r = _post(browser, {"resource_types": TYPES, "connection_id": b})
        assert r.status_code == 502
        assert r.get_json() == {"error": "unavailable"}
        assert "ca-sec-" not in r.get_data(as_text=True)
        assert [c[0] for c in hc.calls] == ["ca-sec-two"]
    finally:
        svc.engine.dispose()


# --- two connections on one tenant --------------------------------------------

def test_the_newer_of_two_connections_on_one_tenant_is_not_a_back_door(
        monkeypatch):
    """Only the oldest connection per tenant is listed; the newer one's id
    is refused like any other id not on the list, even though it points at
    the same records."""
    app, svc, hc, acct, a, b = _two(monkeypatch)
    try:
        dup = svc.add_connection(acct.id, "direct", "ca-sec-one", "Again")
        browser = app.test_client()
        _sign_in(browser, acct)
        assert [c["id"] for c in _post(browser).get_json()["choices"]] == [a, b]
        r = _post(browser, {"resource_types": TYPES, "connection_id": dup})
        assert r.get_json() == {"error": "unknown_records"}
        assert hc.calls == []
    finally:
        svc.engine.dispose()
