"""The connectathon test patient, chosen on the check-in wallet page.

A visiting Verifier team signs in to the wallet fresh, so their account has
no records to share. Behind CARE_CHECKIN_TEST_PATIENT (and the wallet's own
flag), POST /checkin/wallet/test-patient gives that account one tenant
seeded from the synthetic US Core test patient
(careagents/checkin/us_core_test_patient.json), and the relay answers from
it. What these pin:

- Both flags, a full sign-in (#920 F1), a JSON body of exactly `{}` (the
  CSRF guard), not paused. Nothing from the browser chooses the records.
- Offered only to an account with no real-record connection, and made at
  most once per account: a second call makes nothing.
- The test patient is a `sample` connection (a fixed provider marks it), so
  every `kind != "sample"` gate that opens real-record features stays shut
  (`has_real_connection` among them), and account deletion purges it as it
  purges every connection.
- The relay prefers a real connection; it answers from the test patient
  only when there is none and the flag is on. A plain sample is still
  `sample_only`.
- On the real engine: the seeded tenant answers all six check-in types,
  each with the US Core claim the request matches on.

Synthetic data only.
"""
from __future__ import annotations

import json

import pytest

from careagents import checkin_wallet
from careagents.healthclaw import HealthClawClient, HealthClawError
from tests import checkin_fixtures as f
from tests.test_checkin_relay import _Engine, _counts
from tests.test_checkin_wallet import _account, _config, _sign_in

ROUTE = "/checkin/wallet/test-patient"
RELAY = "/checkin/wallet/records"
FLAGS = {"CARE_CHECKIN_WALLET": "1", "CARE_CHECKIN_TEST_PATIENT": "1"}
BUNDLE = b'{"resourceType": "Bundle", "type": "collection", "entry": []}'


class _FakeClient:
    """Records the calls made; proves a call is made, not accepted (the
    real-engine test below is that)."""

    def __init__(self, seed_error=None):
        self.seed_error = seed_error
        self.seeded, self.exports, self.purged = [], [], []
        self._n = 0

    def new_tenant_id(self):
        self._n += 1
        return f"ca-test-{self._n}"

    def seed(self, tenant, bundle=None):
        self.seeded.append((tenant, bundle))
        if self.seed_error is not None:
            raise self.seed_error
        return 0

    def record_count(self, tenant):
        return 9

    def purge_tenant(self, tenant):
        self.purged.append(tenant)
        return {}

    def checkin_export(self, tenant, resource_types):
        self.exports.append(tenant)
        return BUNDLE


def _careagents(client, **env):
    from careagents.accounts import AccountService
    from careagents.app import create_app
    cfg = _config(**env)
    svc = AccountService(cfg)
    app = create_app(config=cfg, client=client, accounts=svc)
    app.config["TESTING"] = True
    return app, svc


@pytest.fixture
def tester(monkeypatch):
    """A fresh account, signed in, with nothing connected."""
    hc = _FakeClient()
    app, svc = _careagents(hc, **FLAGS)
    acct = _account(svc, monkeypatch)
    browser = app.test_client()
    _sign_in(browser, acct)
    yield browser, hc, svc, acct
    svc.engine.dispose()


def _choose(browser, body=None):
    return browser.post(ROUTE, json={} if body is None else body)


def _test_conns(svc, acct):
    return [c for c in svc.list_home(acct.id)["connections"]
            if c["provider"] == checkin_wallet.TEST_PROVIDER]


# --- the flag -------------------------------------------------------------------

def test_the_flag_is_off_by_default_and_reads_the_usual_words():
    assert _config().checkin_test_patient_enabled is False
    for on in ("1", "true", "yes", "on"):
        assert _config(CARE_CHECKIN_TEST_PATIENT=on
                       ).checkin_test_patient_enabled is True
    for off in ("", "0", "false", "no"):
        assert _config(CARE_CHECKIN_TEST_PATIENT=off
                       ).checkin_test_patient_enabled is False


@pytest.mark.parametrize("env", [
    {"CARE_CHECKIN_WALLET": "1"},
    {"CARE_CHECKIN_TEST_PATIENT": "1"},
])
def test_without_both_flags_there_is_no_route_and_no_offer(env, monkeypatch):
    hc = _FakeClient()
    app, svc = _careagents(hc, **env)
    try:
        acct = _account(svc, monkeypatch)
        browser = app.test_client()
        _sign_in(browser, acct)
        assert _choose(browser).status_code == 404
        page = browser.get("/checkin/wallet")
        assert b"data-test-patient" not in page.data
        assert hc.seeded == []
    finally:
        svc.engine.dispose()


