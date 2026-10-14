"""The first answer is never a false "no records".

Seen on the owner's account (2026-10-12): a chat opened with "show me
summary" and the answer said no health information was connected and it
could not see conditions, medications or labs. The next message listed the
records, prefaced "it looks like I needed a moment to find your records".
The connection held 263 records.

Nothing in a turn told the model records existed. `get_health_summary` was
all or nothing over four searches: one failed search raised out of the tool
and the model got `{"error": "search Condition failed (503)"}` with no word
on what that meant. A tenant whose records had not landed returned four
empty lists, the same bytes as "none". And the page's own greeting, which
counts the records, never reaches the model. Each of these reads to a model
as "nothing connected".

Also on this answer path: an allergy-status code ("Patient not asked",
SNOMED 1631000175102) labelled by the terminology resolver reached the model
as an allergy's name, and the truncation marker's counts were read out to a
patient as "I could only see 12 of 19" (#939).

The cross-layer tests drive the real client onto the real engine.
"""

from __future__ import annotations

import json
import re

import pytest
import requests

from careagents import personas
from careagents.agent import _execute_tool, _summarize_bundle
from careagents.healthclaw import HealthClawError
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, _turn, cfg, svc)
from tests.test_careagents_imessage_demo import _Call, _Turn

SUMMARY_KEYS = ("conditions", "medications", "medication_statements",
                "allergies")
#: What a model must never be handed as fact about a connected person.
ABSENCE = re.compile(r"no (health )?(records|information) (is |are )?"
                     r"connected|has no (conditions|medications|allergies)",
                     re.I)


class _HC:
    """Answers each search from `bundles`; a type in `fail` raises."""

    def __init__(self, bundles=None, fail=(), count=None):
        self.bundles, self.fail, self.count = bundles or {}, set(fail), count
        self.counted = 0

    def search(self, tenant, rt, params=None):
        if rt in self.fail:
            raise HealthClawError(f"search {rt} failed (503)", 503)
        return self.bundles.get(rt, {"resourceType": "Bundle", "total": 0})

    def read(self, tenant, rt, rid):
        raise HealthClawError("read failed", 503)

    def record_count(self, tenant):
        self.counted += 1
        if isinstance(self.count, Exception):
            raise self.count
        return self.count


def _bundle(*resources):
    return {"resourceType": "Bundle", "total": len(resources),
            "entry": [{"resource": r} for r in resources]}


_CONDITION = {"resourceType": "Condition",
              "code": {"text": "Type 2 diabetes mellitus"}}


def _summary(hc):
    return json.loads(_execute_tool(hc, "t", "get_health_summary", {}, []))


# --- 1. a failed search is a failed read, not an empty list -------------------

def test_one_failed_search_does_not_sink_the_summary():
    hc = _HC({"Condition": _bundle(_CONDITION)}, fail={"MedicationRequest"},
             count=263)
    out = _summary(hc)
    assert out["conditions"][0]["name"] == "Type 2 diabetes mellitus"
    assert out["medications"] == {"could_not_read": True}
    note = out["note"].lower()
    assert "could not be read" in note and "not absent" in note
    assert "503" not in json.dumps(out)


def test_every_search_failing_is_still_an_answer_with_a_note():
    hc = _HC(fail={"Condition", "MedicationRequest", "MedicationStatement",
                   "AllergyIntolerance"},
             count=HealthClawError("count failed", 503))
    out = _summary(hc)
    assert all(out[k] == {"could_not_read": True} for k in SUMMARY_KEYS)
    note = out["note"].lower()
    assert "could not be read" in note and "try again" in note
    assert not ABSENCE.search(out["note"])


# --- 2. empty lists say why they are empty -----------------------------------

def test_empty_lists_with_records_on_file_say_records_exist():
    out = _summary(_HC(count=263))
    note = out["note"].lower()
    assert "some records are on file" in note
    assert "never say" in note and "no records" in note
    # The count spans labs AND immunizations: no claim about which kind.
    assert "on file for this person, such as" not in note


def test_empty_lists_with_nothing_landed_say_still_loading():
    out = _summary(_HC(count=0))
    note = out["note"].lower()
    assert "still loading" in note
    assert "never say" in note


