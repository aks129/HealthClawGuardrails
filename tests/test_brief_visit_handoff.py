"""The visit brief as a document a patient hands to a clinician.

Six defects seen on a real account on 2026-10-26 (no record content is
reproduced here; every resource below is synthetic):

1. One condition listed three times, from repeated problem-list entries.
2. Encounter and screening codes ("Adult health examination", "Encounter
   for screening for other disorder") listed as current conditions.
3. "We couldn't check your screenings just now" while the chat's care-gaps
   tool answered on the same account.
4. Lab names in LOINC's long form, and rows with no result at all.
5. Every visit labelled only "Visit".
(6, print, is a CareAgents page concern: tests/test_careagents_brief_handoff.py.)

Labels come from r6/terminology.py keyed by code. Each test plants an
upstream `display`/`text` that must not reach the brief.
"""

import json
from datetime import date, timedelta

from r6 import terminology
from r6.brief.engine import (
    CARE_GAPS_UNAVAILABLE,
    build_labs,
    build_problems,
    build_visits,
)
from r6.models import R6Resource, db

PLANTED = "Jane Secret"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
SCT = "http://snomed.info/sct"
LOINC = "http://loinc.org"
ACT = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
CPT = "http://www.ama-assn.org/go/cpt"
_URL = "/r6/fhir/AppointmentBrief"
_SECTION = "https://healthclaw.io/fhir/StructureDefinition/brief-section-"


def _condition(cid, system, code, onset, status="active"):
    return {
        "resourceType": "Condition", "id": cid,
        "clinicalStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
            "code": status}]},
        "code": {"coding": [{"system": system, "code": code,
                             "display": PLANTED}], "text": PLANTED},
        "onsetDateTime": onset,
    }


# --- 1. duplicates ---------------------------------------------------------

def test_repeated_problem_list_entries_collapse_to_the_most_recent_onset():
    """MUTATION: drop the dedupe -> three hypertension rows."""
    rows = build_problems([
        _condition("c1", ICD10, "I10", "2019-02-01"),
        _condition("c2", ICD10, "I10", "2023-07-15"),
        _condition("c3", ICD10, "I10", "2021-05-03"),
        _condition("c4", ICD10, "E78.5", "2020-01-01"),
    ])
    labels = [r.label for r in rows]
    assert labels.count(
        "High blood pressure (essential hypertension)") == 1, labels
    htn = next(r for r in rows if r.source_id in ("c1", "c2", "c3"))
    assert htn.source_id == "c2"
    assert htn.value == "Active since Jul 2023"
    assert "High cholesterol (hyperlipidemia)" in labels
    assert all(PLANTED not in r.label for r in rows)


def test_dedupe_keeps_a_different_clinical_status_apart():
    rows = build_problems([
        _condition("c1", ICD10, "I10", "2019-02-01", status="active"),
        _condition("c2", ICD10, "I10", "2023-07-15", status="recurrence"),
    ])
    assert len(rows) == 2


def test_uncoded_entries_are_not_merged():
    """Two entries with no code are not provably the same condition."""
    bare = {"resourceType": "Condition",
            "clinicalStatus": {"coding": [{"code": "active"}]}}
    rows = build_problems([dict(bare, id="u1"), dict(bare, id="u2")])
    assert [r.source_id for r in rows] == ["u1", "u2"]


# --- 2. encounter and screening codes are not conditions ---------------------

def test_encounter_and_screening_codes_are_not_current_conditions():
    """MUTATION: drop the encounter-concept filter -> both rows return."""
    rows = build_problems([
        _condition("z1", ICD10, "Z13.89", "2024-03-01"),   # screening
        _condition("z2", ICD10, "Z00.00", "2024-03-01"),   # adult exam
        _condition("s1", SCT, "268565007", "2024-03-01"),  # adult health exam
        _condition("s2", SCT, "185349003", "2024-03-01"),  # check up
        _condition("c1", ICD10, "I10", "2021-05-03"),
    ])
    assert [r.source_id for r in rows] == ["c1"]


def test_the_encounter_rule_does_not_catch_real_conditions():
    for system, code in ((ICD10, "Z14.1"), (ICD10, "Z79.4"), (ICD10, "E11.9"),
                         (SCT, "44054006")):
        assert not terminology.is_encounter_concept(system, code), code
    for system, code in ((ICD10, "Z00.00"), (ICD10, "Z13"), (ICD10, "Z1389"),
                         ("urn:oid:2.16.840.1.113883.6.90", "Z12.11"),
                         (SCT, "268565007")):
        assert terminology.is_encounter_concept(system, code), code


