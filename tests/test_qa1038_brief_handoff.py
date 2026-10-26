"""QA for #1038, the visit brief a patient hands a clinician.

Adversarial cases the PR's own tests do not reach. Every resource is
synthetic; PLANTED stands for a name an upstream feed could carry in
`display` or `text`, and must never reach the brief.

Two tests here are RED on the PR head on purpose: they reproduce a defect
the PR introduces (problem-list dedupe merges distinct conditions that share
any one coding). One is a strict xfail: a pre-existing gap the PR names in
its Known limits (missing date of birth still reads "couldn't check just
now"). A strict xfail goes red when the gap is FIXED; remove the marker then
(AGENTS.md, "a strict-xfail row ... goes red when you fix what it pins").
"""

import json

import pytest

from r6.brief.engine import (
    CARE_GAPS_UNAVAILABLE,
    build_care_gaps,
    build_labs,
    build_problems,
    build_visits,
)
from r6.caregaps.evaluate import evaluate_care_gaps
from r6.caregaps.report import build_consumer_summary
from tests.careagents_consent_helpers import consented
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)

PLANTED = "Jane Secret"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
SCT = "http://snomed.info/sct"
LOINC = "http://loinc.org"
ACT = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
_AS_OF = "2026-10-26"
_CARE_GAPS_SECTION = ("https://healthclaw.io/fhir/StructureDefinition/"
                      "brief-section-care-gaps")


def _condition(cid, codings, onset, status="active"):
    return {
        "resourceType": "Condition", "id": cid,
        "clinicalStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
            "code": status}]},
        "code": {"coding": [{"system": s, "code": c, "display": PLANTED}
                            for s, c in codings], "text": PLANTED},
        "onsetDateTime": onset,
    }


# --- 1. dedupe must never hide a distinct active condition ------------------

def test_two_cancers_sharing_only_an_icd10_rollup_both_stay_listed():
    """Real problem lists carry a specific SNOMED concept beside a coarser
    ICD-10-CM code. Non-small cell and small cell lung cancer are different
    diseases; both roll up to C34.90 when the site is unspecified. Merging on
    any one shared coding drops one active cancer from the clinician's
    handout. RED on #1038 head ede9812a: one row, `nsclc` gone."""
    rows = build_problems([
        _condition("nsclc", [(SCT, "254637007"), (ICD10, "C34.90")],
                   "2024-01-10"),
        _condition("sclc", [(SCT, "254632001"), (ICD10, "C34.90")],
                   "2025-06-01"),
    ])
    assert sorted(r.source_id for r in rows) == ["nsclc", "sclc"]


def test_an_entry_sharing_one_code_with_each_of_two_others_hides_neither():
    """Union through a shared coding is transitive: an entry coded with
    diabetes and I10 pulls the diabetes row AND the hypertension row into one
    slot, and only the latest onset survives. RED on #1038 head ede9812a:
    one row, `htn`; diabetes is gone from the brief."""
    rows = build_problems([
        _condition("dm", [(SCT, "44054006")], "2020-01-01"),
        _condition("mixed", [(SCT, "44054006"), (ICD10, "I10")], "2021-01-01"),
        _condition("htn", [(ICD10, "I10"), (SCT, "38341003")], "2022-01-01"),
    ])
    ids = {r.source_id for r in rows}
    assert {"dm", "htn"} <= ids, ids


def test_history_and_status_z_codes_stay_on_the_problem_list():
    """Z79 (long-term drug therapy), Z86/Z87 (history), Z91 (allergy
    status), Z95 (cardiac implant), Z99 (dependence on machine) describe the
    person, not the visit. MUTATION: widen the encounter rule to Z\\d\\d ->
    all of them disappear."""
    keep = ["Z79.4", "Z79.01", "Z86.73", "Z87.891", "Z91.010", "Z95.1",
            "Z99.2", "Z14.1", "Z15.01"]
    drop = ["Z00.00", "Z01.419", "Z12.31", "Z13.89", "Z23"]
    rows = build_problems(
        [_condition(c, [(ICD10, c)], "2024-01-01") for c in keep + drop])
    assert [r.source_id for r in rows] == keep
    assert all(PLANTED not in r.label for r in rows)


# --- 3. due first, then undecided, on the real evaluator --------------------

def test_due_lines_come_before_the_undecided_one():
    """The evaluator emits colorectal (indeterminate) between due lines.
    The brief lists every due line first. MUTATION: drop the sort in
    build_care_gaps -> colorectal is third, not last."""
    consumer = build_consumer_summary(evaluate_care_gaps(
        {"resourceType": "Patient", "id": "p", "birthDate": "1970-06-01",
         "gender": "female"}, as_of=_AS_OF))
    producer = [ln["status"] for ln in consumer["lines"]]
    assert producer.index("indeterminate") < len(producer) - 1, producer
    section = build_care_gaps({"consumer": consumer})
    assert section.status == CARE_GAPS_UNAVAILABLE
    labels = [f.label for f in section.fields]
    assert labels[-1] == "Colorectal cancer screening", labels
    assert len(labels) == 6
    assert section.fields[-1].value.startswith("We could not check whether")
    assert all(f.value.startswith("You may be due")
               for f in section.fields[:-1])