def test_empty_lists_when_the_count_fails_claim_nothing():
    out = _summary(_HC(count=HealthClawError("count failed", 503)))
    note = out["note"].lower()
    assert "could not check" in note
    assert "never say" in note


def test_a_readable_summary_pays_for_no_count_and_carries_no_note():
    hc = _HC({"Condition": _bundle(_CONDITION)}, count=263)
    out = _summary(hc)
    assert hc.counted == 0
    assert "note" not in out


# --- 3. a tool that raises reaches the model as a read failure --------------

def test_a_tool_error_tells_the_model_it_is_not_absence():
    from careagents.agent import tool_error_result
    out = json.loads(tool_error_result(HealthClawError(
        "$interpret failed (503)", 503)))
    assert out["error"] == "records_unavailable"
    note = out["note"].lower()
    assert "not absent" in note and "try again" in note
    assert "503" not in json.dumps(out) and "$interpret" not in json.dumps(out)


def test_the_turn_loop_hands_over_the_read_failure(cfg):  # noqa: F811
    from careagents import agent

    class _Raising:
        def interpret_labs(self, tenant):
            raise HealthClawError("$interpret failed (503)", 503)

    seen = []

    def complete(_cfg, system, history, tools):
        results = [m for m in history if m.get("role") == "tool"]
        if not results:
            return _Turn("", [_Call(0, "get_labs", {})])
        seen.append(results[-1]["content"])
        return _Turn("ok")

    import careagents.llm as llm_mod
    orig = llm_mod.complete
    llm_mod.complete = complete
    try:
        list(agent.run_turn(cfg, _Raising(), "t", "sys", [], "labs?"))
    finally:
        llm_mod.complete = orig
    [content] = seen
    assert json.loads(content)["error"] == "records_unavailable"


# --- 4. the system prompt says records are connected -------------------------

def test_the_safety_core_forbids_saying_nothing_is_connected():
    core = " ".join(personas.SAFETY_CORE.lower().split())
    assert "never say that no records are connected" in core
    assert "read them with your tools" in core


def test_a_pending_connection_says_records_may_still_be_loading():
    arriving = personas.system_prompt("A", "calm", arriving=True)
    settled = personas.system_prompt("A", "calm")
    assert "still loading" in arriving.lower()
    assert "still loading" not in settled.lower()


# --- 5. an allergy-status code is not an allergy -----------------------------

@pytest.mark.parametrize("system,code", [
    ("http://snomed.info/sct", "1631000175102"),
])
def test_an_allergy_status_code_reads_as_records_not_saying(system, code):
    # The label is the terminology service's, keyed by code: the resolver
    # gives "Patient not asked" for the SNOMED one.
    res = {"resourceType": "AllergyIntolerance",
           "code": {"text": "Patient not asked",
                    "coding": [{"system": system, "code": code,
                                "display": "Patient not asked"}]}}
    [item] = _summarize_bundle(_bundle(res))
    assert "name" not in item
    assert item["not_an_allergy"] is True
    assert item["statement"] == (
        "Your records don't say whether you have allergies.")
    text = json.dumps(item).lower()
    assert "not asked" not in text
    assert "no known" not in text


def test_a_real_allergy_is_unchanged():
    res = {"resourceType": "AllergyIntolerance",
           "code": {"text": "Allergy to penicillin",
                    "coding": [{"system": "http://snomed.info/sct",
                                "code": "91936005"}]}}
    [item] = _summarize_bundle(_bundle(res))
    assert item["name"] == "Allergy to penicillin"


def test_a_status_code_on_another_type_is_left_alone():
    res = {"resourceType": "Condition",
           "code": {"coding": [{"system": "http://snomed.info/sct",
                                "code": "1631000175102"}]}}
    # Coded only as status, outside allergies: no finding, so left out of
    # the list, and never an allergy statement (#949 patient tester).
    assert _summarize_bundle(_bundle(res)) == []


# --- 6. #939: truncation in plain words, no counts ---------------------------

