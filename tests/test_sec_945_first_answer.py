"""Security exploits for PR #945 (the first answer is never a false "no
records"). V1, V2, V6 and the NKA non-negotiable, on the real engine.

Every free-text field and every engine error body carries a canary. A
stub model records each request it is handed; the canary must not appear
in any model request, the SSE stream, the run events, the logs or audit
detail. Synthetic data only.
"""

from __future__ import annotations

import json
import logging

import pytest
import requests

from careagents.agent import _execute_tool, _is_allergy_status, _summarize_bundle
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, _turn, cfg, svc)
from tests.test_careagents_imessage_demo import _Call, _Turn

CANARY = "QZXCANARY"
SNOMED = "http://snomed.info/sct"
NULLFLAVOR = "http://terminology.hl7.org/CodeSystem/v3-NullFlavor"
DAR = "http://terminology.hl7.org/CodeSystem/data-absent-reason"
PATIENT = {"reference": "Patient/demo-patient-rivera",
           "display": f"{CANARY} Patient Zelda Quornwhistle"}


def _free_text(res: dict, tag: str) -> dict:
    """Plant a canary in every free-text field a FHIR resource can carry."""
    res = dict(res)
    res["patient"] = PATIENT
    res["subject"] = PATIENT
    res["text"] = {"status": "generated",
                   "div": f"<div>{CANARY}-{tag}-narrative</div>"}
    res["note"] = [{"text": f"{CANARY}-{tag}-note call 555-0100"}]
    res["recorder"] = {"reference": "Practitioner/x",
                       "display": f"{CANARY}-{tag}-recorder"}
    code = dict(res.get("code") or {})
    code["text"] = f"{CANARY}-{tag}-code-text"
    code["coding"] = [dict(c, display=f"{CANARY}-{tag}-display")
                      for c in code.get("coding") or []]
    res["code"] = code
    if res["resourceType"] == "AllergyIntolerance":
        res["reaction"] = [{"description": f"{CANARY}-{tag}-reaction",
                            "manifestation": [{"text": f"{CANARY}-{tag}-man"}]}]
    return res


def _allergy(rid, *codings):
    return {"resourceType": "AllergyIntolerance", "id": rid,
            "code": {"coding": [{"system": s, "code": c} for s, c in codings]}}


def _plant(chain, *resources):
    from models import db
    from r6.models import R6Resource
    with chain.engine_app.app_context():
        for res in resources:
            db.session.add(R6Resource(res["resourceType"], json.dumps(res),
                                      resource_id=res["id"], tenant_id=TENANT))
        db.session.commit()


def _turn_capturing(chain, monkeypatch, tool="get_health_summary"):
    """One worker turn whose stub model calls `tool`, then echoes the tool
    result back as its answer (the worst case: the model quotes it). Returns
    (every model request serialised, the SSE body, the run events)."""
    requests_seen: list[str] = []

    def complete(_cfg, system, history, tools):
        requests_seen.append(json.dumps({"system": system,
                                         "history": history}, default=str))
        results = [m["content"] for m in history if m.get("role") == "tool"]
        if not results:
            return _Turn("", [_Call(0, tool, {})])
        return _Turn("answer: " + results[-1])
    monkeypatch.setattr("careagents.worker.llm.complete", complete)

    c = chain.app.test_client()
    _login(c, chain.svc, monkeypatch, email="acceptance@example.com")
    from careagents.worker import RunWorker
    RunWorker(chain.app.extensions["careagents_runtime"]["config"],
              chain.hc, chain.svc, "sec945-worker").run_once()
    r = _turn(c, chain.agent, "show me summary")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    page = c.get("/").get_data(as_text=True)
    from r6.agent_runs.models import AgentRun
    with chain.engine_app.app_context():
        run_ids = [x.id for x in AgentRun.query.filter_by(tenant_id=TENANT)]
    events = json.dumps([chain.hc.agent_run_events(TENANT, rid, 0, 500)
                         for rid in run_ids], default=str)
    assert requests_seen, "the model was never called"
    return "\n".join(requests_seen), body + page, events


