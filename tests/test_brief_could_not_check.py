"""An undecided screening reaches the brief as a line, not as silence (#436).

#563 gave `$care-gaps` a patient line for every screening the person is
eligible for and the rules could not decide, labelled "could not check". Two
layers downstream still threw it away:

- `r6/brief/engine.py::build_care_gaps` kept `status == "due"` only, so the
  producer's could-not-check line never became a brief field.
- `careagents/templates/brief.html` rendered fields only when the section
  status was "ok". Any undecided screening sets `unevaluated`, which makes the
  section unavailable, so the page showed the banner and dropped every field,
  including the due ones.

Built from the real evaluator and producer, not a hand-written payload, so a
drift in either shows up here. Synthetic patient only.
"""

import json

from r6.brief.engine import CARE_GAPS_UNAVAILABLE, build_care_gaps
from r6.caregaps.evaluate import evaluate_care_gaps
from r6.caregaps.report import build_consumer_summary
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)

_AS_OF = "2026-09-23"
# 56, female, nothing on file: colorectal is eligible and undecided (stool
# tests are not read), the others are due.
_PATIENT = {"resourceType": "Patient", "id": "syn-1",
            "birthDate": "1970-06-01", "gender": "female"}


def _consumer(patient=_PATIENT):
    return build_consumer_summary(evaluate_care_gaps(patient, as_of=_AS_OF))


def _line(consumer, rule_id):
    return next(ln for ln in consumer["lines"] if ln["rule_id"] == rule_id)


def test_an_undecided_eligible_screening_becomes_a_brief_field():
    """MUTATION: restore `status != "due"` in build_care_gaps -> no field."""
    consumer = _consumer()
    crc = _line(consumer, "colorectal-screening")
    assert crc["status"] == "indeterminate", "producer changed; test is stale"

    section = build_care_gaps({"consumer": consumer})
    by_id = {f.source_id: f for f in section.fields}
    assert "colorectal-screening" in by_id
    field = by_id["colorectal-screening"]
    # The producer's own sentence, written once for every surface.
    assert field.value == crc["message"]
    # Never presented as due: the label says it was not decided, and the
    # sentence says we could not check rather than "you may be due".
    assert "could not check" in field.label
    assert "may be due" not in field.value.lower()


def test_the_due_screenings_travel_with_it_and_the_answer_stays_partial():
    consumer = _consumer()
    section = build_care_gaps({"consumer": consumer})
    due_ids = {ln["rule_id"] for ln in consumer["lines"]
               if ln["status"] == "due"}
    assert due_ids, "fixture should have due screenings"
    assert due_ids <= {f.source_id for f in section.fields}
    # A partial answer is still not a whole one.
    assert section.status == CARE_GAPS_UNAVAILABLE
    assert section.reason == consumer["unevaluated_note"]


def test_every_line_the_producer_does_not_call_up_to_date_reaches_the_brief():
    """The property #436 asks for, one layer up: nothing the patient was given
    a line for is dropped in silence, except up-to-date, which is not a gap."""
    consumer = _consumer()
    section = build_care_gaps({"consumer": consumer})
    expected = {ln["rule_id"] for ln in consumer["lines"]
                if ln["status"] != "up_to_date"}
    assert {f.source_id for f in section.fields} == expected


def test_no_field_is_invented_for_a_screening_whose_eligibility_is_unknown():
    """Sex not on file: cervical and mammography cannot say whether they apply.
    The producer gives them no line, and the brief must not make one up."""
    consumer = _consumer({**_PATIENT, "gender": None})
    section = build_care_gaps({"consumer": consumer})
    ids = {f.source_id for f in section.fields}
    assert "cervical-screening" not in ids
    assert "mammography" not in ids
    assert "Cervical" in section.reason  # named in the marker instead


# --- CareAgents renders what the brief carries -------------------------------

_CARE_GAPS_SECTION = ("https://healthclaw.io/fhir/StructureDefinition/"
                      "brief-section-care-gaps")


def _brief(status, fields):
    return {"resourceType": "Basic", "extension": [{
        "url": _CARE_GAPS_SECTION,
        "extension": ([{"url": "field", "valueString": json.dumps(f)}
                       for f in fields]
                      + [{"url": "status", "valueString": status}])}]}


def test_careagents_shows_the_fields_of_a_partial_review(  # noqa: F811
        app, svc, monkeypatch):  # noqa: F811
    """MUTATION: gate the field list on care_gaps_ok again -> the could-not-
    check line and the due line both vanish behind the banner."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = c.post("/api/connections/sample").get_json()["id"]
    agent_id = c.post("/api/agents", json={
        "name": "Ada", "persona": "direct",
        "connection_id": conn_id}).get_json()["id"]

    section = build_care_gaps({"consumer": _consumer()})
    fields = [{"label": f.label, "value": f.value,
               "sourceType": f.source_type, "sourceId": f.source_id}
              for f in section.fields]
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant: _brief(section.status, fields))

    body = c.get(f"/brief?agent={agent_id}").get_data(as_text=True)
    assert "Colorectal cancer screening (could not check)" in body
    assert "Influenza (flu) vaccine" in body
    # Still says the review is not whole, and never that nothing is due.
    assert "Screening review incomplete" in body
    assert "no preventive care items" not in body