def _page(app, svc, monkeypatch, brief):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = c.post("/api/connections/direct",
                     json=consented()).get_json()["id"]
    agent_id = c.post("/api/agents", json={
        "name": "Ada", "persona": "direct",
        "connection_id": conn_id}).get_json()["id"]
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant, **_: brief)
    resp = c.get(f"/brief?agent={agent_id}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _brief_for(patient):
    section = build_care_gaps({"consumer": build_consumer_summary(
        evaluate_care_gaps(patient, as_of=_AS_OF))})
    sub = [{"url": "field", "valueString": json.dumps({
        "label": f.label, "value": f.value, "sourceType": f.source_type,
        "sourceId": f.source_id})} for f in section.fields]
    sub.append({"url": "status", "valueString": section.status})
    sub.append({"url": "reason", "valueString": section.reason})
    return section, {"resourceType": "Basic", "extension": [
        {"url": _CARE_GAPS_SECTION, "extension": sub}]}


@pytest.mark.xfail(strict=True, reason=(
    "QA #1038: pre-existing, named in the PR's Known limits. With no date "
    "of birth every rule is undecided, no line is produced, and the page "
    "drops the engine's reason for 'couldn't check just now'."))
def test_missing_birth_date_says_why_not_just_now(app, svc,  # noqa: F811
                                                  monkeypatch):
    section, brief = _brief_for(
        {"resourceType": "Patient", "id": "p", "gender": "male"})
    assert section.fields == []
    assert "date of birth" in section.reason
    body = _page(app, svc, monkeypatch, brief)
    assert "couldn't check your screenings just now" not in body
    assert "date of birth" in body


# --- 7. units: a spoofed code or unit never carries text --------------------

def _weight(oid, vq, when):
    return {"resourceType": "Observation", "id": oid,
            "code": {"coding": [{"system": LOINC, "code": "29463-7",
                                 "display": PLANTED}], "text": PLANTED},
            "effectiveDateTime": when, "valueQuantity": vq}


def test_a_spoofed_unit_beside_an_allowlisted_one_shows_only_the_token():
    """The PR's test spoofs `unit` alone. Here one of `code`/`unit` is an
    allowlist token and the other carries a name: only the token reaches the
    page, byte for byte."""
    rows = build_labs([
        _weight("a", {"value": 80, "code": "kg", "unit": f"kg {PLANTED}"},
                "2026-09-03"),
        _weight("b", {"value": 81, "code": PLANTED, "unit": "kg"},
                "2026-09-02"),
        _weight("c", {"value": 82, "code": "KG", "unit": "Kg"}, "2026-09-01"),
        _weight("d", {"value": 83, "code": "[lb_av]",
                      "unit": f"lb {PLANTED}"}, "2026-08-31"),
    ])
    got = {r.source_id: (r.label, r.value) for r in rows}
    assert got == {
        "a": ("Body weight", "80 kg (Sep 3, 2026)"),
        "b": ("Body weight", "81 kg (Sep 2, 2026)"),
        "d": ("Body weight", "83 lb (Aug 31, 2026)"),
    }
    assert PLANTED not in json.dumps(got)


# --- 5. visits: no upstream text through any class shape -------------------

def test_no_visit_shape_carries_upstream_text():
    """R4 Coding class with display, an R4 class written as a bare
    CodeableConcept with text, the R5/R6 list shape with text, and a type
    that is text only. MUTATION: read `text` or `display` anywhere in
    _visit_kind -> PLANTED reaches a label."""
    encs = [
        {"resourceType": "Encounter", "id": "r4", "status": "finished",
         "class": {"system": ACT, "code": "AMB", "display": PLANTED},
         "period": {"start": "2026-10-06"}},
        {"resourceType": "Encounter", "id": "cc", "status": "finished",
         "class": {"coding": [{"system": ACT, "code": "EMER",
                               "display": PLANTED}], "text": PLANTED},
         "period": {"start": "2026-10-05"}},
        {"resourceType": "Encounter", "id": "r5", "status": "finished",
         "class": [{"coding": [{"system": ACT, "code": "VR",
                                "display": PLANTED}], "text": PLANTED}],
         "period": {"start": "2026-10-04"}},
        {"resourceType": "Encounter", "id": "txt", "status": "finished",
         "type": [{"text": PLANTED}],
         "class": [{"text": PLANTED}],
         "period": {"start": "2026-10-03"}},
    ]
    labels = {r.source_id: r.label for r in build_visits(encs)}
    assert labels == {
        "r4": "Office visit (Oct 6, 2026)",
        "cc": "Emergency visit (Oct 5, 2026)",
        "r5": "Video visit (Oct 4, 2026)",
        "txt": "Visit (Oct 3, 2026)",
    }
