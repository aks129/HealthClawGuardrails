"""The SMART Health Check-in records relay (careagents/checkin_wallet.py).

POST /checkin/wallet/records carries the wallet's list of record types to the
engine's `checkin` export (r6/checkin.py) and the Bundle back. What these pin:

- Behind the wallet flag, a full sign-in and a JSON body (the CSRF guard).
- The body is exactly `{"resource_types": [...]}` from the six: no origin,
  no patient or tenant id, nothing else reaches the engine.
- The tenant is the account holder's one active, non-sample connection;
  anything else is a fixed refusal, never a guess.
- The response is the engine's Bundle with `Cache-Control: no-store`; an
  error is a fixed code. No CareAgents table gains a row, and no body is
  logged.
- On the real engine (not the fake client): one export writes exactly one
  share-bundle AuditEvent, the single-use token never reaches the browser,
  and it cannot be spent twice.
- The type and profile tables the wallet uses equal the engine's.

Synthetic data only (tests/checkin_fixtures.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select, table

from careagents.healthclaw import (HealthClawClient, HealthClawError,
                                   agent_header)
from tests import checkin_fixtures as f
from tests.test_beta_acceptance_rows import _to_requests
from tests.test_checkin_wallet import _account, _config, _sign_in

RELAY = "/checkin/wallet/records"
STATIC = Path(__file__).resolve().parent.parent / "careagents" / "static"
BUNDLE = b'{"resourceType": "Bundle", "type": "collection", "entry": []}'
#: Stands where the engine's body would carry a record value.
CANARY = "CANARY-relay-body-Rivera"


class _FakeClient:
    """Records the one call the relay makes; proves a call is made, not that
    the engine accepts it (that is the cross-layer test below)."""

    def __init__(self, result=BUNDLE, error=None):
        self.result, self.error, self.calls = result, error, []

    def checkin_export(self, tenant, resource_types):
        self.calls.append((tenant, list(resource_types), agent_header()))
        if self.error is not None:
            raise self.error
        return self.result


def _careagents(client, **extra):
    from careagents.accounts import AccountService
    from careagents.app import create_app
    cfg = _config(CARE_CHECKIN_WALLET="1", **extra)
    svc = AccountService(cfg)
    app = create_app(config=cfg, client=client, accounts=svc)
    app.config["TESTING"] = True
    return app, svc


def _counts(svc):
    names = inspect(svc.engine).get_table_names()
    with svc.engine.connect() as c:
        return {n: c.execute(select(func.count()).select_from(table(n)))
                .scalar() for n in names}


@pytest.fixture
def relay(monkeypatch):
    """A signed-in browser whose account holds one uploaded record set."""
    hc = _FakeClient()
    app, svc = _careagents(hc)
    acct = _account(svc, monkeypatch)
    svc.add_connection(acct.id, "direct", "ca-relay-one", "Upload")
    browser = app.test_client()
    _sign_in(browser, acct)
    yield browser, hc, svc, acct
    svc.engine.dispose()


def _post(browser, body=None, **kw):
    if body is None:
        body = {"resource_types": ["Patient", "Coverage"]}
    return browser.post(RELAY, json=body, **kw)


# --- the gate ----------------------------------------------------------------

def test_flag_off_the_relay_is_not_there(monkeypatch):
    from careagents.accounts import AccountService
    from careagents.app import create_app
    cfg = _config()
    svc = AccountService(cfg)
    try:
        app = create_app(config=cfg, client=_FakeClient(), accounts=svc)
        assert _post(app.test_client()).status_code == 404
    finally:
        svc.engine.dispose()


def test_signed_out_or_by_confirm_link_it_is_refused(relay, monkeypatch):
    browser, hc, svc, acct = relay
    app = browser.application
    r = _post(app.test_client())
    assert r.status_code == 401
    linked = app.test_client()
    _sign_in(linked, acct, via_link=True)
    assert _post(linked).status_code == 401
    assert hc.calls == []


def test_a_form_post_is_refused_before_anything_else(relay):
    """A cross-site form cannot send JSON without a preflight."""
    browser, hc, *_ = relay
    r = browser.post(RELAY, data={"resource_types": "Patient"})
    assert r.status_code == 415
    assert r.headers["Cache-Control"] == "no-store"
    assert hc.calls == []


@pytest.mark.parametrize("body", [
    {},
    {"resource_types": []},
    {"resource_types": "Patient"},
    {"resource_types": ["Organization"]},
    {"resource_types": ["Patient", "RelatedPerson"]},
    {"resource_types": ["patient"]},
    {"resource_types": [["Patient"]]},
    # Nothing but the type list: the website's origin, its request and any
    # id the browser might name never reach the engine.
    {"resource_types": ["Patient"], "origin": "https://clinic.example"},
    {"resource_types": ["Patient"], "patient_id": f.SIBLING},
    {"resource_types": ["Patient"], "tenant_id": "ca-someone-else"},
    {"resource_types": ["Patient"], "purpose": "I am your doctor"},
    ["Patient"],
])
def test_anything_but_a_list_of_the_six_is_refused(relay, body):
    browser, hc, *_ = relay
    r = _post(browser, body)
    assert r.status_code == 400
    assert r.get_json() == {"error": "bad_request"}
    assert hc.calls == []


def test_a_paused_account_is_refused(relay):
    browser, hc, svc, acct = relay
    svc.set_paused(acct.email, True)
    assert _post(browser).status_code == 403
    assert hc.calls == []


# --- which records -------------------------------------------------------------

def test_the_one_connection_answers_with_the_sorted_type_set(relay):
    browser, hc, _, acct = relay
    r = _post(browser, {"resource_types": ["Patient", "Coverage", "Patient"]})
    assert r.status_code == 200
    assert hc.calls == [("ca-relay-one", ["Coverage", "Patient"],
                         f"careagents:{acct.id}")]


def test_no_records_but_the_sample_is_a_fixed_refusal(monkeypatch):
    """No records at all, only the made-up sample, and real records not
    ready yet are told apart: the wallet says which (#973 patient test,
    finding 4; #1027 QA F1 split the last one out of `no_records`)."""
    hc = _FakeClient()
    app, svc = _careagents(hc)
    try:
        acct = _account(svc, monkeypatch)
        browser = app.test_client()
        _sign_in(browser, acct)
        assert _post(browser).get_json() == {"error": "no_records"}
        svc.add_connection(acct.id, "sample", "ca-relay-sample", "Sample")
        r = _post(browser)
        assert r.status_code == 409
        assert r.get_json() == {"error": "sample_only"}
        svc.add_connection(acct.id, "direct", "ca-relay-wait", "Upload",
                           status="pending")
        r = _post(browser)
        assert r.status_code == 409
        assert r.get_json() == {"error": "records_not_ready"}
        assert hc.calls == []
    finally:
        svc.engine.dispose()


def test_someone_cared_for_is_not_the_person_checking_in(relay):
    browser, hc, svc, acct = relay
    other = svc.add_person(acct.id, "parent")
    svc.add_connection(acct.id, "direct", "ca-relay-parent", "Upload",
                       person_id=other)
    assert _post(browser).status_code == 200
    assert [c[0] for c in hc.calls] == ["ca-relay-one"]


def test_two_record_sets_are_not_guessed_between(relay):
    browser, hc, svc, acct = relay
    svc.add_connection(acct.id, "fasten", "ca-relay-two", "Portal")
    r = _post(browser)
    assert r.status_code == 409
    assert r.get_json() == {"error": "several_records"}
    assert hc.calls == []


# --- what goes back ------------------------------------------------------------

def test_the_bundle_passes_through_uncached(relay):
    browser, *_ = relay
    r = _post(browser)
    assert r.status_code == 200
    assert r.get_data() == BUNDLE
    assert r.mimetype == "application/fhir+json"
    assert r.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("error, status, code", [
    (HealthClawError(f"export failed {CANARY}", 413,
                     code="checkin_too_large"), 413, "too_large"),
    (HealthClawError(f"no patient {CANARY}", 0, code="no_patient"),
     409, "no_patient"),
    (HealthClawError(f"export failed {CANARY}", 401), 502, "unavailable"),
    (HealthClawError(f"export failed {CANARY}", 0), 502, "unavailable"),
])
def test_an_engine_failure_is_a_fixed_code(monkeypatch, caplog, error,
                                           status, code):
    hc = _FakeClient(error=error)
    app, svc = _careagents(hc)
    try:
        acct = _account(svc, monkeypatch)
        svc.add_connection(acct.id, "direct", "ca-relay-one", "Upload")
        browser = app.test_client()
        _sign_in(browser, acct)
        caplog.set_level(logging.DEBUG)
        r = _post(browser)
        assert r.status_code == status
        assert r.get_json() == {"error": code}
        assert r.headers["Cache-Control"] == "no-store"
        assert CANARY not in r.get_data(as_text=True)
        assert CANARY not in "\n".join(m.getMessage() for m in caplog.records)
    finally:
        svc.engine.dispose()


def test_a_relay_call_writes_no_row_and_logs_no_body(monkeypatch, caplog):
    hc = _FakeClient(result=('{"resourceType": "Bundle", "entry": [{"resource":'
                             f' {{"resourceType": "Patient", "id": "{CANARY}"}}'
                             '}]}').encode())
    app, svc = _careagents(hc)
    try:
        acct = _account(svc, monkeypatch)
        svc.add_connection(acct.id, "direct", "ca-relay-one", "Upload")
        browser = app.test_client()
        _sign_in(browser, acct)
        before = _counts(svc)
        caplog.set_level(logging.DEBUG)
        for _ in range(2):
            assert CANARY in _post(browser).get_data(as_text=True)
        assert _counts(svc) == before
        assert CANARY not in "\n".join(m.getMessage() for m in caplog.records)
    finally:
        svc.engine.dispose()


# --- the client, on the real engine -------------------------------------------

class _Engine:
    """A requests-shaped session over the engine's own WSGI app, recording
    what CareAgents sent it."""

    def __init__(self, client):
        self.client, self.sent = client, []

    @staticmethod
    def _path(url):
        return url.replace("http://engine", "", 1)

    def post(self, url, json=None, headers=None, timeout=None, data=None):
        self.sent.append(("POST", self._path(url), json, dict(headers or {})))
        return _to_requests(self.client.post(self._path(url), json=json,
                                             data=data, headers=headers or {}))

    def get(self, url, params=None, headers=None, timeout=None):
        self.sent.append(("GET", self._path(url), params, dict(headers or {})))
        return _to_requests(self.client.get(self._path(url),
                                            headers=headers or {},
                                            query_string=params or {}))


TYPES = ["Patient", "Condition", "Coverage"]


@pytest.fixture
def chain(app, client, monkeypatch):
    """The real client on the real engine, behind a real CareAgents."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f.MINT_SECRET)
    # The sibling's Condition shares the tenant but names another patient.
    f.seed(app, f.patient(), f.condition(), f.coverage(), f.organization(),
           f.condition("ci-cond-sib", pid=f.SIBLING))
    engine = _Engine(client)
    hc = HealthClawClient(base="http://engine", mint_secret=f.MINT_SECRET)
    hc.http = engine
    ca, svc = _careagents(hc)
    acct = _account(svc, monkeypatch)
    svc.add_connection(acct.id, "direct", f.TENANT, "Upload")
    browser = ca.test_client()
    _sign_in(browser, acct)
    yield browser, engine, svc
    svc.engine.dispose()


def _audits(app):
    from r6.models import AuditEventRecord
    with app.app_context():
        return [(a.resource_type, a.resource_id, a.agent_id, a.detail or "")
                for a in AuditEventRecord.query.filter_by(
                    tenant_id=f.TENANT).all()]


def test_one_relay_call_is_one_export_on_the_real_engine(app, client, chain,
                                                        caplog):
    browser, engine, svc = chain
    before = _counts(svc)
    caplog.set_level(logging.DEBUG)
    r = browser.post(RELAY, json={"resource_types": TYPES})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.mimetype == "application/fhir+json"
    assert r.headers["Cache-Control"] == "no-store"
    bundle = r.get_json()
    got = sorted((e["resource"]["resourceType"], e["resource"]["id"])
                 for e in bundle["entry"])
    # The tenant's patient only (the sibling's Condition stays home), with
    # the payor the Coverage references.
    assert got == [("Condition", "ci-cond-1"), ("Coverage", "ci-cov-1"),
                   ("Organization", "ci-org-1"), ("Patient", f.PT)]

    # Exactly one share-bundle AuditEvent, from fixed words. The relay's
    # Patient search (to find whose records these are) is audited too, as
    # every read is; it is a separate row on Patient, not a second export.
    rows = _audits(app)
    exports = [x for x in rows if x[1] == "share-bundle"]
    assert exports == [("Bundle", "share-bundle", "careagents-checkin",
                        "share-bundle export (profile=checkin): 4 resources "
                        "across 4 type(s); types=Condition,Coverage,Patient")]
    assert all(x[0] == "Patient" for x in rows if x[1] != "share-bundle")
    for value in (f.CANARY_DISPLAY, f.CANARY_NOTE, f.CANARY_SSN, f.MEMBER_ID,
                  "Testperson", "clinic", "origin"):
        assert all(value not in x[3] for x in rows), value

    # What CareAgents sent: the usual read token for the Patient search, a
    # mint for exactly the types, then the export for the tenant's one
    # patient. No origin, no request, no purpose.
    general, mint, export = [s for s in engine.sent if s[0] == "POST"]
    assert general[2] == {"tenant_id": f.TENANT}
    assert mint[1] == "/r6/fhir/internal/step-up-token"
    assert mint[2] == {"tenant_id": f.TENANT, "purpose": "checkin",
                       "resource_types": sorted(TYPES)}
    assert export[1] == "/r6/fhir/$share-bundle"
    assert export[2] == {"profile": "checkin", "resource_types": sorted(TYPES),
                         "patient_id": f.PT}

    # The tokens stay between CareAgents and the engine.
    tokens = {s[3]["X-Step-Up-Token"] for s in engine.sent
              if "X-Step-Up-Token" in s[3]}
    assert len(tokens) == 2      # the general read token and the check-in one
    shown = r.get_data(as_text=True) + repr(r.headers.to_wsgi_list())
    for tok in tokens:
        assert tok not in shown
        assert tok.rsplit(".", 1)[-1] not in shown

    # Nothing kept, nothing logged.
    assert _counts(svc) == before
    logged = "\n".join(m.getMessage() for m in caplog.records)
    for value in ("Testperson", f.MEMBER_ID, f.PAYOR_NAME, *tokens):
        assert value not in logged


def test_each_relay_call_mints_its_own_single_use_token(app, client, chain):
    browser, engine, _ = chain
    for _ in range(2):
        assert browser.post(RELAY, json={"resource_types": TYPES}
                            ).status_code == 200
    spent = [s[3]["X-Step-Up-Token"] for s in engine.sent
             if s[1] == "/r6/fhir/$share-bundle"]
    assert len(spent) == 2 and spent[0] != spent[1]
    assert len([x for x in _audits(app) if x[1] == "share-bundle"]) == 2
    # A token already spent is refused if anyone replays it.
    again = f.export(client, spent[0], TYPES)
    assert again.status_code == 401


def test_a_tenant_with_two_patients_and_no_named_one_exports_nothing(
        app, client, monkeypatch):
    """The patient is the tenant's one Patient, found on the server; with two
    there is no telling whose check-in this is, and nothing is minted."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f.MINT_SECRET)
    f.seed(app, f.patient(), f.patient(f.SIBLING, given="Bo"))
    engine = _Engine(client)
    hc = HealthClawClient(base="http://engine", mint_secret=f.MINT_SECRET)
    hc.http = engine
    with pytest.raises(HealthClawError) as caught:
        hc.checkin_export(f.TENANT, ["Patient"])
    assert caught.value.code == "no_patient"
    assert not [s for s in engine.sent if s[1] == "/r6/fhir/$share-bundle"]
    assert not [s for s in engine.sent if s[2] and s[2].get("purpose")]


# --- the wallet's tables equal the engine's -------------------------------------

def test_the_relay_types_are_the_engines():
    from careagents.checkin_wallet import CHECKIN_TYPES
    from r6.checkin import CHECKIN_TYPES as ENGINE
    assert CHECKIN_TYPES == ENGINE


def test_the_wallet_types_and_profiles_are_the_engines():
    from r6.checkin import CHECKIN_TYPES, CLAIMS_BY_TYPE
    src = (STATIC / "checkin_answer.js").read_text()
    block = re.search(r"export const CHECKIN_TYPES = \[(.*?)\];", src,
                      re.S).group(1)
    assert set(re.findall(r'"(\w+)"', block)) == set(CHECKIN_TYPES)
    block = re.search(r"export const PROFILE_TYPES = \{(.*?)\};", src,
                      re.S).group(1)
    rows = re.findall(r'"(http[^"]+)": "(\w+)"', block)
    assert {(url, t) for url, t in rows} == {
        (url, t) for t, urls in CLAIMS_BY_TYPE.items() for url in urls}


# --- what the wallet names a record by, on the real engine ---------------------

E2E_EXPORT = (Path(__file__).resolve().parent.parent / "e2e" / "fixtures"
              / "checkin-export-test-patient.json")
TEST_PATIENT = (Path(__file__).resolve().parent.parent / "careagents"
                / "checkin" / "us_core_test_patient.json")


def _normalised(bundle):
    """The export without what changes run to run."""
    bundle = dict(bundle)
    bundle.pop("timestamp", None)
    for e in bundle["entry"]:
        meta = e["resource"].get("meta")
        if isinstance(meta, dict):
            meta.pop("lastUpdated", None)
            meta.pop("versionId", None)
    return bundle


def test_the_e2e_relay_answer_is_what_the_engine_exports(app, client,
                                                         monkeypatch):
    """The browser specs answer the relay with e2e/fixtures/
    checkin-export-test-patient.json, so it must be the engine's own checkin
    export of the test patient: upstream words dropped, our labels written.
    CHECKIN_EXPORT_WRITE=1 regenerates it."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f.MINT_SECRET)
    bundle = json.loads(TEST_PATIENT.read_text())
    f.seed(app, *[e["resource"] for e in bundle["entry"]])
    types = sorted({"Patient", "Condition", "AllergyIntolerance",
                    "MedicationRequest", "Immunization", "Coverage"})
    r = f.export(client, f.token(client, types), types,
                 patient_id="checkin-test-patient")
    assert r.status_code == 200
    got = _normalised(r.get_json())
    text = json.dumps(got, indent=1, sort_keys=True) + "\n"
    if os.environ.get("CHECKIN_EXPORT_WRITE") == "1":
        E2E_EXPORT.write_text(text)
    assert E2E_EXPORT.read_text() == text


_NAMES = """
const m = await import(process.argv[1]);
const bundle = JSON.parse(process.argv[2]);
const request = { type: "smart-health-checkin-request", version: "1", id: "x",
  items: JSON.parse(process.argv[3]).map((t, i) => ({ id: `i${i}`, title: t,
    accept: ["application/fhir+json"],
    content: { kind: "selection.fhir", resourceTypes: [t] } })) };
const matches = m.matchItems(request, [], m.prepareRecords(bundle));
const share = Object.fromEntries(request.items.map((i) => [i.id, true]));
console.log(JSON.stringify({
  shown: matches.flatMap((x) => [m.itemHeading(x), ...m.recordNames(x)]),
  sent: m.buildResponse(request, matches, share),
}));
"""


def _wallet_view(bundle, types):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "--input-type=module", "-e", _NAMES,
         str(STATIC / "checkin_answer.js"), json.dumps(bundle),
         json.dumps(types)],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_the_wallet_names_records_by_the_engines_label_never_upstream(
        app, client, chain):
    """#973 patient test, finding 1: real records read "A health problem".
    The wallet names a record by the engine-written `code.text`, which is
    r6/terminology.py's label for the code, the same words the website
    gets. An upstream name in `code.text` or `coding.display` (the canary)
    reaches neither what the person is shown nor what is sent."""
    browser, _, _ = chain
    r = browser.post(RELAY, json={"resource_types": ["Condition"]})
    assert r.status_code == 200
    bundle = r.get_json()
    assert f.CANARY_DISPLAY not in r.get_data(as_text=True)
    view = _wallet_view(bundle, ["Condition"])
    shown = json.dumps(view["shown"])
    assert "Type 2 diabetes mellitus" in view["shown"], view["shown"]
    assert "Your health problems" in view["shown"]
    assert f.CANARY_DISPLAY not in shown
    assert f.CANARY_DISPLAY not in json.dumps(view["sent"])