def _audit_details(chain):
    from r6.models import AuditEventRecord
    with chain.engine_app.app_context():
        return json.dumps([(a.detail, a.resource_type)
                           for a in AuditEventRecord.query.all()], default=str)


def _no_canary(*blobs):
    for blob in blobs:
        assert CANARY not in blob
        assert "Quornwhistle" not in blob
        assert "555-0100" not in blob


# --- (1) V1: canaries in records never reach model / SSE / page / logs -------

def test_status_and_real_allergies_with_canaries_leak_nothing(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    monkeypatch.setattr(
        "r6.terminology_resolver.resolve",
        lambda s, c: "Patient not asked" if c == "1631000175102" else None)
    _plant(chain,
           _free_text(_allergy("s945-a1", (SNOMED, "1631000175102")), "a1"),
           _free_text(_allergy("s945-a2", (NULLFLAVOR, "UNK")), "a2"),
           _free_text(_allergy("s945-a3", (DAR, "asked-declined")), "a3"),
           _free_text(_allergy("s945-a4", (SNOMED, "91936005")), "a4"),
           _free_text(_allergy("s945-a5", ("urn:x-unknown", "ZZ1")), "a5"),
           _free_text({"resourceType": "Condition", "id": "s945-c1",
                       "code": {"coding": [{"system": SNOMED,
                                            "code": "44054006"}]}}, "c1"))
    model, sse, events = _turn_capturing(chain, monkeypatch)
    _no_canary(model, sse, events, caplog.text, _audit_details(chain))
    # a4 is a recorded penicillin allergy, so the status records must not
    # say the records are silent on allergies (F2). They are not sent at
    # all: a "do not mention" note was mentioned and disclosed (#949 QA).
    assert "Your records don't say whether you have allergies." not in model
    assert "allergy information was not collected" not in model
    assert "do not mention" not in model.lower()


# --- (1) V1: engine error bodies never reach the model ------------------------

def _fail(status, diag):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps({
        "resourceType": "OperationOutcome",
        "error": f"{CANARY}-code", "message": f"{CANARY}-message {diag}",
        "issue": [{"severity": "error", "code": "exception",
                   "diagnostics": f"{CANARY}-diag {diag}",
                   "details": {"text": f"{CANARY}-details Zelda Quornwhistle"}}],
    }).encode()
    r.headers["Content-Type"] = "application/fhir+json"
    return r


@pytest.mark.parametrize("failing", ["Condition", "AllergyIntolerance",
                                     "MedicationRequest"])
def test_engine_error_bodies_never_reach_the_model(
        cfg, svc, monkeypatch, caplog, failing):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    real_get = chain.relay.get
    hits = []

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith(f"/r6/fhir/{failing}"):
            hits.append(url)
            return _fail(503, "token=sk-test-sec945")
        return real_get(url, params=params, headers=headers, timeout=timeout)
    monkeypatch.setattr(chain.relay, "get", get)
    model, sse, events = _turn_capturing(chain, monkeypatch)
    assert hits, "the failure was never injected"
    _no_canary(model, sse, events, caplog.text)
    assert "sk-test-sec945" not in model + sse + events + caplog.text
    assert "503" not in model


def test_a_failed_count_body_never_reaches_the_model(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    """Empty lists drive record_count; its failure body is a new path."""
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    from models import db
    from r6.models import R6Resource
    with chain.engine_app.app_context():
        R6Resource.query.filter_by(tenant_id=TENANT).delete()
        db.session.commit()
    real_get = chain.relay.get

    def get(url, params=None, headers=None, timeout=None):
        if (params or {}).get("_summary") == "count":
            return _fail(500, "count")
        return real_get(url, params=params, headers=headers, timeout=timeout)
    monkeypatch.setattr(chain.relay, "get", get)
    model, sse, events = _turn_capturing(chain, monkeypatch)
    _no_canary(model, sse, events, caplog.text)
    assert "could not check whether records are on file" in model


@pytest.mark.parametrize("tool", ["get_labs", "get_care_gaps",
                                  "appointment_brief",
                                  "search_records", "show_lab_timeline"])
def test_other_tool_failures_hand_over_no_exception_text(
        cfg, svc, monkeypatch, caplog, tool):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    real_get, real_post = chain.relay.get, chain.relay.post

    def get(url, params=None, headers=None, timeout=None):
        if "/r6/fhir/" in url and "_summary" not in (params or {}):
            return _fail(502, "get")
        return real_get(url, params=params, headers=headers, timeout=timeout)

    def post(url, json=None, headers=None, timeout=None, data=None):
        if "$" in url:
            return _fail(500, "post")
        return real_post(url, json=json, headers=headers, timeout=timeout,
                         data=data)
    monkeypatch.setattr(chain.relay, "get", get)
    monkeypatch.setattr(chain.relay, "post", post)
    model, sse, events = _turn_capturing(chain, monkeypatch, tool=tool)
    _no_canary(model, sse, events, caplog.text)
    assert "records_unavailable" in model, "the tool never failed"


# --- (2) NKA is never inferred; status matching is keyed by system+code ------

#: The "no known allergy" codes left this list when #946 was decided: they
#: are quoted as dated entries, never as status or absence, and
#: tests/test_careagents_nka_946.py pins how.
@pytest.mark.parametrize("system,code", [
    (SNOMED, "1150001000175102"),
    ("http://loinc.org", "UNK"),                 # status code, wrong system
    ("http://snomed.info/sct/", "1631000175102"),  # near-miss system
    ("", "1631000175102"),                        # code only
    (NULLFLAVOR.upper(), "UNK"),
    (DAR, "UNKNOWN"),
])
def test_no_status_match_and_no_absence_outside_the_closed_set(system, code):
    res = {"resourceType": "AllergyIntolerance",
           "code": {"coding": [{"system": system, "code": code}]}}
    assert _is_allergy_status(res) is False
    out = json.dumps(_summarize_bundle(
        {"resourceType": "Bundle", "entry": [{"resource": res}]})).lower()
    assert "no known" not in out and "not_an_allergy" not in out
    assert "no allergies" not in out


@pytest.mark.parametrize("system,code,status", [
    (SNOMED, "1631000175102", True), (NULLFLAVOR, "NI", False),
    (NULLFLAVOR, "ASKU", False), (DAR, "asked-unknown", False),
    (DAR, "asked-declined", False)])
def test_status_statement_never_says_absence(system, code, status):
    # Round two (F3): only explicit SNOMED status concepts are status; a
    # null flavor or data-absent-reason reads as a real allergy record.
    out = json.dumps(_summarize_bundle({"resourceType": "Bundle", "entry": [
        {"resource": _allergy("x", (system, code))}]})).lower()
    assert "no known" not in out
    assert "you have no allergies" not in out
    assert ("don't say whether you have allergies" in out) is status


def test_a_real_allergen_coding_is_never_hidden_by_a_status_coding():
    """A record whose codings are a real allergen AND a null flavor: the
    allergen must still reach the model. Hiding it behind "your records
    don't say whether you have allergies" tells a person with a recorded
    penicillin allergy that the allergy is unknown."""
    res = _allergy("mixed", (SNOMED, "91936005"), (NULLFLAVOR, "UNK"))
    [item] = _summarize_bundle({"resourceType": "Bundle",
                                "entry": [{"resource": res}]})
    assert not item.get("not_an_allergy"), item


def test_a_real_allergy_beside_a_status_record_is_not_called_unknown():
    """Two records: penicillin, and 'Patient not asked'. The model is told,
    for the second, to say the records don't say whether the person has
    allergies, which is false while the first is in the same list."""
    out = _summarize_bundle({"resourceType": "Bundle", "entry": [
        {"resource": _allergy("pen", (SNOMED, "91936005"))},
        {"resource": _allergy("pna", (SNOMED, "1631000175102"))}]})
    real = [i for i in out if not i.get("not_an_allergy")]
    assert real, out
    status = [i for i in out if i.get("not_an_allergy")]
    for i in status:
        assert "don't say whether" not in json.dumps(i), (
            "an unknown-status statement sits beside a recorded allergy")


# --- (3) V6: record_count / the note are this tenant's alone ------------------

class _Spy:
    def __init__(self, hc):
        self.hc, self.tenants = hc, []

    def __getattr__(self, name):
        return getattr(self.hc, name)

    def search(self, tenant, rt, params=None):
        self.tenants.append(tenant)
        return self.hc.search(tenant, rt, params)

    def record_count(self, tenant):
        self.tenants.append(("count", tenant))
        return type(self.hc).record_count(self, tenant)


def test_records_in_another_tenant_never_drive_the_on_file_note(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)       # TENANT holds the sample chart
    other = "ca-sec945-empty"
    monkeypatch.setenv("PUBLIC_TENANTS", f"test-tenant,{TENANT},{other}")
    spy = _Spy(chain.hc)
    out = json.loads(_execute_tool(spy, other, "get_health_summary", {}, []))
    assert all(out[k] == [] for k in ("conditions", "medications",
                                      "medication_statements", "allergies"))
    assert "No records have arrived here yet" in out["note"]
    assert "on file" not in out["note"]
    assert set(t if isinstance(t, str) else t[1]
               for t in spy.tenants) == {other}
    # And the other way: the populated tenant never reads the empty one.
    spy2 = _Spy(chain.hc)
    out2 = json.loads(_execute_tool(spy2, TENANT, "get_health_summary", {},
                                    []))
    assert "note" not in out2
    assert set(spy2.tenants) == {TENANT}


def test_an_injected_tenant_header_value_cannot_widen_the_count(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    for hostile in (f"{TENANT},x", "*", f"{TENANT}\r\nX-Tenant-Id: {TENANT}",
                    "../" + TENANT):
        try:
            n = chain.hc.record_count(hostile)
        except Exception:  # noqa: BLE001 - a refusal is the pass
            continue
        assert n == 0, (hostile, n)


# --- (5) V2: the count is audited, PHI-free, and fails closed -----------------

def test_count_reads_are_audited_and_detail_is_phi_free(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    from models import db
    from r6.models import AuditEventRecord, R6Resource
    with chain.engine_app.app_context():
        R6Resource.query.filter_by(tenant_id=TENANT).delete()
        db.session.commit()
        before = AuditEventRecord.query.count()
    _execute_tool(chain.hc, TENANT, "get_health_summary", {}, [])
    with chain.engine_app.app_context():
        rows = AuditEventRecord.query.all()[before:]
        types = [r.resource_type for r in rows]
    # 4 list searches + the counted types.
    assert len(rows) >= 4 + len(chain.hc.COUNTED_TYPES), types
    _no_canary(_audit_details(chain))


def test_an_audit_failure_on_the_count_is_never_read_as_zero(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    chain.engine_app.config["PROPAGATE_EXCEPTIONS"] = False
    from models import db
    from r6.models import R6Resource
    with chain.engine_app.app_context():
        R6Resource.query.filter_by(tenant_id=TENANT).delete()
        db.session.commit()
    import r6.audit as audit
    real = audit._new_audit_event
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] > 4:      # the four list reads pass; the counts fail
            raise RuntimeError("forced audit failure")
        return real(*a, **k)
    monkeypatch.setattr(audit, "_new_audit_event", flaky)
    try:
        out = json.loads(_execute_tool(chain.hc, TENANT, "get_health_summary",
                                       {}, []))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"audit failure escaped the tool: {type(exc).__name__}")
    assert "No records have arrived" not in out.get("note", "")
    assert "could not check" in out.get("note", "")


def test_an_audit_failure_on_a_list_read_is_could_not_read(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    chain.engine_app.config["PROPAGATE_EXCEPTIONS"] = False
    import r6.audit as audit

    def boom(*a, **k):
        raise RuntimeError("forced audit failure")
    monkeypatch.setattr(audit, "_new_audit_event", boom)
    out = json.loads(_execute_tool(chain.hc, TENANT, "get_health_summary",
                                   {}, []))
    for k in ("conditions", "medications", "medication_statements",
              "allergies"):
        assert out[k] == {"could_not_read": True}, (k, out[k])
    assert "never say the person has none" in out["note"]
