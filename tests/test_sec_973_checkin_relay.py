"""Security exploit tests for the check-in records relay (PR #973).

What the PR's own tests do not reach:

- The REAL HealthClawClient against an engine that answers with hostile
  bodies (a canary in an HTML 500, in an OperationOutcome, in a 200 that is
  not a Bundle, in a refused mint, in a transport error). The relay must
  answer a fixed code, `no-store`, and no canary or token anywhere: not in the
  response, not in the log.
- A forced audit write failure on the real engine, through the relay: the
  look must fail closed and no record may reach the browser (V2).
- Pinned engine gaps the relay inherits (strict xfail, so each one turns red
  when it is fixed and the pin must be removed):
  - a record's own HTEST tag is dropped by the checkin export;
  - every `fullUrl` names the host CareAgents called, which in production is
    the private Railway hostname, and it reaches the clinic.
- The wallet's allergy allow-list run in node against absence concepts the
  engine already knows about (r6/terminology.py ABSENCE_CONCEPTS).

Synthetic data only (tests/checkin_fixtures.py).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
import requests

from careagents.healthclaw import HealthClawClient
from tests import checkin_fixtures as f
from tests.test_beta_acceptance_rows import _to_requests
from tests.test_checkin_relay import RELAY, TYPES, _careagents, _Engine
from tests.test_checkin_wallet import _account, _sign_in

CANARY = "CANARY-sec973-Rivera-Marisol"
TOKEN = "sec973tok.CANARYSIGNATURE"
STATIC = Path(__file__).resolve().parent.parent / "careagents" / "static"


class _Resp:
    """A requests.Response-shaped answer with a chosen status and body."""

    def __init__(self, status, body, ctype="application/json"):
        self.status_code = status
        self.ok = 200 <= status < 400
        self.content = body.encode() if isinstance(body, str) else body
        self.text = self.content.decode()
        self.headers = {"Content-Type": ctype}

    def json(self):
        return json.loads(self.text)


class _Hostile:
    """An engine that serves a good Patient search and a good mint, then
    answers `stage` with `answer` (a _Resp, or an exception to raise)."""

    def __init__(self, stage, answer):
        self.stage, self.answer = stage, answer

    def _give(self, stage, good):
        if stage != self.stage:
            return good
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def get(self, url, params=None, headers=None, timeout=None):
        return self._give("search", _Resp(200, json.dumps({
            "resourceType": "Bundle", "entry": [{"resource": {
                "resourceType": "Patient", "id": f.PT}}]})))

    def post(self, url, json=None, headers=None, timeout=None, data=None):
        if url.endswith("/internal/step-up-token"):
            stage = "mint" if (json or {}).get("purpose") == "checkin" \
                else "general-mint"
            return self._give(stage, _Resp(200, '{"token": "%s"}' % TOKEN))
        return self._give("export", _Resp(200, '{"resourceType": "Bundle", '
                                                '"entry": []}'))


HOSTILE = [
    ("export", _Resp(500, f"<html><body>Traceback {CANARY} {TOKEN}</body>"
                          "</html>", "text/html"), 502, "unavailable"),
    ("export", _Resp(400, json.dumps({
        "resourceType": "OperationOutcome", "issue": [{
            "severity": "error", "code": "required",
            "diagnostics": f"{CANARY} {TOKEN}"}]})), 502, "unavailable"),
    ("export", _Resp(200, json.dumps({
        "resourceType": "OperationOutcome", "issue": [{
            "diagnostics": CANARY}]})), 502, "unavailable"),
    ("export", _Resp(200, f"<html>{CANARY}</html>", "text/html"),
     502, "unavailable"),
    ("export", _Resp(413, json.dumps({"error": "checkin_too_large",
                                      "detail": CANARY})), 413, "too_large"),
    ("export", requests.ConnectionError(f"https://engine/?t={TOKEN} {CANARY}"),
     502, "unavailable"),
    ("export", requests.ReadTimeout(f"{CANARY} {TOKEN}"), 502, "unavailable"),
    ("mint", _Resp(403, json.dumps({"error": f"{CANARY}"})), 502,
     "unavailable"),
    ("mint", _Resp(200, json.dumps({"token": None, "x": CANARY})), 502,
     "unavailable"),
    ("search", _Resp(500, f"{CANARY}", "text/plain"), 502, "unavailable"),
    ("search", requests.ConnectionError(CANARY), 502, "unavailable"),
    ("general-mint", _Resp(401, CANARY, "text/plain"), 502, "unavailable"),
]


@pytest.mark.parametrize("stage,answer,status,code", HOSTILE,
                         ids=[f"{h[0]}-{i}" for i, h in enumerate(HOSTILE)])
def test_a_hostile_engine_answer_never_reaches_the_browser_or_the_log(
        monkeypatch, caplog, stage, answer, status, code):
    hc = HealthClawClient(base="http://engine", mint_secret="m")
    hc.http = _Hostile(stage, answer)
    app, svc = _careagents(hc)
    try:
        acct = _account(svc, monkeypatch)
        svc.add_connection(acct.id, "direct", f.TENANT, "Upload")
        browser = app.test_client()
        _sign_in(browser, acct)
        caplog.set_level(logging.DEBUG)
        r = browser.post(RELAY, json={"resource_types": ["Patient"]})
        shown = r.get_data(as_text=True) + repr(r.headers.to_wsgi_list())
        logged = "\n".join(m.getMessage() for m in caplog.records) + "\n" + \
            "\n".join(repr(m.exc_info) for m in caplog.records if m.exc_info)
        assert (r.status_code, r.get_json()) == (status, {"error": code})
        assert r.headers["Cache-Control"] == "no-store"
        for secret in (CANARY, TOKEN, "CANARYSIGNATURE"):
            assert secret not in shown
            assert secret not in logged
    finally:
        svc.engine.dispose()


# --- V2: a forced audit failure on the real engine fails the look -------------

@pytest.fixture
def real_chain(app, client, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f.MINT_SECRET)
    pt = f.patient()
    pt["meta"]["tag"] = [{"system": "http://terminology.hl7.org/CodeSystem/"
                                    "v3-ActReason", "code": "HTEST"}]
    f.seed(app, pt, f.condition(), f.coverage(), f.organization())
    engine = _Engine(client)
    hc = HealthClawClient(base="http://engine", mint_secret=f.MINT_SECRET)
    hc.http = engine
    ca, svc = _careagents(hc)
    acct = _account(svc, monkeypatch)
    svc.add_connection(acct.id, "direct", f.TENANT, "Upload")
    browser = ca.test_client()
    _sign_in(browser, acct)
    yield browser, engine
    svc.engine.dispose()


def test_an_audit_write_failure_on_the_export_fails_closed_through_the_relay(
        app, real_chain, monkeypatch):
    from r6 import checkin
    from r6.audit import AuditWriteError

    browser, engine = real_chain
    # As in production: the engine answers the exception, it does not raise
    # it into the caller.
    monkeypatch.setitem(app.config, "PROPAGATE_EXCEPTIONS", False)

    def broken(event_type, *a, **k):
        raise AuditWriteError("forced by sec973")

    monkeypatch.setattr(checkin, "add_audit_event", broken)
    r = browser.post(RELAY, json={"resource_types": TYPES})
    body = r.get_data(as_text=True)
    assert r.status_code == 502, body
    assert r.get_json() == {"error": "unavailable"}
    for value in ("Testperson", f.MEMBER_ID, f.PAYOR_NAME, f.PT, "Bundle"):
        assert value not in body


def test_a_token_minted_for_one_tenant_is_refused_on_another(app, client,
                                                            monkeypatch):
    """Tenant swap on the engine, with a live check-in token."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f.MINT_SECRET)
    f.seed(app, f.patient())
    f.seed(app, f.patient(), tenant=f.OTHER_TENANT)
    tok = f.token(client, ["Patient"])
    r = f.export(client, tok, ["Patient"], tenant=f.OTHER_TENANT)
    assert r.status_code == 401
    assert "Testperson" not in r.get_data(as_text=True)


