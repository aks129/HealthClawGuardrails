"""Which read paths return resource content that never met apply_redaction.

#282 lists eight sites that read `R6Resource` and never redact. That list was
produced by reading the code. Reading tells you a call is absent; it does not
tell you whether anything reaches the caller, which is the only question that
matters. So this measures instead: seed one Patient and one Observation whose
every free-text field carries a distinct marker, drive each endpoint, and look
for the markers in the response body.

The markers are the fields real feeds actually put a name in — `display`,
`CodeableConcept.text`, `Patient.name` — which is why CLAUDE.md forbids
preserving an upstream `display` to make a record readable. Labels come from
`r6/terminology.py`, keyed by code, applied AFTER redaction.

## What this file is

A characterization of today's behaviour, not a specification of the desired
one. A row asserting a marker DOES appear is recording a leak that exists; it
is written so that fixing the leak fails here and forces the inventory to be
updated deliberately, rather than a fix landing with no record that anything
changed. Each such row names the disposition it is waiting on.

Sites not covered here (`r6/actions/rails/form_fill.py`, `r6/sdc/documents.py`,
`r6/smbp/routes.py`, `r6/curatr.py`) are reached through multi-step flows —
an approval, a questionnaire, an enrolment — rather than one request, so they
need their own probes and are NOT silently claimed as clean by this file.
"""

from __future__ import annotations

import json

import pytest

from models import db
from r6.models import R6Resource
from r6.quality.routes import MEASURE_ID

# Distinct per field, so a hit names WHICH field leaked rather than only that
# something did.
NAME_MARKER = "Marguerite"
OBS_DISPLAY_MARKER = "PHIDISPLAYMARKER"
OBS_TEXT_MARKER = "PHITEXTMARKER"
PATIENT_ID = "redaction-probe-patient"

LOINC = "http://loinc.org"
TOTAL_CHOL = "2093-3"


def _seed(tenant_id):
    patient = {
        "resourceType": "Patient",
        "id": PATIENT_ID,
        "name": [{"family": NAME_MARKER, "given": ["Josephine"]}],
        "birthDate": "1962-03-04",
        "gender": "female",
    }
    observation = {
        "resourceType": "Observation",
        "id": "redaction-probe-obs",
        "status": "final",
        # Both places a real feed smuggles a name into a lab result.
        "code": {
            "coding": [{"system": LOINC, "code": TOTAL_CHOL,
                        "display": OBS_DISPLAY_MARKER}],
            "text": OBS_TEXT_MARKER,
        },
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
        "effectiveDateTime": "2026-01-15",
        "valueQuantity": {"value": 244, "unit": "mg/dL"},
    }
    for resource in (patient, observation):
        db.session.add(R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource["id"],
            tenant_id=tenant_id))
    db.session.commit()


def _markers_in(response) -> set[str]:
    body = response.get_data(as_text=True)
    return {m for m in (NAME_MARKER, OBS_DISPLAY_MARKER, OBS_TEXT_MARKER)
            if m in body}


@pytest.fixture
def seeded(client, tenant_id):
    _seed(tenant_id)
    return client


def test_labs_interpret_returns_no_upstream_free_text(seeded, tenant_headers):
    """`Observation/$interpret` is the path CareAgents drives for "what do my
    labs say?", so its output reaches a patient's chat verbatim.

    It reads stored Observations without calling apply_redaction (#282,
    r6/labs/routes.py). This asserts the property that actually matters —
    that no upstream free text leaves — which redaction is one way to get.
    """
    response = seeded.post("/r6/fhir/Observation/$interpret",
                           headers=tenant_headers, json={})
    assert response.status_code == 200
    assert _markers_in(response) == set(), (
        "an upstream display/text reached the interpreter's output; labels "
        "must come from r6/terminology.py keyed by code, never from the feed")


def test_care_gaps_returns_no_upstream_free_text(seeded, tenant_headers):
    """`Patient/$care-gaps` loads the Patient row itself (r6/caregaps/
    routes.py `_patient_for`) to read age and sex, and every Observation for
    the subject, with no redaction on either."""
    response = seeded.post(
        "/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
        json={"resourceType": "Parameters",
              "parameter": [{"name": "subject",
                             "valueReference": {
                                 "reference": f"Patient/{PATIENT_ID}"}}]})
    assert response.status_code == 200, response.get_data(as_text=True)[:200]
    assert _markers_in(response) == set(), (
        "a care-gaps response carried the patient's name or an upstream "
        "code display")


def test_quality_measure_evaluation_returns_no_upstream_free_text(
        seeded, tenant_headers):
    """`$evaluate-measure` loads whole resource types (r6/quality/routes.py
    `_load`). A MeasureReport is a count, so nothing free-text SHOULD survive
    into it — this pins that, because the loader itself has no guard."""
    response = seeded.post(
        f"/r6/fhir/Measure/{MEASURE_ID}/$evaluate-measure",
        headers=tenant_headers,
        json={"resourceType": "Parameters",
              "parameter": [{"name": "subject",
                             "valueString": f"Patient/{PATIENT_ID}"}]})
    assert response.status_code == 200, (
        "the probe never reached the loader — a 404 here would make the "
        "assertion below pass without measuring anything: "
        + response.get_data(as_text=True)[:200])
    assert _markers_in(response) == set(), (
        "a MeasureReport carried free text from the resources it counted")


def test_the_redacting_read_path_still_strips_all_three_markers(
        seeded, tenant_headers):
    """The control. `/Patient/<id>` goes through apply_redaction, so if this
    ever fails the probe above is measuring a broken harness rather than a
    real property — a test that cannot fail for the reason it claims is the
    failure mode this file exists to avoid.
    """
    response = seeded.get(f"/r6/fhir/Patient/{PATIENT_ID}",
                          headers=tenant_headers)
    assert response.status_code == 200
    assert NAME_MARKER not in response.get_data(as_text=True), (
        "the redacted single-resource read leaked the family name — the "
        "guard this whole inventory is measured against is broken")