def test_with_both_flags_the_page_carries_the_offer(tester):
    browser, *_ = tester
    page = browser.get("/checkin/wallet").get_data(as_text=True)
    assert f'data-test-patient="{ROUTE}"' in page
    assert "Use the connectathon test patient" in page


# --- the gate -------------------------------------------------------------------

def test_signed_out_or_by_confirm_link_it_is_refused(tester):
    browser, hc, svc, acct = tester
    app = browser.application
    assert _choose(app.test_client()).status_code == 401
    linked = app.test_client()
    _sign_in(linked, acct, via_link=True)
    assert _choose(linked).status_code == 401
    assert hc.seeded == [] and _test_conns(svc, acct) == []


def test_a_form_post_is_refused(tester):
    browser, hc, *_ = tester
    r = browser.post(ROUTE, data={"x": "1"})
    assert r.status_code == 415
    assert r.headers["Cache-Control"] == "no-store"
    assert hc.seeded == []


@pytest.mark.parametrize("body", [
    {"tenant_id": "someone-else"},
    {"origin": "https://clinic.example"},
    {"bundle": {"resourceType": "Bundle"}},
    [],
    "x",
])
def test_the_body_chooses_nothing(tester, body):
    browser, hc, *_ = tester
    r = _choose(browser, body)
    assert r.status_code == 400
    assert r.get_json() == {"error": "bad_request"}
    assert hc.seeded == []


def test_a_paused_account_is_refused(tester):
    browser, hc, svc, acct = tester
    svc.set_paused(acct.email, True)
    r = _choose(browser)
    assert r.status_code == 403 and r.get_json() == {"error": "paused"}
    assert hc.seeded == []


def test_an_account_with_real_records_is_not_offered_it(tester):
    browser, hc, svc, acct = tester
    svc.add_connection(acct.id, "direct", "ca-real", "Upload")
    r = _choose(browser)
    assert r.status_code == 409 and r.get_json() == {"error": "has_records"}
    assert hc.seeded == [] and _test_conns(svc, acct) == []


def test_a_mint_already_running_is_not_doubled(tester):
    browser, hc, svc, acct = tester
    assert svc.claim_sample_start(acct.id)
    r = _choose(browser)
    assert r.status_code == 409 and r.get_json() == {"error": "busy"}
    assert hc.seeded == []


# --- what it makes --------------------------------------------------------------

def test_it_seeds_one_tenant_from_the_fixture_once(tester):
    browser, hc, svc, acct = tester
    before = len(svc.list_home(acct.id)["connections"])
    r = _choose(browser)
    assert r.status_code == 200
    # No tenant id, nothing about the records, reaches the browser.
    assert r.get_json() == {"ready": True}
    assert r.headers["Cache-Control"] == "no-store"
    [(tenant, bundle)] = hc.seeded
    assert bundle == checkin_wallet.test_patient_bundle()
    assert bundle == json.loads(checkin_wallet.TEST_PATIENT_FILE.read_text())
    [conn] = _test_conns(svc, acct)
    assert conn["tenant_id"] == tenant
    assert conn["kind"] == "sample" and conn["status"] == "active"
    assert len(svc.list_home(acct.id)["connections"]) == before + 1

    # Once per account: the second choice makes nothing.
    again = _choose(browser)
    assert again.status_code == 200 and again.get_json() == {"ready": True}
    assert len(hc.seeded) == 1 and len(_test_conns(svc, acct)) == 1
    # The lease went back.
    assert svc.claim_sample_start(acct.id)


def test_the_test_patient_never_counts_as_real_records(tester):
    browser, hc, svc, acct = tester
    assert _choose(browser).status_code == 200
    assert svc.has_real_connection(acct.id) is False
    assert svc.has_connections(acct.id) is True


def test_the_hub_still_works_for_an_account_holding_only_the_test_patient(
        tester):
    """It is the account's visible sample afterwards: the hub renders it and
    the sample tap answers, rather than a second mint or a 500."""
    browser, hc, svc, acct = tester
    assert _choose(browser).status_code == 200
    assert browser.get("/").status_code == 200
    r = browser.post("/api/connections/sample", json={})
    assert r.status_code == 200, r.get_data(as_text=True)
    # The existing one opens, with an assistant to open it in, as a sample
    # tap's own sample does.
    assert r.get_json()["existing"] is True
    assert r.get_json().get("redirect")
    assert len(hc.seeded) == 1


