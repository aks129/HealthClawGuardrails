"""QA #973: relay rules the PR's own tests let through under mutation.

Each test here was written against a mutation that left
tests/test_checkin_relay.py green:

- the 1 KB body cap, removed outright (the existing oversize test pads with
  an extra key, so the key check refuses it first);
- the engine's 413 mapped to `too_large` (only the fake client raised it);
- a 200 from the engine that is not a Bundle;
- the patient looked up fresh on each export, not from the labs cache.

Synthetic data only (tests/checkin_fixtures.py).
"""
from __future__ import annotations

import pytest

from careagents.healthclaw import HealthClawClient
from tests import checkin_fixtures as f
from tests.test_checkin_relay import (  # noqa: F401  (fixtures)
    RELAY, _account, _careagents, _Engine, _sign_in, chain, relay)


def test_a_well_formed_body_over_the_cap_is_refused(relay):  # noqa: F811
    """Only the one allowed key, so nothing but the size cap can refuse it."""
    browser, hc, *_ = relay
    body = {"resource_types": ["Patient"] * 200}
    r = browser.post(RELAY, json=body)
    assert r.status_code == 400
    assert r.get_json() == {"error": "bad_request"}
    assert hc.calls == []


def test_a_second_patient_after_an_export_is_not_guessed(
        app, client, chain):  # noqa: F811
    """The patient is looked up on every export. A tenant that gains a
    second Patient after one export refuses the next, on the same client."""
    browser, engine, _ = chain
    assert browser.post(RELAY, json={"resource_types": ["Patient"]}
                        ).status_code == 200
    f.seed(app, f.patient(f.SIBLING, given="Bo"))
    r = browser.post(RELAY, json={"resource_types": ["Patient"]})
    assert r.status_code == 409
    assert r.get_json() == {"error": "no_patient"}
    exports = [s for s in engine.sent if s[1] == "/r6/fhir/$share-bundle"]
    assert len(exports) == 1


def test_an_export_over_the_engines_cap_reaches_the_browser_as_too_large(
        app, client, monkeypatch):
    """On the real engine: a record set over r6/checkin.py MAX_BUNDLE_BYTES
    is a 413 there, and the wallet is told `too_large`, not `unavailable`."""
    from r6.checkin import MAX_BUNDLE_BYTES
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f.MINT_SECRET)
    big = f.patient()
    big["address"] = [{"line": ["9 Synthetic Way " + "x" * MAX_BUNDLE_BYTES],
                       "city": "Testville", "state": "WI"}]
    f.seed(app, big)
    engine = _Engine(client)
    hc = HealthClawClient(base="http://engine", mint_secret=f.MINT_SECRET)
    hc.http = engine
    ca, svc = _careagents(hc)
    try:
        acct = _account(svc, monkeypatch)
        svc.add_connection(acct.id, "direct", f.TENANT, "Upload")
        browser = ca.test_client()
        _sign_in(browser, acct)
        r = browser.post(RELAY, json={"resource_types": ["Patient"]})
        assert r.status_code == 413
        assert r.get_json() == {"error": "too_large"}
        assert r.headers["Cache-Control"] == "no-store"
    finally:
        svc.engine.dispose()


class _NotABundle:
    """An engine double that answers the export 200 with something that is
    not a Bundle (an interstitial's JSON, an OperationOutcome)."""

    def __init__(self, body):
        self.body = body

    def get(self, url, params=None, headers=None, timeout=None):
        import requests
        r = requests.Response()
        r.status_code = 200
        r._content = (b'{"resourceType": "Bundle", "entry": [{"resource": '
                      b'{"resourceType": "Patient", "id": "ci-pt-1"}}]}')
        return r

    def post(self, url, json=None, headers=None, timeout=None, data=None):
        import requests
        r = requests.Response()
        r.status_code = 200
        r._content = (b'{"token": "tok"}' if url.endswith("step-up-token")
                      else self.body)
        return r


@pytest.mark.parametrize("body", [
    b'{"resourceType": "OperationOutcome", "issue": []}',
    b'{"entry": []}',
])
def test_a_200_that_is_not_a_bundle_is_unavailable(monkeypatch, body):
    hc = HealthClawClient(base="http://engine", mint_secret=f.MINT_SECRET)
    hc.http = _NotABundle(body)
    ca, svc = _careagents(hc)
    try:
        acct = _account(svc, monkeypatch)
        svc.add_connection(acct.id, "direct", "ca-relay-one", "Upload")
        browser = ca.test_client()
        _sign_in(browser, acct)
        r = browser.post(RELAY, json={"resource_types": ["Patient"]})
        assert r.status_code == 502
        assert r.get_json() == {"error": "unavailable"}
        assert body not in r.get_data()
    finally:
        svc.engine.dispose()
