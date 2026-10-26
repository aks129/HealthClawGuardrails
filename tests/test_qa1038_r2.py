"""QA for #1038, round 2 (head 10ebdf2e). Synthetic records only.

Its own file: tests/test_qa1038_brief_handoff.py is now held by the Dev
worktree (lane check).

Two tests are RED on 10ebdf2e on purpose, as reproductions:
- a broad SNOMED identity coding still merges two different cancers;
- a year-only onset wins over a full date and the row loses its date.
"""

import json

from careagents import brief as ca_brief
from r6.brief.engine import (
    RESULT_ON_FILE,
    build_labs,
    build_problems,
)
from r6.models import R6Resource, db

PLANTED = "Jane Secret"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
SCT = "http://snomed.info/sct"
LOINC = "http://loinc.org"
_URL = "/r6/fhir/AppointmentBrief"


def _condition(cid, codings, onset, status="active"):
    r = {"resourceType": "Condition", "id": cid,
         "clinicalStatus": {"coding": [{
             "system": "http://terminology.hl7.org/CodeSystem/"
                       "condition-clinical", "code": status}]},
         "code": {"coding": [{"system": s, "code": c, "display": PLANTED}
                             for s, c in codings], "text": PLANTED}}
    if onset:
        r["onsetDateTime"] = onset
    return r


# --- F1 residual: the identity coding can be a broad SNOMED concept --------

def test_a_broad_snomed_identity_does_not_merge_two_different_cancers():
    """SNOMED 363346000 is "Malignant neoplastic disease", the parent of
    every cancer. A feed that maps a problem to it, with the specific site in
    ICD-10-CM, gives lung (C34.90) and breast (C50.919) cancer the same
    identity, so one disappears from the clinician's handout. RED on
    10ebdf2e: one row, `breast` gone."""
    rows = build_problems([
        _condition("lung", [(SCT, "363346000"), (ICD10, "C34.90")],
                   "2024-01-10"),
        _condition("breast", [(SCT, "363346000"), (ICD10, "C50.919")],
                   "2025-06-01"),
    ])
    assert sorted(r.source_id for r in rows) == ["breast", "lung"]


def test_a_year_only_onset_does_not_erase_a_dated_one():
    """"2019" sorts before "2019-05-01" as a string, so the year-only entry
    stands for both, and _onset_display shows nothing for a bare year: the
    row reads "Active" where it read "Active since May 2019". RED on
    10ebdf2e."""
    rows = build_problems([
        _condition("full", [(ICD10, "E11.9")], "2019-05-01"),
        _condition("year", [(ICD10, "E11.9")], "2019"),
    ])
    assert len(rows) == 1
    assert rows[0].value == "Active since May 2019", rows[0].value


# --- F4: "Result on file" carries nothing the record wrote ------------------

def _obs(oid, vq=None, code="3016-3", **extra):
    o = {"resourceType": "Observation", "id": oid,
         "code": {"coding": [{"system": LOINC, "code": code,
                              "display": PLANTED}], "text": PLANTED},
         "effectiveDateTime": "2026-09-01"}
    if vq is not None:
        o["valueQuantity"] = vq
    o.update(extra)
    return o


def test_result_on_file_never_carries_the_spoofed_unit_or_the_number():
    rows = build_labs([
        _obs("both", {"value": 42.5, "code": PLANTED,
                      "unit": f"{PLANTED} 42.5"}),
        _obs("case", {"value": 42.5, "code": "UIU/ML", "unit": "UIU/ML"}),
        _obs("bp", code="85354-9", component=[
            {"code": {"coding": [{"system": LOINC, "code": "8480-6"}]},
             "valueQuantity": {"value": 142.5, "unit": f"mm {PLANTED}"}},
            {"code": {"coding": [{"system": LOINC, "code": "8462-4"}]},
             "valueQuantity": {"value": 80, "unit": "mmHg"}}]),
    ])
    got = {r.source_id: r.value for r in rows}
    on_file = f"{RESULT_ON_FILE} (Sep 1, 2026)"
    assert got == {"both": on_file, "case": on_file, "bp": on_file}
    text = json.dumps([r.__dict__ for r in rows])
    assert PLANTED not in text and "Jane" not in text
    assert "42.5" not in text and "142" not in text


def test_a_value_that_is_not_a_number_is_still_dropped():
    rows = build_labs([
        _obs("nan", {"value": "NaN", "unit": "kg"}),
        _obs("str", {"value": f"81 kg {PLANTED}", "unit": "kg"}),
        _obs("none", {"unit": "kg"}),
        _obs("vstr", valueString=f"7.1 {PLANTED}"),
    ])
    assert rows == []


# --- F5 across the boundary: the engine's wire carries `cause` --------------

def test_the_route_sends_the_demographic_cause_the_page_reads(
        app, client, tenant_id, tenant_headers):
    """The page tests hand-build the brief with a `cause` extension. This is
    the engine's own wire for a Patient with no birth date, read back with
    CareAgents' own parser. MUTATION: stop the route emitting `cause`
    (r6/brief/routes.py) -> red; it survived every other test."""
    with app.app_context():
        db.session.add(R6Resource(
            resource_type="Patient", resource_id="p-nodob",
            resource_json=json.dumps({"resourceType": "Patient",
                                      "id": "p-nodob", "gender": "male"}),
            tenant_id=tenant_id))
        db.session.commit()
    r = client.get(_URL, headers=tenant_headers)
    assert r.status_code == 200
    body = r.get_json()
    cause = ca_brief.care_gaps_marker(body, "cause")
    assert cause == "birth-date-unavailable"
    assert cause in ca_brief.DEMOGRAPHIC_CAUSES
    assert "date of birth" in ca_brief.care_gaps_marker(body, "reason")