def test_a_seed_that_fails_leaves_nothing_behind(monkeypatch):
    hc = _FakeClient(seed_error=HealthClawError("seed failed (503)", 503))
    app, svc = _careagents(hc, **FLAGS)
    try:
        acct = _account(svc, monkeypatch)
        browser = app.test_client()
        _sign_in(browser, acct)
        r = _choose(browser)
        assert r.status_code == 502
        assert r.get_json() == {"error": "unavailable"}
        assert svc.list_home(acct.id)["connections"] == []
        # Whatever landed before the failure is purged, and the lease is
        # back so the person can try again.
        assert hc.purged == [hc.seeded[0][0]]
        assert svc.claim_sample_start(acct.id)
    finally:
        svc.engine.dispose()


def test_deleting_the_account_purges_the_test_patient(tester):
    browser, hc, svc, acct = tester
    assert _choose(browser).status_code == 200
    [conn] = _test_conns(svc, acct)
    r = browser.post("/api/account/delete", json={"confirm": "DELETE"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert conn["tenant_id"] in hc.purged
    assert svc.get_account(acct.id) is None


# --- the relay ------------------------------------------------------------------

def test_the_relay_answers_from_the_test_patient(tester):
    browser, hc, svc, acct = tester
    r = browser.post(RELAY, json={"resource_types": ["Patient"]})
    assert r.status_code == 409 and r.get_json() == {"error": "no_records"}
    assert _choose(browser).status_code == 200
    [conn] = _test_conns(svc, acct)
    r = browser.post(RELAY, json={"resource_types": ["Patient"]})
    assert r.status_code == 200
    assert hc.exports == [conn["tenant_id"]]


@pytest.mark.parametrize("kind,status,someone_else,why", [
    ("direct", "empty", False, "records_not_ready"),
    ("fasten", "pending", False, "records_not_ready"),
    ("direct", "error", False, "records_not_ready"),
    ("direct", "revoked", False, "records_disconnected"),
    ("direct", "active", True, "not_your_records"),
    ("fasten", "revoked", True, "not_your_records"),
])
def test_real_records_not_ready_are_named_and_never_offered(
        tester, kind, status, someone_else, why):
    """Any non-sample connection on the account, not yet one the relay can
    answer from, is named for what it is (still coming, disconnected, or
    someone else's), and is never an offer the route refuses (#1027 QA F1,
    #1030 QA wording)."""
    browser, hc, svc, acct = tester
    person = svc.add_person(acct.id, "parent") if someone_else else None
    svc.add_connection(acct.id, kind, "ca-waiting", "Records", status=status,
                       person_id=person)
    svc.add_connection(acct.id, "sample", "ca-sample", "Sample records",
                       provider="CareAgents sample")
    r = browser.post(RELAY, json={"resource_types": ["Patient"]})
    assert r.status_code == 409
    assert r.get_json() == {"error": why}
    assert _choose(browser).get_json() == {"error": "has_records"}


def test_ones_own_waiting_records_outrank_disconnected_and_others(tester):
    browser, hc, svc, acct = tester
    other = svc.add_person(acct.id, "parent")
    svc.add_connection(acct.id, "direct", "ca-theirs", "Records",
                       person_id=other)
    svc.add_connection(acct.id, "direct", "ca-gone", "Upload",
                       status="revoked")
    r = browser.post(RELAY, json={"resource_types": ["Patient"]})
    assert r.get_json() == {"error": "records_disconnected"}
    svc.add_connection(acct.id, "fasten", "ca-coming", "Doctor",
                       status="pending")
    r = browser.post(RELAY, json={"resource_types": ["Patient"]})
    assert r.get_json() == {"error": "records_not_ready"}
    assert hc.exports == [] and hc.seeded == []


def test_the_page_offers_the_test_patient_only_where_it_can_be_made():
    """The offer's reasons are the relay's two that the route accepts, and
    the refusals it does not offer on have words, none of them "a few
    minutes"."""
    import re
    from pathlib import Path
    js = (Path(checkin_wallet.__file__).parent / "static"
          / "checkin_wallet.js").read_text()
    offer = re.search(r"const OFFER_TEST = \{(.*?)\n\};", js, re.S).group(1)
    assert set(re.findall(r"^\s*(\w+):", offer, re.M)) == {
        "no_records", "sample_only"}
    words = re.search(r"const NO_RECORDS = \{(.*?)\n\};", js, re.S).group(1)
    for code in ("records_not_ready", "records_disconnected",
                 "not_your_records", "has_records"):
        line = re.search(rf"^\s*{code}: \"([^\"]+)\"", words, re.M)
        assert line, code
        assert "few minutes" not in line.group(1)


def test_a_plain_sample_is_still_sample_only(tester):
    browser, hc, svc, acct = tester
    svc.add_connection(acct.id, "sample", "ca-sample", "Sample records",
                       provider="CareAgents sample")
    r = browser.post(RELAY, json={"resource_types": ["Patient"]})
    assert r.status_code == 409 and r.get_json() == {"error": "sample_only"}
    # With the plain sample there, the test patient is still offered.
    assert _choose(browser).status_code == 200
    assert browser.post(RELAY, json={"resource_types": ["Patient"]}
                        ).status_code == 200


def test_real_records_win_over_the_test_patient(tester):
    browser, hc, svc, acct = tester
    assert _choose(browser).status_code == 200
    svc.add_connection(acct.id, "direct", "ca-real", "Upload")
    assert browser.post(RELAY, json={"resource_types": ["Patient"]}
                        ).status_code == 200
    assert hc.exports == ["ca-real"]


def test_flag_off_the_relay_ignores_the_test_patient(monkeypatch):
    """Turning the flag off stops the test patient answering, whatever the
    account already holds."""
    hc = _FakeClient()
    app, svc = _careagents(hc, CARE_CHECKIN_WALLET="1")
    try:
        acct = _account(svc, monkeypatch)
        svc.add_connection(acct.id, "sample", "ca-test-old",
                           checkin_wallet.TEST_LABEL,
                           provider=checkin_wallet.TEST_PROVIDER)
        browser = app.test_client()
        _sign_in(browser, acct)
        r = browser.post(RELAY, json={"resource_types": ["Patient"]})
        assert r.status_code == 409 and r.get_json() == {"error": "sample_only"}
        assert hc.exports == []
    finally:
        svc.engine.dispose()


# --- on the real engine ---------------------------------------------------------

def test_the_seeded_test_patient_answers_every_checkin_type(app, client,
                                                            monkeypatch):
    """The real client seeds the real engine through the route, and the relay
    exports each of the six types with the claim a check-in matches on."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f.MINT_SECRET)
    engine = _Engine(client)
    hc = HealthClawClient(base="http://engine", mint_secret=f.MINT_SECRET)
    hc.http = engine
    ca, svc = _careagents(hc, **FLAGS)
    try:
        acct = _account(svc, monkeypatch)
        browser = ca.test_client()
        _sign_in(browser, acct)
        r = _choose(browser)
        assert r.status_code == 200, r.get_data(as_text=True)
        [conn] = _test_conns(svc, acct)
        seeds = [s for s in engine.sent if s[1] == "/r6/fhir/internal/seed"]
        assert len(seeds) == 1
        assert seeds[0][3]["X-Tenant-Id"] == conn["tenant_id"]

        before = _counts(svc)
        types = sorted(checkin_wallet.CHECKIN_TYPES)
        r = browser.post(RELAY, json={"resource_types": types})
        assert r.status_code == 200, r.get_data(as_text=True)
        got = {}
        for e in r.get_json()["entry"]:
            res = e["resource"]
            got.setdefault(res["resourceType"], []).append(
                (res.get("meta") or {}).get("profile") or [])
        us_core = "http://hl7.org/fhir/us/core/StructureDefinition/"
        want = {
            "Patient": "us-core-patient",
            "Condition": "us-core-condition-problems-health-concerns",
            "AllergyIntolerance": "us-core-allergyintolerance",
            "MedicationRequest": "us-core-medicationrequest",
            "Immunization": "us-core-immunization",
            "Coverage": "us-core-coverage",
        }
        for rtype, profile in want.items():
            assert rtype in got, rtype
            assert any(us_core + profile in p for p in got[rtype]), rtype
        # Nothing about the export is kept on the CareAgents side.
        assert _counts(svc) == before
    finally:
        svc.engine.dispose()