# --- engine gaps the relay inherits (pinned; flip when fixed) -----------------

def test_a_records_own_htest_tag_survives_the_relay(real_chain):
    browser, _ = real_chain
    r = browser.post(RELAY, json={"resource_types": ["Patient"]})
    assert r.status_code == 200
    patient = next(e["resource"] for e in r.get_json()["entry"]
                   if e["resource"]["resourceType"] == "Patient")
    tags = (patient.get("meta") or {}).get("tag") or []
    assert any(t.get("code") == "HTEST" for t in tags)


class _HostEngine(_Engine):
    """Like _Engine, but the engine sees the Host CareAgents called, the way
    a real server-to-server request arrives."""

    HOST = "healthclaw-engine.railway.internal"

    def post(self, url, json=None, headers=None, timeout=None, data=None):
        self.sent.append(("POST", self._path(url), json, dict(headers or {})))
        return _to_requests(self.client.post(
            self._path(url), json=json, data=data, headers=headers or {},
            base_url=f"http://{self.HOST}"))

    def get(self, url, params=None, headers=None, timeout=None):
        return _to_requests(self.client.get(
            self._path(url), headers=headers or {},
            query_string=params or {}, base_url=f"http://{self.HOST}"))


@pytest.mark.xfail(strict=True, reason="#973 flag 5: r6/checkin.py full_url "
                   "builds every fullUrl from request.host_url, so the "
                   "private hostname CareAgents calls reaches the clinic.")
def test_no_full_url_names_the_internal_host_careagents_called(
        app, client, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f.MINT_SECRET)
    f.seed(app, f.patient(), f.condition())
    hc = HealthClawClient(base="http://engine", mint_secret=f.MINT_SECRET)
    hc.http = _HostEngine(client)
    ca, svc = _careagents(hc)
    try:
        acct = _account(svc, monkeypatch)
        svc.add_connection(acct.id, "direct", f.TENANT, "Upload")
        browser = ca.test_client()
        _sign_in(browser, acct)
        r = browser.post(RELAY, json={"resource_types": ["Patient",
                                                         "Condition"]})
        assert r.status_code == 200, r.get_data(as_text=True)
        urls = [e["fullUrl"] for e in r.get_json()["entry"]]
        assert urls
        assert not [u for u in urls if _HostEngine.HOST in u], urls
    finally:
        svc.engine.dispose()


