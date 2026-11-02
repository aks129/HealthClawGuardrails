"""Security review of PR #1086, round 2: the made-up label comes from the
relay's `X-Checkin-Records: made-up` header (checkin_wallet.py
answered_from_test_patient), never from record content.

Attacked here, on the real engine through the real HealthClawClient:

- The header for real records: a test patient beside a real connection
  (real wins, no header), HTEST-stamped real records, a client sending
  the header or a `made_up` key, another account's test-patient tenant.
- The header vanishing for made-up records: it is there on the plain
  test-patient answer.
- A revoked test patient, a test patient held for someone cared for, and
  a pick of the test patient's connection id: no answer, so no header.
- Page code: the label is read only from the header; the sample-clinic
  message is gated on the browser-reported origin equal to the wallet's
  own, and the library drops a `null` origin and any non-opener source.

Synthetic data only.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from careagents import checkin_wallet as cw
from careagents.healthclaw import HealthClawClient
from tests import checkin_fixtures as f
from tests.test_checkin_relay import RELAY, _careagents, _Engine
from tests.test_checkin_wallet import _account, _sign_in

pytestmark = pytest.mark.extension

STATIC = Path(__file__).resolve().parent.parent / "careagents" / "static"
HTEST = {"system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
         "code": "HTEST"}
SIX = ["AllergyIntolerance", "Condition", "Coverage", "Immunization",
       "MedicationRequest", "Patient"]
HEADER = "X-Checkin-Records"


def _stamped(resource):
    res = copy.deepcopy(resource)
    res.setdefault("meta", {})["tag"] = [dict(HTEST)]
    return res


@pytest.fixture
def hc(app, client, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f.MINT_SECRET)
    h = HealthClawClient(base="http://engine", mint_secret=f.MINT_SECRET)
    h.http = _Engine(client)
    return h


def _browser(hc, monkeypatch, email="r2-wallet@example.test"):
    ca, svc = _careagents(hc, CARE_CHECKIN_TEST_PATIENT="1")
    acct = _account(svc, monkeypatch, email=email)
    b = ca.test_client()
    _sign_in(b, acct)
    return b, svc, acct


def _test_patient(b, svc, acct):
    assert b.post("/checkin/wallet/test-patient", json={}).status_code == 200
    return next(c for c in svc.list_home(acct.id)["connections"]
                if cw._is_test_patient(c))


def _real_upload(hc):
    out = hc.ingest_bundle(f.TENANT, {
        "resourceType": "Bundle", "type": "collection",
        "entry": [{"resource": _stamped(r)} for r in (
            f.patient(), f.condition(), f.coverage(), f.organization())]})
    assert out.get("ingested") == 4, out


def test_the_plain_test_patient_answer_carries_the_header(hc, monkeypatch):
    b, svc, acct = _browser(hc, monkeypatch)
    _test_patient(b, svc, acct)
    r = b.post(RELAY, json={"resource_types": SIX})
    assert r.status_code == 200
    assert r.headers.get(HEADER) == "made-up"
    assert "no-store" in r.headers["Cache-Control"]


def test_a_test_patient_beside_real_records_never_labels_them(hc, monkeypatch):
    b, svc, acct = _browser(hc, monkeypatch)
    _test_patient(b, svc, acct)
    # Real records added after the test patient; HTEST-stamped, as a
    # hostile feed sends them.
    _real_upload(hc)
    svc.add_connection(acct.id, "direct", f.TENANT, "Upload")
    r = b.post(RELAY, json={"resource_types": SIX})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert any(e["resource"]["resourceType"] == "Patient"
               and e["resource"]["id"] == f.PT for e in r.get_json()["entry"])
    assert HEADER not in r.headers


def test_a_client_cannot_ask_for_the_label(hc, monkeypatch):
    b, svc, acct = _browser(hc, monkeypatch)
    _real_upload(hc)
    svc.add_connection(acct.id, "direct", f.TENANT, "Upload")
    r = b.post(RELAY, json={"resource_types": SIX},
               headers={HEADER: "made-up"})
    assert r.status_code == 200
    assert HEADER not in r.headers
    r = b.post(RELAY, json={"resource_types": SIX, "made_up": True})
    assert r.status_code == 400
    assert HEADER not in r.headers


def test_picking_the_test_patients_connection_is_refused(hc, monkeypatch):
    b, svc, acct = _browser(hc, monkeypatch)
    tp = _test_patient(b, svc, acct)
    r = b.post(RELAY, json={"resource_types": SIX, "connection_id": tp["id"]})
    assert r.status_code == 409
    assert HEADER not in r.headers


def test_a_revoked_test_patient_answers_nothing_and_says_nothing(
        hc, monkeypatch):
    b, svc, acct = _browser(hc, monkeypatch)
    tp = _test_patient(b, svc, acct)
    svc.set_connection_status(tp["tenant_id"], "revoked")
    assert not cw.answered_from_test_patient(svc, acct.id, tp["tenant_id"])
    r = b.post(RELAY, json={"resource_types": SIX})
    assert r.status_code != 200
    assert HEADER not in r.headers


def test_another_accounts_test_patient_tenant_is_not_this_accounts(
        hc, monkeypatch):
    a, svc, alice = _browser(hc, monkeypatch, email="r2-alice@example.test")
    tp = _test_patient(a, svc, alice)
    bob = _account(svc, monkeypatch, email="r2-bob@example.test")
    assert cw.answered_from_test_patient(svc, alice.id, tp["tenant_id"])
    assert not cw.answered_from_test_patient(svc, bob.id, tp["tenant_id"])
    # Bob holding a real connection on that same tenant id still gets no
    # label: the test-patient connection is Alice's.
    svc.add_connection(bob.id, "direct", tp["tenant_id"], "Upload")
    assert not cw.answered_from_test_patient(svc, bob.id, tp["tenant_id"])


def test_a_test_patient_held_for_someone_else_is_not_the_label(
        hc, monkeypatch):
    b, svc, acct = _browser(hc, monkeypatch)
    from careagents.models import Person
    with svc.session() as s:
        p = Person(account_id=acct.id, kind="other", nickname="R2")
        s.add(p)
        s.flush()
        pid = p.id
    svc.add_connection(acct.id, "sample", "r2-other-tenant", "Test",
                       provider=cw.TEST_PROVIDER, person_id=pid)
    assert not cw.answered_from_test_patient(svc, acct.id, "r2-other-tenant")


# --- page code ---------------------------------------------------------------

def _code(path):
    src = path.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def test_the_label_is_read_only_from_the_header():
    wallet = _code(STATIC / "checkin_wallet.js")
    answer = _code(STATIC / "checkin_answer.js")
    sample = _code(STATIC / "checkin_sample.js")
    for src in (wallet, answer, sample):
        assert "HTEST" not in src
        assert "v3-ActReason" not in src
    # The read lives in checkin_answer.js relaySaysMadeUp, so node can run
    # it (#1086 review); the wallet calls it once, on the relay's headers.
    assert answer.count('headers.get("X-Checkin-Records") === "made-up"') == 1
    assert wallet.count("made: relaySaysMadeUp(res.headers)") == 1
    # The message goes only to a requester on the wallet's own origin.
    assert ("if (made && origin === location.origin) {" in wallet)
    assert wallet.count("MADE_UP_MESSAGE") == 2  # the import and the one use


def test_the_library_drops_a_null_origin_and_a_non_opener():
    lib = (STATIC / "vendor" / "smart-health-checkin-client" / "0.4.4" /
           "wallet.js").read_text()
    assert 'if(r.source!==window.opener||r.origin==="null"||t)return;' in lib
    # The origin handed to onRequest is the MessageEvent's, not the
    # request's.
    assert "let{source:o,origin:c}=r" in lib
    assert "onRequest({request:d.smartRequest,unsupportedItems:" \
           "d.unsupportedItems,origin:c" in lib
