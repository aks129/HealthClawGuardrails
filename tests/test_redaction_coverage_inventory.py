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
They are measured in `tests/test_redaction_probes_multistep.py`, which names
its own uncovered sites in turn. `Questionnaire/$populate` is measured in
`tests/test_sdc_populate_bounded.py`.

The rows at the end of the file come from a later sweep of every read route
(2026-09-23), which found two leaks: `DiagnosticReport.conclusion` on the
standard read path, and `$curatr-evaluate` quoting the stored display.
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


# ---------------------------------------------------------------------------
# Re-measured 2026-09-23 on main after #725/#732, by driving every read route
# with a record whose name, text, display, note and narrative each carried a
# marker. Two leaks came back that neither the code-reading inventory nor the
# rows above had found; these rows pin both closed.
# ---------------------------------------------------------------------------

CONCLUSION_MARKER = "PHICONCLUSIONMARKER"
CURATR_DISPLAY_MARKER = "PHICURATRDISPLAYMARKER"
SNOMED = "http://snomed.info/sct"
HYPERTENSION = "38341003"
CANONICAL = "Hypertensive disorder"


def _store(resource, tenant_id):
    db.session.add(R6Resource(
        resource_type=resource["resourceType"],
        resource_json=json.dumps(resource),
        resource_id=resource["id"],
        tenant_id=tenant_id))
    db.session.commit()


@pytest.mark.parametrize("path", [
    "/r6/fhir/DiagnosticReport/redaction-probe-dr",
    "/r6/fhir/DiagnosticReport",
])
def test_diagnostic_report_conclusion_is_redacted(client, tenant_id,
                                                  tenant_headers, path):
    """`DiagnosticReport.conclusion` is the clinician's free-text reading of
    the report, the same kind of field as `note`. apply_redaction stripped
    `note` and `comment` but not `conclusion`, so the standard read path
    returned it verbatim. That was a hole in the profile, not a route that
    skipped it, which is why only a probe of the response could find it."""
    _store({
        "resourceType": "DiagnosticReport", "id": "redaction-probe-dr",
        "status": "final",
        "code": {"coding": [{"system": LOINC, "code": "24331-1"}]},
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
        "conclusion": CONCLUSION_MARKER,
    }, tenant_id)

    response = client.get(path, headers=tenant_headers)
    body = response.get_data(as_text=True)
    assert response.status_code == 200, body[:200]
    assert "redaction-probe-dr" in body, (
        "the report never came back, so the assertion below measures "
        "nothing: " + body[:200])
    assert CONCLUSION_MARKER not in body, (
        "DiagnosticReport.conclusion reached the caller unredacted")


def test_curatr_evaluate_does_not_quote_the_upstream_display(
        client, tenant_id, tenant_headers):
    """`$curatr-evaluate` reported a display mismatch by quoting the stored
    `coding.display` back ("The description says '<display>' ..."). That is
    the upstream display reaching the caller, and the realistic caller is the
    `curatr_evaluate` MCP tool, so it landed in a model's context.

    The terminology lookup is patched so the mismatch branch fires without
    the network. The issue must still be raised and still carry the
    canonical label, which comes from the terminology service keyed by code:
    an evaluator that stopped reporting the mismatch would also pass the
    marker assertion, so that is checked first.
    """
    from unittest.mock import patch

    _store({
        "resourceType": "Condition", "id": "redaction-probe-cond",
        "code": {"coding": [{"system": SNOMED, "code": HYPERTENSION,
                             "display": CURATR_DISPLAY_MARKER}]},
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
    }, tenant_id)

    with patch("r6.curatr.CuratrEngine._lookup_code",
               return_value={"valid": True, "display": CANONICAL,
                             "message": None}):
        response = client.get(
            "/r6/fhir/Condition/redaction-probe-cond/$curatr-evaluate",
            headers=tenant_headers)
    body = response.get_data(as_text=True)
    assert response.status_code == 200, body[:200]

    mismatch = [i for i in response.get_json()["issues"]
                if i["field_path"].endswith(".display")]
    assert len(mismatch) == 1, (
        "the display-mismatch issue was not raised, so the marker assertion "
        "below would pass without measuring anything: " + body[:400])
    assert mismatch[0]["suggested_value"] == {"display": CANONICAL}
    assert CANONICAL in mismatch[0]["plain_language"]

    assert CURATR_DISPLAY_MARKER not in body, (
        "$curatr-evaluate quoted the stored coding.display back to the caller")


# Free-text sweep, 2026-09-23 (#282). Every `string`/`markdown` element of
# every type in `R6Resource.SUPPORTED_TYPES` was taken from the published
# StructureDefinitions, given its own marker, stored, and read back through
# `GET /<type>/<id>` and `GET /<type>`. The profile strips by field name, so
# a free-text string whose name was not on a list came back verbatim. These
# rows are the survivors that are clinician or feed free text; the ones kept
# (definitional titles, units, versions, lot numbers, linkIds) are argued in
# the PR that added them.
# ---------------------------------------------------------------------------