# --- the wallet's allergy allow-list against the engine's absence set ----------

_PROBE = """
const m = await import(process.argv[1]);
const r = (code) => ({ resourceType: "AllergyIntolerance",
  code: { coding: [{ system: "http://snomed.info/sct", code }] } });
console.log(JSON.stringify(Object.fromEntries(
  JSON.parse(process.argv[2]).map((c) => [c, m.shareableAllergy(r(c))]))));
"""


def _shareable(codes):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "--input-type=module", "-e", _PROBE,
         str(STATIC / "checkin_answer.js"), json.dumps(codes)],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_no_snomed_nka_concept_is_shared_as_an_allergy():
    from r6.terminology import NKA_CONCEPTS, SNOMED
    codes = sorted(c for s, c in NKA_CONCEPTS if s == SNOMED)
    assert _shareable(codes) == {c: False for c in codes}


def test_no_engine_absence_concept_is_shared_as_an_allergy():
    """#973 flag 4, fixed: SNOMED 1631000175102 'Patient not asked' was
    sent as a fulfilled allergy."""
    from r6.terminology import ABSENCE_CONCEPTS, SNOMED
    codes = sorted(c for s, c in ABSENCE_CONCEPTS if s == SNOMED)
    assert _shareable(codes) == {c: False for c in codes}


def test_a_padded_nka_code_is_not_shared_as_an_allergy():
    """#973 flag 4, fixed: a padded ' 716186003' was an NKA sent as an
    allergy; codes are trimmed before the absence check."""
    assert _shareable(["716186003 ", " 409137002"]) == {
        "716186003 ": False, " 409137002": False}


# --- recheck at 1b828dc: the wallet now names records by `code.text` ----------

def _upstream_named(rtype, field, concept, rid):
    res = {"resourceType": rtype, "id": rid,
           "meta": f.claim({
               "Condition": "us-core-condition-problems-health-concerns",
               "Immunization": "us-core-immunization",
               "MedicationRequest": "us-core-medicationrequest",
               "AllergyIntolerance": "us-core-allergyintolerance"}[rtype]),
           field: concept}
    res["patient" if rtype in ("Immunization", "AllergyIntolerance")
        else "subject"] = {"reference": f"Patient/{f.PT}"}
    return res


#: Concepts the engine has no label for, carrying upstream words in `text`
#: and `display`: the relabel cannot overwrite them, so only the strip
#: stands between the feed and what the wallet shows.
_UNLABELLED = {
    "unknown code": lambda: {"coding": [{"system": "http://snomed.info/sct",
                                         "code": "999999999",
                                         "display": CANARY}], "text": CANARY},
    "text only": lambda: {"text": CANARY},
    "coding not a list": lambda: {"coding": {"system": "x", "code": "1"},
                                  "text": CANARY},
    "empty coding": lambda: {"coding": [], "text": CANARY},
}
_FIELDS = [("Condition", "code"), ("Immunization", "vaccineCode"),
           ("MedicationRequest", "medicationCodeableConcept"),
           # A substance code so the allow-list lets it through to be named.
           ("AllergyIntolerance", "code")]


@pytest.mark.parametrize("shape", sorted(_UNLABELLED))
def test_upstream_text_on_an_unlabelled_concept_never_names_a_record(
        app, client, monkeypatch, shape):
    from tests.test_checkin_relay import _wallet_view
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f.MINT_SECRET)
    rows = [f.patient()]
    for i, (rtype, field) in enumerate(_FIELDS):
        concept = _UNLABELLED[shape]()
        if rtype == "AllergyIntolerance" and isinstance(
                concept.get("coding"), list):
            concept["coding"].append({
                "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                "code": "999999"})
        rows.append(_upstream_named(rtype, field, concept, f"sec973-{i}"))
    f.seed(app, *rows)
    hc = HealthClawClient(base="http://engine", mint_secret=f.MINT_SECRET)
    hc.http = _Engine(client)
    ca, svc = _careagents(hc)
    try:
        acct = _account(svc, monkeypatch)
        svc.add_connection(acct.id, "direct", f.TENANT, "Upload")
        browser = ca.test_client()
        _sign_in(browser, acct)
        types = sorted({t for t, _ in _FIELDS})
        r = browser.post(RELAY, json={"resource_types": types})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert CANARY not in r.get_data(as_text=True)
        got = {e["resource"]["id"] for e in r.get_json()["entry"]}
        # Every planted record is exported (else this proves nothing), the
        # allergy only when its coding is a list a substance code can join.
        assert {"sec973-0", "sec973-1", "sec973-2"} <= got, got
        view = _wallet_view(r.get_json(), types)
        assert CANARY not in json.dumps(view)
        assert {"A health problem", "A vaccine", "A medicine"} <= set(
            view["shown"]), view["shown"]
    finally:
        svc.engine.dispose()