def test_a_truncated_list_carries_no_numbers_for_the_model_to_quote():
    bundle = {"resourceType": "Bundle", "total": 19, "entry": [
        {"resource": {"resourceType": "Observation",
                      "code": {"text": "Blood pressure"}}}
        for _ in range(19)]}
    items = _summarize_bundle(bundle)
    [marker] = [i for i in items if i.get("truncated")]
    assert not re.search(r"\d", json.dumps(marker)), marker
    note = marker["note"].lower()
    assert "longer than" in note and "patient portal" in note
    assert "complete" in note
    # It cannot narrow or page, so it must not offer to (#945 tester).
    assert "narrow" not in note and "show the rest" not in note.replace(
        "do not offer to show the rest", "")


def test_truncation_uses_the_bundle_total_not_the_page():
    # The engine's page is 50; a total of 80 with 12 entries on the page
    # is still a partial list.
    bundle = {"resourceType": "Bundle", "total": 80, "entry": [
        {"resource": {"resourceType": "Condition", "code": {"text": "C"}}}
        for _ in range(5)]}
    items = _summarize_bundle(bundle)
    assert any(i.get("truncated") for i in items)


# --- 7. cross-layer: the real client on the real engine ----------------------

def _first_turn(chain, monkeypatch, message="show me summary"):
    """Run one chat turn whose model reads the summary and answers with the
    tool result it was handed. Returns (tool result, system prompt)."""
    seen = []

    def complete(_cfg, system, history, tools):
        results = [m["content"] for m in history if m.get("role") == "tool"]
        if not results:
            seen.append({"system": system})
            return _Turn("", [_Call(0, "get_health_summary", {})])
        seen[-1]["result"] = results[-1]
        return _Turn("answered")
    monkeypatch.setattr("careagents.worker.llm.complete", complete)

    c = chain.app.test_client()
    _login(c, chain.svc, monkeypatch, email="acceptance@example.com")
    from careagents.worker import RunWorker
    RunWorker(chain.app.extensions["careagents_runtime"]["config"],
              chain.hc, chain.svc, "test-worker").run_once()
    r = _turn(c, chain.agent, message)
    assert r.status_code == 200
    assert "answered" in r.get_data(as_text=True)
    [turn] = seen
    return json.loads(turn["result"]), turn["system"]