# --- 4. lab names and empty rows -------------------------------------------

def _obs(oid, code, value=None, unit=None, when="2026-09-01"):
    obs = {"resourceType": "Observation", "id": oid,
           "code": {"coding": [{"system": LOINC, "code": code,
                                "display": PLANTED}], "text": PLANTED},
           "effectiveDateTime": when}
    if value is not None:
        obs["valueQuantity"] = {"value": value, "unit": unit, "code": unit}
    return obs


def test_lab_rows_with_no_value_are_dropped():
    """MUTATION: restore RESULT_NOT_LISTED -> the empty row returns."""
    rows = build_labs([_obs("h", "8302-2"),
                       _obs("k", "2823-3", 4.1, "mmol/L")])
    assert [r.source_id for r in rows] == ["k"]
    assert all("not listed" not in r.value for r in rows)


def test_dropped_rows_do_not_take_the_ten_slots():
    empties = [_obs(f"e{i}", "8302-2", when="2026-10-01") for i in range(12)]
    rows = build_labs(empties + [_obs("k", "2823-3", 4.1, "mmol/L")])
    assert [r.source_id for r in rows] == ["k"]


def test_height_and_weight_rows_show_with_their_units():
    """A height or weight in a body unit is a value, not an empty row.
    MUTATION: drop the body units from _UNIT_DISPLAY -> both rows vanish."""
    rows = build_labs([
        _obs("h-cm", "8302-2", 172.5, "cm", when="2026-09-04"),
        _obs("h-in", "8302-2", 68, "[in_i]", when="2026-09-03"),
        _obs("w-kg", "29463-7", 81.2, "kg", when="2026-09-02"),
        _obs("w-lb", "29463-7", 179, "[lb_av]", when="2026-09-01"),
    ])
    values = {r.source_id: (r.label, r.value) for r in rows}
    assert values == {
        "h-cm": ("Body height", "172.5 cm (Sep 4, 2026)"),
        "h-in": ("Body height", "68 in (Sep 3, 2026)"),
        "w-kg": ("Body weight", "81.2 kg (Sep 2, 2026)"),
        "w-lb": ("Body weight", "179 lb (Sep 1, 2026)"),
    }


def test_a_body_measure_in_an_unknown_unit_or_free_text_still_never_shows():
    rows = build_labs([
        _obs("h", "8302-2", 172, "CM", when="2026-09-04"),     # exact only
        _obs("w", "29463-7", 81, f"kg {PLANTED}", when="2026-09-03"),
        dict(_obs("s", "29463-7", when="2026-09-02"),
             valueString=f"81 kg, {PLANTED}"),
    ])
    assert rows == []


def test_a_lab_reads_by_its_plain_name_not_loincs_long_name(monkeypatch):
    """Anion gap is in the table; a code only the resolver names gets
    LOINC's specimen and method qualifiers trimmed. MUTATION: label labs with
    terminology.lookup -> the long form comes back."""
    long_names = {
        "1863-0": "Anion gap 4 in Serum or Plasma",
        "99999-9": "Widget [Mass/volume] in Serum or Plasma by calculation",
    }
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda system, code: long_names.get(code))
    rows = build_labs([_obs("a", "33037-3", 9, "mmol/L"),
                       _obs("w", "99999-9", 3, "mg/dL", when="2026-08-01")])
    labels = {r.source_id: r.label for r in rows}
    assert labels["a"] == "Anion gap"
    assert labels["w"] == "Widget"
    assert all(PLANTED not in v for v in labels.values())


def test_plain_lab_name_keeps_a_specimen_that_changes_the_meaning():
    assert terminology.plain_lab_name(
        "Protein [Mass/volume] in Urine by Test strip") == "Protein in Urine"
    assert terminology.plain_lab_name(
        "Anion gap in Serum or Plasma by calculation") == "Anion gap"
    assert terminology.plain_lab_name(
        "Hemoglobin [Mass/volume] in Blood") == "Hemoglobin"


# --- 5. visit type -----------------------------------------------------------