FREE_TEXT_MARKER = "PHIFREETEXTMARKER"
SUBJECT = {"reference": f"Patient/{PATIENT_ID}"}
FMH_CODE = {"coding": [{"system": "http://snomed.info/sct",
                        "code": "38341003"}]}


@pytest.mark.parametrize("field, resource", [
    ("MedicationRequest.dosageInstruction.patientInstruction", {
        "resourceType": "MedicationRequest", "status": "active",
        "intent": "order", "subject": SUBJECT,
        "dosageInstruction": [{"patientInstruction": FREE_TEXT_MARKER}]}),
    ("MedicationDispense.dosageInstruction.patientInstruction", {
        "resourceType": "MedicationDispense", "status": "completed",
        "subject": SUBJECT,
        "dosageInstruction": [{"patientInstruction": FREE_TEXT_MARKER}]}),
    ("ServiceRequest.patientInstruction", {
        "resourceType": "ServiceRequest", "status": "active",
        "intent": "order", "subject": SUBJECT,
        "patientInstruction": FREE_TEXT_MARKER}),
    ("Condition.onsetString", {
        "resourceType": "Condition", "subject": SUBJECT,
        "onsetString": FREE_TEXT_MARKER}),
    ("Condition.abatementString", {
        "resourceType": "Condition", "subject": SUBJECT,
        "abatementString": FREE_TEXT_MARKER}),
    ("AllergyIntolerance.onsetString", {
        "resourceType": "AllergyIntolerance", "patient": SUBJECT,
        "onsetString": FREE_TEXT_MARKER}),
    ("FamilyMemberHistory.condition.onsetString", {
        "resourceType": "FamilyMemberHistory", "status": "completed",
        "patient": SUBJECT,
        "condition": [{"code": FMH_CODE, "onsetString": FREE_TEXT_MARKER}]}),
    ("FamilyMemberHistory.ageString", {
        "resourceType": "FamilyMemberHistory", "status": "completed",
        "patient": SUBJECT, "ageString": FREE_TEXT_MARKER}),
    ("FamilyMemberHistory.bornString", {
        "resourceType": "FamilyMemberHistory", "status": "completed",
        "patient": SUBJECT, "bornString": FREE_TEXT_MARKER}),
    ("FamilyMemberHistory.deceasedString", {
        "resourceType": "FamilyMemberHistory", "status": "completed",
        "patient": SUBJECT, "deceasedString": FREE_TEXT_MARKER}),
    ("Immunization.occurrenceString", {
        "resourceType": "Immunization", "status": "completed",
        "patient": SUBJECT, "occurrenceString": FREE_TEXT_MARKER}),
    ("Procedure.performedString", {
        "resourceType": "Procedure", "status": "completed",
        "subject": SUBJECT, "performedString": FREE_TEXT_MARKER}),
    ("CarePlan.activity.detail.scheduledString", {
        "resourceType": "CarePlan", "status": "active", "intent": "plan",
        "subject": SUBJECT,
        "activity": [{"detail": {"status": "scheduled",
                                 "scheduledString": FREE_TEXT_MARKER}}]}),
    # Annotation.authorString is the author's NAME, in an Annotation that is
    # not under `note`, so the note replacement never reached it.
    ("CarePlan.activity.progress.authorString", {
        "resourceType": "CarePlan", "status": "active", "intent": "plan",
        "subject": SUBJECT,
        "activity": [{"progress": [{"authorString": FREE_TEXT_MARKER,
                                    "text": "progress"}]}]}),
    ("CarePlan.title", {
        "resourceType": "CarePlan", "status": "active", "intent": "plan",
        "subject": SUBJECT, "title": FREE_TEXT_MARKER}),
    ("Goal.statusReason", {
        "resourceType": "Goal", "lifecycleStatus": "cancelled",
        "subject": SUBJECT, "description": {"text": "goal"},
        "statusReason": FREE_TEXT_MARKER}),
    ("Goal.target.detailString", {
        "resourceType": "Goal", "lifecycleStatus": "active",
        "subject": SUBJECT, "description": {"text": "goal"},
        "target": [{"detailString": FREE_TEXT_MARKER}]}),
])
def test_free_text_string_is_redacted(client, tenant_id, tenant_headers,
                                      field, resource):
    """Each row is a free-text string the sweep found on the standard read
    path. Both the read and the search must bring the record back, or the
    marker assertion measures nothing."""
    rid = "redaction-probe-" + field.replace(".", "-").lower()
    db.session.add(R6Resource(
        resource_type=resource["resourceType"],
        resource_json=json.dumps({**resource, "id": rid}),
        resource_id=rid, tenant_id=tenant_id))
    db.session.commit()
    rtype = resource["resourceType"]

    for path in (f"/r6/fhir/{rtype}/{rid}", f"/r6/fhir/{rtype}"):
        response = client.get(path, headers=tenant_headers)
        body = response.get_data(as_text=True)
        assert response.status_code == 200, body[:200]
        assert rid in body, (
            f"{path} never returned the record, so the assertion below "
            "measures nothing: " + body[:200])
        assert FREE_TEXT_MARKER not in body, (
            f"{field} reached the caller unredacted via {path}")