def test_first_turn_on_a_cold_worker_reads_the_records(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    out, system = _first_turn(chain, monkeypatch)
    assert [c["name"] for c in out["conditions"]] == [
        "Type 2 diabetes mellitus"]
    assert out["medications"][0]["name"] == "Metformin 500 mg"
    assert "note" not in out
    assert "never say that no records are connected" in " ".join(
        system.lower().split())


def test_first_turn_with_a_failed_engine_search_says_it_could_not_read(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    real_get = chain.relay.get
    failed = []

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/r6/fhir/Condition") and not params and not failed:
            failed.append(url)
            r = requests.Response()
            r.status_code = 503
            r._content = b"{}"
            return r
        return real_get(url, params=params, headers=headers, timeout=timeout)
    monkeypatch.setattr(chain.relay, "get", get)

    out, _system = _first_turn(chain, monkeypatch)
    assert failed, "the Condition search was never made"
    assert out["conditions"] == {"could_not_read": True}
    assert out["medications"][0]["name"] == "Metformin 500 mg"
    assert "could not be read" in out["note"].lower()


def test_first_turn_before_records_land_says_still_loading(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    from models import db
    from r6.models import R6Resource
    with chain.engine_app.app_context():
        R6Resource.query.filter_by(tenant_id=TENANT).delete()
        db.session.commit()
    # A Fasten connection just made: pending, and the engine has no job
    # for it yet. (Made-up records never arrive, so the sample cannot.)
    _as_fasten(svc)
    svc.set_connection_status(TENANT, "pending")
    out, system = _first_turn(chain, monkeypatch)
    assert all(out[k] == [] for k in SUMMARY_KEYS)
    assert "still loading" in out["note"].lower()
    assert personas.ARRIVING_STYLE in system


def test_patient_not_asked_reaches_the_model_as_plain_words(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    # Production runs the terminology resolver, which labels this code
    # "Patient not asked" (tx.fhir.org, 2026-10-12).
    monkeypatch.setattr(
        "r6.terminology_resolver.resolve",
        lambda s, c: "Patient not asked" if c == "1631000175102" else None)
    from models import db
    from r6.models import R6Resource
    res = {"resourceType": "AllergyIntolerance", "id": "fa-allergy-1",
           "patient": {"reference": "Patient/demo-patient-rivera"},
           "code": {"coding": [{"system": "http://snomed.info/sct",
                                "code": "1631000175102"}]}}
    with chain.engine_app.app_context():
        db.session.add(R6Resource("AllergyIntolerance", json.dumps(res),
                                  resource_id=res["id"], tenant_id=TENANT))
        db.session.commit()
    out, _system = _first_turn(chain, monkeypatch)
    [allergy] = out["allergies"]
    assert allergy["statement"] == (
        "Your records don't say whether you have allergies.")
    assert "not asked" not in json.dumps(out).lower()


def test_a_failed_tool_in_the_worker_reaches_the_model_as_a_read_failure(
        cfg, svc, monkeypatch):  # noqa: F811
    # The production path: the run worker, not run_turn.
    chain = Chain(cfg, svc, monkeypatch)
    real_post = chain.relay.post

    def post(url, json=None, headers=None, timeout=None, data=None):
        if "/Observation/$interpret" in url:
            r = requests.Response()
            r.status_code = 503
            r._content = b"{}"
            return r
        return real_post(url, json=json, headers=headers, timeout=timeout,
                         data=data)
    monkeypatch.setattr(chain.relay, "post", post)

    seen = []

    def complete(_cfg, system, history, tools):
        results = [m["content"] for m in history if m.get("role") == "tool"]
        if not results:
            return _Turn("", [_Call(0, "get_labs", {})])
        seen.append(results[-1])
        return _Turn("answered")
    monkeypatch.setattr("careagents.worker.llm.complete", complete)

    c = chain.app.test_client()
    _login(c, svc, monkeypatch, email="acceptance@example.com")
    from careagents.worker import RunWorker
    RunWorker(cfg, chain.hc, svc, "test-worker").run_once()
    r = _turn(c, chain.agent, "what do my labs say?")
    assert r.status_code == 200
    [content] = seen
    out = json.loads(content)
    assert out["error"] == "records_unavailable"
    assert "not absent" in out["note"].lower()
    assert "503" not in content and "$interpret" not in content


# --- 8. the visit brief: a cut section says so, without counts ---------------

def _brief_with(section, n):
    from careagents import brief as brief_mod
    return {"resourceType": "Basic", "extension": [{
        "url": brief_mod.SECTION_PREFIX + section,
        "extension": [{"valueString": json.dumps(
            {"label": f"Item {chr(65 + i)}", "value": "on file"})}
            for i in range(n)]}]}


class _BriefHC:
    def __init__(self, resource):
        self.resource = resource

    def fetch_appointment_brief(self, tenant):
        return self.resource


def test_a_cut_brief_section_carries_no_counts_for_the_model_to_quote():
    out = json.loads(_execute_tool(_BriefHC(_brief_with("problems", 9)), "t",
                                   "appointment_brief", {}, []))
    assert len(out["sections"]["problems"]) == 6
    note = out["note"]
    assert not re.search(r"\d", note), note
    lowered = note.lower()
    assert "problems" in lowered and "more" in lowered
    assert "complete" in lowered and "visit brief page" in lowered
    assert "narrow" not in lowered


def test_an_uncut_brief_section_says_nothing_about_more():
    out = json.loads(_execute_tool(_BriefHC(_brief_with("problems", 3)), "t",
                                   "appointment_brief", {}, []))
    assert "narrow" not in out["note"].lower()


@pytest.mark.parametrize("system,code", [
    ("http://terminology.hl7.org/CodeSystem/v3-NullFlavor", "NASK"),
    ("http://terminology.hl7.org/CodeSystem/v3-NullFlavor", "UNK"),
    ("http://terminology.hl7.org/CodeSystem/data-absent-reason",
     "asked-unknown"),
])
def test_a_null_flavored_allergy_code_is_still_an_allergy_record(
        system, code):
    # C-CDA writes an uncoded allergen as nullFlavor UNK beside free text
    # that redaction strips: a real allergy whose substance is not coded
    # (#945 security F3). It reads as a record, never as "don't say".
    res = {"resourceType": "AllergyIntolerance",
           "code": {"coding": [{"system": system, "code": code}]}}
    [item] = _summarize_bundle(_bundle(res))
    assert "not_an_allergy" not in item
    assert item["unreadable"] is True


_NOT_ASKED = {"resourceType": "AllergyIntolerance",
              "code": {"coding": [{"system": "http://snomed.info/sct",
                                   "code": "1631000175102"}]}}
_PENICILLIN = {"resourceType": "AllergyIntolerance",
               "code": {"text": "Allergy to penicillin",
                        "coding": [{"system": "http://snomed.info/sct",
                                    "code": "91936005"}]}}


@pytest.mark.parametrize("tail", [[_PENICILLIN], []],
                         ids=["allergy-past-the-cut", "status-only-but-cut"])
def test_a_cut_allergy_list_never_says_the_records_are_silent(tail):
    # Decided on the full result, not the page (#945 security F5): what is
    # past the cut may be a recorded allergy.
    items = _summarize_bundle(_bundle(*([_NOT_ASKED] * 13), *tail))
    assert any(i.get("truncated") for i in items)
    assert not any("don't say whether" in json.dumps(i) for i in items)
    assert all("statement" not in i for i in items)


# --- 9. still arriving: the import, not the stored status (QA945-F1) ---------
#
# The chat page settles a connection to `active` once anything lands, so a
# first answer mid-import never heard that records were still arriving.
# The engine's Fasten job list (#940) is what knows; a look that fails says
# "may still be loading" rather than nothing.

class _Jobs:
    def __init__(self, state):
        self.state, self.asked = state, 0

    def fasten_import_state(self, tenant):
        self.asked += 1
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


@pytest.mark.parametrize("state,status,arriving", [
    ("running", "active", True),
    ("running", "pending", True),
    ("done", "pending", False),
    ("failed", "active", False),
    ("none", "pending", True),
    ("none", "active", False),
    (HealthClawError("import status failed (503)", 503), "active", True),
])
def test_arriving_follows_the_import_and_fails_closed(state, status,
                                                      arriving):
    from careagents.agent import records_arriving
    hc = _Jobs(state)
    assert records_arriving(hc, "t", {"kind": "fasten",
                                      "status": status}) is arriving
    assert hc.asked == 1


def test_made_up_records_are_never_arriving_and_cost_no_look():
    from careagents.agent import records_arriving
    hc = _Jobs(HealthClawError("down", 503))
    assert records_arriving(hc, "t", {"kind": "sample",
                                      "status": "active"}) is False
    assert hc.asked == 0


def test_records_on_file_mid_import_say_more_may_still_be_loading():
    hc = _HC(count=39)
    out = json.loads(_execute_tool(hc, "t", "get_health_summary", {}, [],
                                   arriving=True))
    note = out["note"].lower()
    assert "some records are on file" in note
    assert "still loading" in note
    settled = json.loads(_execute_tool(_HC(count=39), "t",
                                       "get_health_summary", {}, []))
    assert "still loading" not in settled["note"].lower()


def _as_fasten(svc):  # noqa: F811
    """The Chain's connection, as a consented Fasten connection."""
    from careagents import tester_terms
    from careagents.models import Connection
    with svc.session() as s:
        for c in s.query(Connection).filter_by(tenant_id=TENANT).all():
            c.kind = "fasten"
            c.consent_version = tester_terms.CONSENT_VERSION


def _mid_import(chain, status="ingesting"):
    """Labs have landed; conditions, medications and allergies have not,
    and the engine's Fasten job is still writing."""
    from models import db
    from r6.fasten.models import FastenJob
    from r6.models import R6Resource
    with chain.engine_app.app_context():
        R6Resource.query.filter(
            R6Resource.tenant_id == TENANT,
            R6Resource.resource_type.in_((
                "Condition", "MedicationRequest", "MedicationStatement",
                "AllergyIntolerance"))).delete(synchronize_session=False)
        db.session.add(FastenJob(task_id="fa-mid-1", org_connection_id="oc-1",
                                 tenant_id=TENANT, status=status))
        db.session.commit()


@pytest.mark.parametrize("job,arriving", [("ingesting", True),
                                          ("complete", False)])
def test_mid_import_first_answer_on_the_real_engine(
        cfg, svc, monkeypatch, job, arriving):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    _as_fasten(svc)
    _mid_import(chain, status=job)
    # Settled, as the chat page leaves it once labs have landed.
    svc.set_connection_status(TENANT, "active")
    out, system = _first_turn(chain, monkeypatch)
    assert (personas.ARRIVING_STYLE in system) is arriving
    assert ("still loading" in out["note"].lower()) is arriving
    assert "some records are on file" in out["note"].lower()


def test_an_unanswered_import_look_fails_closed_on_the_real_engine(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    _as_fasten(svc)
    _mid_import(chain, status="complete")
    svc.set_connection_status(TENANT, "active")
    real_get = chain.relay.get
    looked = []

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/fasten/jobs"):
            looked.append(url)
            r = requests.Response()
            r.status_code = 503
            r._content = b"{}"
            return r
        return real_get(url, params=params, headers=headers, timeout=timeout)
    monkeypatch.setattr(chain.relay, "get", get)
    out, system = _first_turn(chain, monkeypatch)
    assert looked
    assert personas.ARRIVING_STYLE in system
    assert "still loading" in out["note"].lower()


# --- 10. a real problem list fits (#945 patient tester) ----------------------
#
# 19 conditions came back as 12, and diabetes, hypertension, CAD, AFib,
# hypothyroidism and obesity were among the seven cut. "Show me the rest"
# and "look at my heart conditions" returned the same 12: the tool cannot
# narrow, and the engine has no offset to page with.

def _conditions(n):
    return [{"resourceType": "Condition",
             "code": {"text": f"Condition {i:02d}"}} for i in range(n)]


def test_nineteen_conditions_all_reach_the_model():
    hc = _HC({"Condition": _bundle(*_conditions(19))})
    out = _summary(hc)
    assert len(out["conditions"]) == 19
    assert not any(i.get("truncated") for i in out["conditions"])


def test_sixty_conditions_show_the_engine_page_and_say_the_list_is_longer():
    from careagents.agent import SUMMARY_LIMIT
    assert SUMMARY_LIMIT == 50
    bundle = _bundle(*_conditions(50))
    bundle["total"] = 60
    out = _summary(_HC({"Condition": bundle}))
    items = out["conditions"]
    [marker] = [i for i in items if i.get("truncated")]
    assert len(items) == 51
    note = marker["note"].lower()
    assert "patient portal" in note and "narrow" not in note


def test_search_records_returns_the_engine_page_too():
    hc = _HC({"Condition": _bundle(*_conditions(19))})
    out = json.loads(_execute_tool(hc, "t", "search_records",
                                   {"resource_type": "Condition"}, []))
    assert len(out) == 19


def test_the_records_on_file_notes_name_no_kind_they_cannot_vouch_for():
    for arriving in (False, True):
        out = json.loads(_execute_tool(_HC(count=39), "t",
                                       "get_health_summary", {},
                                       [], arriving=arriving))
        note = out["note"].lower()
        assert "vaccines" not in note and "lab results" not in note


def test_the_prompt_says_not_to_narrate_tool_steps():
    prompt = " ".join(personas.system_prompt("A", "calm").lower().split())
    assert "never narrate your searches" in prompt


def _plant_conditions(chain, n):
    from models import db
    from r6.models import R6Resource
    with chain.engine_app.app_context():
        R6Resource.query.filter_by(tenant_id=TENANT,
                                   resource_type="Condition").delete()
        for i in range(n):
            res = {"resourceType": "Condition", "id": f"fa-cond-{i}",
                   "subject": {"reference": "Patient/demo-patient-rivera"},
                   "code": {"coding": [{"system": "http://snomed.info/sct",
                                        "code": "44054006"}]}}
            db.session.add(R6Resource("Condition", json.dumps(res),
                                      resource_id=res["id"],
                                      tenant_id=TENANT))
        db.session.commit()


@pytest.mark.parametrize("n,shown,cut", [(19, 19, False), (60, 50, True)])
def test_a_long_problem_list_on_the_real_engine(
        cfg, svc, monkeypatch, n, shown, cut):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    _plant_conditions(chain, n)
    out, _system = _first_turn(chain, monkeypatch)
    records = [i for i in out["conditions"] if not i.get("truncated")]
    assert len(records) == shown
    assert any(i.get("truncated") for i in out["conditions"]) is cut