def test_a_visit_is_named_by_its_type_or_class():
    """MUTATION: drop the class fallback -> "Visit (Oct 6, 2026)"."""
    encs = [
        {"resourceType": "Encounter", "id": "v1", "status": "finished",
         "class": {"system": ACT, "code": "AMB", "display": PLANTED},
         "type": [{"coding": [{"system": "urn:local", "code": "X1",
                               "display": PLANTED}], "text": PLANTED}],
         "period": {"start": "2026-10-06"}},
        {"resourceType": "Encounter", "id": "v2", "status": "finished",
         "class": {"system": ACT, "code": "EMER"},
         "period": {"start": "2026-09-01"}},
        {"resourceType": "Encounter", "id": "v3", "status": "finished",
         "type": [{"coding": [{"system": CPT, "code": "99214",
                               "display": PLANTED}]}],
         "period": {"start": "2026-08-01"}},
        # R5/R6 shape: class is a list of CodeableConcepts.
        {"resourceType": "Encounter", "id": "v4", "status": "finished",
         "class": [{"coding": [{"system": ACT, "code": "VR"}]}],
         "period": {"start": "2026-07-01"}},
    ]
    labels = {r.source_id: r.label for r in build_visits(encs)}
    assert labels["v1"] == "Office visit (Oct 6, 2026)"
    assert labels["v2"] == "Emergency visit (Sep 1, 2026)"
    assert labels["v3"] == "Office visit (Aug 1, 2026)"
    assert labels["v4"] == "Video visit (Jul 1, 2026)"
    assert all(PLANTED not in v for v in labels.values())


def test_a_visit_with_nothing_to_name_it_by_still_reads_visit():
    rows = build_visits([{"resourceType": "Encounter", "id": "v",
                          "period": {"start": "2026-10-06"}}])
    assert rows[0].label == "Visit (Oct 6, 2026)"


# --- 3. preventive care: the real cause -------------------------------------

def _store(app, resource, tenant_id):
    with app.app_context():
        db.session.add(R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource.get("id"),
            tenant_id=tenant_id))
        db.session.commit()


def _care_gaps(body):
    for ext in body.get("extension", []):
        if ext.get("url") == _SECTION + "care-gaps":
            return ext
    raise AssertionError("no care-gaps section")


def test_a_review_that_ran_shows_what_it_could_not_check(
        app, client, tenant_id, tenant_headers):
    """The live cause. A 56-year-old whose blood pressure, cholesterol and
    flu shot are all current has nothing DUE; colorectal cannot be decided
    because stool tests are not read. The review ran. The brief kept only
    `due` lines, so the section had no items and an `unavailable` status,
    and the page read that as "We couldn't check your screenings just now".
    The chat's tool read the same payload's `lines` and answered.

    MUTATION: keep only status == "due" lines in build_care_gaps -> the
    section is empty and the colorectal line is gone.
    """
    recent = (date.today() - timedelta(days=30)).isoformat()
    born = f"{date.today().year - 56}-01-01"
    pid = "p-handoff"
    subject = {"reference": f"Patient/{pid}"}
    _store(app, {"resourceType": "Patient", "id": pid, "birthDate": born,
                 "gender": "male"}, tenant_id)
    _store(app, {"resourceType": "Observation", "id": "bp", "status": "final",
                 "subject": subject, "effectiveDateTime": recent,
                 "code": {"coding": [{"system": LOINC, "code": "85354-9"}]}},
           tenant_id)
    _store(app, {"resourceType": "Observation", "id": "chol",
                 "status": "final", "subject": subject,
                 "effectiveDateTime": recent,
                 "code": {"coding": [{"system": LOINC, "code": "2093-3"}]},
                 "valueQuantity": {"value": 180, "unit": "mg/dL"}}, tenant_id)
    _store(app, {"resourceType": "Immunization", "id": "flu",
                 "status": "completed", "patient": subject,
                 "occurrenceDateTime": recent,
                 "vaccineCode": {"coding": [{
                     "system": "http://hl7.org/fhir/sid/cvx", "code": "140"}]}},
           tenant_id)

    # The chat's tool and the brief read one review.
    gaps = client.get("/r6/fhir/Patient/$care-gaps", headers=tenant_headers)
    assert gaps.status_code == 200
    consumer = json.loads(next(
        p["valueString"] for p in gaps.get_json()["parameter"]
        if p["name"] == "consumerSummary"))
    assert not [ln for ln in consumer["lines"] if ln["status"] == "due"]
    assert any(ln["status"] == "indeterminate" for ln in consumer["lines"])

    r = client.get(_URL, headers=tenant_headers)
    assert r.status_code == 200
    section = _care_gaps(r.get_json())
    fields = [json.loads(e["valueString"]) for e in section["extension"]
              if e["url"] == "field"]
    status = next(e["valueString"] for e in section["extension"]
                  if e["url"] == "status")
    # Still not "ok": one screening was not decided, and that is said.
    assert status == CARE_GAPS_UNAVAILABLE
    assert [f["label"] for f in fields] == ["Colorectal cancer screening"]
    assert fields[0]["value"].startswith("We could not check whether you")