# Identifier, Address and Attachment sweep, 2026-09-23 (#282). The profile
# found an Identifier only under the key `identifier`, an Address only in a
# list, and an Attachment only when it carried `contentType` (optional in
# FHIR). Each row plants a marker in one element of that kind that sat
# somewhere else and reads it back through `GET /<type>/<id>` and
# `GET /<type>`.
# ---------------------------------------------------------------------------

VALUE_MARKER = "PHIVALUEMARKER"


def _ident(system="urn:probe"):
    return {"system": system, "value": VALUE_MARKER}


@pytest.mark.parametrize("field, resource", [
    ("Specimen.accessionIdentifier", {
        "resourceType": "Specimen", "subject": SUBJECT,
        "accessionIdentifier": _ident()}),
    ("DocumentReference.masterIdentifier", {
        "resourceType": "DocumentReference", "status": "current",
        "subject": SUBJECT, "masterIdentifier": _ident()}),
    ("MedicationRequest.groupIdentifier", {
        "resourceType": "MedicationRequest", "status": "active",
        "intent": "order", "subject": SUBJECT,
        "groupIdentifier": _ident()}),
    ("ServiceRequest.requisition", {
        "resourceType": "ServiceRequest", "status": "active",
        "intent": "order", "subject": SUBJECT, "requisition": _ident()}),
    ("Encounter.hospitalization.preAdmissionIdentifier", {
        "resourceType": "Encounter", "status": "finished",
        "subject": SUBJECT,
        "hospitalization": {"preAdmissionIdentifier": _ident()}}),
    ("Patient.extension.valueIdentifier", {
        "resourceType": "Patient",
        "extension": [{"url": "http://example.org/fhir/ssn",
                       "valueIdentifier": _ident()}]}),
    # Coverage carries the member id as a plain string in R4 and as an
    # Identifier from R5 on. Both shapes are rows.
    ("Coverage.subscriberId", {
        "resourceType": "Coverage", "status": "active",
        "beneficiary": SUBJECT, "subscriberId": VALUE_MARKER}),
    ("Coverage.subscriberId (Identifier)", {
        "resourceType": "Coverage", "status": "active",
        "beneficiary": SUBJECT, "subscriberId": [_ident()]}),
    ("Coverage.dependent", {
        "resourceType": "Coverage", "status": "active",
        "beneficiary": SUBJECT, "dependent": VALUE_MARKER}),
    ("Coverage.class.value", {
        "resourceType": "Coverage", "status": "active",
        "beneficiary": SUBJECT,
        "class": [{"type": {"coding": [{"code": "group"}]},
                   "value": VALUE_MARKER}]}),
    ("Coverage.class.value (Identifier)", {
        "resourceType": "Coverage", "status": "active",
        "beneficiary": SUBJECT,
        "class": [{"type": {"coding": [{"code": "group"}]},
                   "value": _ident()}]}),
    ("Location.address", {
        "resourceType": "Location", "status": "active",
        "address": {"line": [VALUE_MARKER], "city": VALUE_MARKER + "c",
                    "district": VALUE_MARKER + "d",
                    "postalCode": VALUE_MARKER + "p",
                    "text": VALUE_MARKER + "t", "state": "MA"}}),
    # An Attachment without contentType: the #780 shape, then one with only
    # a link and a title, then one known by its size and hash.
    ("Attachment.data without contentType", {
        "resourceType": "DocumentReference", "status": "current",
        "subject": SUBJECT,
        "content": [{"attachment": {"data": VALUE_MARKER,
                                    "title": VALUE_MARKER + "title"}}]}),
    ("Attachment.url with title", {
        "resourceType": "DocumentReference", "status": "current",
        "subject": SUBJECT,
        "content": [{"attachment": {
            "url": "https://files.example/" + VALUE_MARKER,
            "title": VALUE_MARKER + "title"}}]}),
    ("Attachment.url with size and hash", {
        "resourceType": "DocumentReference", "status": "current",
        "subject": SUBJECT,
        "content": [{"attachment": {
            "url": "https://files.example/" + VALUE_MARKER,
            "size": 1024, "hash": "2jmj7l5rSw0yVb/vlWAYkK/YBwk="}}]}),
    ("Subscription.contact.value", {
        "resourceType": "Subscription", "status": "active",
        "reason": "probe", "criteria": "Observation?code=2093-3",
        "channel": {"type": "rest-hook"},
        "contact": [{"system": "email",
                     "value": VALUE_MARKER + "@example.org"}]}),
])
def test_identifier_address_and_attachment_values_are_redacted(
        client, tenant_id, tenant_headers, field, resource):
    """Each row is an element of a PHI-bearing datatype that the standard
    read path returned verbatim because it sat under an unexpected key or
    in an unexpected shape. Both the read and the search must bring the
    record back, or the marker assertion measures nothing."""
    rid = "redaction-probe-" + "".join(
        ch if ch.isalnum() else "-" for ch in field).lower()
    db.session.add(R6Resource(
        resource_type=resource["resourceType"],
        resource_json=json.dumps({**resource, "id": rid}),
        resource_id=rid, tenant_id=tenant_id))
    db.session.commit()
    rtype = resource["resourceType"]

    for path in (f"/r6/fhir/{rtype}/{rid}", f"/r6/fhir/{rtype}"):
        response = client.get(path, headers=tenant_headers)
        body = response.get_data(as_text=True)
        assert response.status_code == 200, body[:200]
        assert rid in body, (
            f"{path} never returned the record, so the assertion below "
            "measures nothing: " + body[:200])
        assert VALUE_MARKER not in body, (
            f"{field} reached the caller unredacted via {path}")
        if rtype == "Location":
            entries = ([response.get_json()] if path.endswith(rid) else
                       [e["resource"] for e in response.get_json()["entry"]])
            address = next(e for e in entries if e["id"] == rid)["address"]
            assert address == {"state": "MA"}, (
                "Location.address should keep only its coarse state; "
                f"got {address}")


def test_stored_audit_event_agent_identity_is_redacted(
        client, tenant_id, tenant_headers):
    """A stored AuditEvent (an ingested or upstream one; ours are served from
    AuditEventRecord) names its agent by `altId` and by network address.
    Both identify a person or their machine. Search delegates to the audit
    table, so only the read reaches the stored row."""
    rid = "redaction-probe-auditevent"
    db.session.add(R6Resource(
        resource_type="AuditEvent",
        resource_json=json.dumps({
            "resourceType": "AuditEvent", "id": rid,
            "type": {"code": "rest"}, "recorded": "2026-01-15T00:00:00Z",
            "agent": [{"requestor": True, "altId": VALUE_MARKER + "alt",
                       "network": {"address": "10.0.0.1" + VALUE_MARKER,
                                   "type": "2"}},
                      {"requestor": False,
                       "networkString": VALUE_MARKER + "host"},
                      {"requestor": False,
                       "networkUri": "https://" + VALUE_MARKER + ".example"}],
            "source": {"observer": {"reference": "Device/probe"}}}),
        resource_id=rid, tenant_id=tenant_id))
    db.session.commit()

    response = client.get(f"/r6/fhir/AuditEvent/{rid}",
                          headers=tenant_headers)
    body = response.get_data(as_text=True)
    assert response.status_code == 200, body[:200]
    assert rid in body, body[:200]
    assert VALUE_MARKER not in body, (
        "AuditEvent.agent altId/network reached the caller unredacted")


def test_shapes_the_product_needs_are_not_mistaken_for_phi():
    """The negative control for the rows above. Each rule matches a shape,
    so each has a neighbour it must not match: an Extension has a `url`, a
    SampledData has `data`, a Reference has `reference`, a Quantity has a
    `value`, a Coding has a `system`."""
    from r6.redaction import apply_redaction

    out = apply_redaction({
        "resourceType": "Observation", "id": "keep-me", "status": "final",
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
        "code": {"coding": [{"system": LOINC, "code": TOTAL_CHOL}]},
        "extension": [{"url": "http://example.org/fhir/flag",
                       "valueBoolean": True}],
        "valueQuantity": {"value": 244, "unit": "mg/dL",
                          "system": "http://unitsofmeasure.org",
                          "code": "mg/dL"},
        "component": [{"code": {"coding": [{"code": "x"}]},
                       "valueSampledData": {
                           "origin": {"value": 0}, "period": 10,
                           "dimensions": 1, "data": "1 2 3"}}],
    })
    assert out["id"] == "keep-me"
    assert out["subject"]["reference"] == f"Patient/{PATIENT_ID}"
    assert out["extension"][0]["url"] == "http://example.org/fhir/flag"
    assert out["valueQuantity"]["value"] == 244
    assert out["code"]["coding"][0]["code"] == TOTAL_CHOL
    assert out["component"][0]["valueSampledData"]["data"] == "1 2 3"
