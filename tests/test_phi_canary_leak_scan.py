"""A canary PHI scan over every read-shaped surface an agent can reach.

`tests/test_redaction_coverage_inventory.py` and
`tests/test_redaction_probes_multistep.py` measure named fields on named
routes. Both are lists somebody wrote, so a new tool that reaches the store
by a route nobody listed is covered by neither. This file starts from the
other end: the MCP tool catalogue, `adapters/tools.manifest.json` (generated
from `services/agent-orchestrator/src/tools.ts`, pinned by `manifest.test.ts`).
Every tool in it is classed below as READ (scanned), WRITE (excluded, it is
behind step-up and the rail) or OUT_OF_SCOPE (with the reason), and
`test_every_manifest_tool_is_classified` fails when the catalogue and this
table disagree, so the scanned list cannot shrink without a diff here.

## Method

One synthetic Bundle is ingested through `POST /Bundle/$ingest-context`, the
same write boundary an agent's upload takes. Each place a real feed puts a
patient's name or a clinician's words carries its own canary token: names,
identifiers, address, telecom, narrative, every `display`, every
`CodeableConcept.text`, notes, `valueString`, `DiagnosticReport.conclusion`,
attachments, Coverage member numbers, and the free-text strings the #282
sweep found. Each READ tool's Flask call is then made and the body searched
for every canary. Every canary shares a prefix, so a canary that arrives cut
short still matches.

Each probe asserts that it reached data (the record id, a count above zero,
a populated answer) before it asserts the absence of canaries. A 404 would
otherwise pass the scan while measuring nothing.

## What a surface may keep

Two surfaces keep identity fields on purpose, and only those canaries are
excused, only on that surface:

- `questionnaire_populate`: the `%patient` projection keeps name given and
  family, birthDate, telecom phone/email and address line/city/postalCode
  (council ruling D10; `r6/sdc/expressions.py::_PROJECTED_*`;
  `tests/test_sdc_populate_bounded.py::PERMITTED`). `name.text` and
  `identifier.value` are withheld there, and are scanned.
- `shl_generate` defaults to the `intake` profile of `$share-bundle`, an
  identified export the patient sends to a clinic
  (`r6/routes.py::_intake_strip`; `tests/test_share_bundle.py`, which pins
  that the name survives). It strips only top-level `note`, the narrative
  and SSN-class identifiers, so those are the canaries scanned there. The
  `deidentified` profile keeps top-level `birthDate` verbatim
  (`apply_patient_controlled_redaction`, #617) and nothing else.

## What it found (2026-09-24)

One leak. `$compiled-truth`, the call behind `fhir_compiled_truth`, returns
the current resource redacted but builds its evidence timeline from the raw
Provenance rows. Four fields arrive verbatim: `agent[].who.display`,
`reason[0].coding[0].display`, and the curatr-correction extension's
`change_summary` and `patient_intent`. `GET /Provenance/<id>` strips all
four, which the `fhir_read` row here confirms. The two `display` fields are
upstream displays, which CLAUDE.md forbids passing through. The two
curatr-correction strings are written by the curatr flow and shown on the
compiled-truth MCP App on purpose, so whether they are contract-kept or
redacted needs a ruling; this file does not decide it. Each is a strict
xfail row at the end of the file.

## Not covered

- The TypeScript layer. `search` and `fetch` build titles with
  `summarizeResource`, and several tools add an `_mcp_summary`. Both are
  built from the Flask body scanned here, so they can only repeat what that
  body holds, but no MCP process runs in this file.
- The four `mcp-apps/*` pages the tools link as `_meta.ui.resourceUri`
  are HTML shells that render the tenant id and fetch through the
  endpoints scanned here.
- `action_status` reads an action row, which exists only after a write-tier
  tool (`action_propose`, `rx_transfer_request`) created it. The rx-transfer
  script is assembled from the patient's medications, so it is a real read
  vector, and it is not scanned here.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from r6.stepup import generate_step_up_token

MANIFEST = Path(__file__).resolve().parent.parent / "adapters" / "tools.manifest.json"

PREFIX = "ZQCNRY"

# One token per field, so a hit names the field. None is a substring of
# another. The names read as `<resource><field>`.
FAMILY = PREFIX + "FAMILY"
GIVEN = PREFIX + "GIVEN"
NAME_TEXT = PREFIX + "NAMETEXT"
MRN = PREFIX + "MRNVALUE"
SSN = PREFIX + "SSNVALUE"
PHONE = PREFIX + "PHONE"
EMAIL = PREFIX + "EMAIL@example.org"
ADDR_LINE = PREFIX + "ADDRLINE"
ADDR_CITY = PREFIX + "ADDRCITY"
ADDR_DISTRICT = PREFIX + "ADDRDISTRICT"
ADDR_POSTAL = PREFIX + "ADDRPOSTAL"
ADDR_TEXT = PREFIX + "ADDRTEXT"
NARRATIVE = PREFIX + "NARRATIVE"
CONTACT_FAMILY = PREFIX + "CONTACTFAMILY"
CONTACT_PHONE = PREFIX + "CONTACTPHONE"
CONTACT_LINE = PREFIX + "CONTACTLINE"
GP_DISPLAY = PREFIX + "GPDISPLAY"
MARITAL_TEXT = PREFIX + "MARITALTEXT"
EXT_STRING = PREFIX + "EXTSTRING"
SUBJECT_DISPLAY = PREFIX + "SUBJECTDISPLAY"
OBS_DISPLAY = PREFIX + "OBSDISPLAY"
OBS_TEXT = PREFIX + "OBSTEXT"
OBS_NOTE = PREFIX + "OBSNOTE"
OBS_PERFORMER = PREFIX + "OBSPERFORMER"
OBS_INTERP_TEXT = PREFIX + "OBSINTERPTEXT"
OBS_VALUE_STRING = PREFIX + "OBSVALUESTRING"
COND_DISPLAY = PREFIX + "CONDDISPLAY"
COND_TEXT = PREFIX + "CONDTEXT"
COND_NOTE = PREFIX + "CONDNOTE"
COND_ONSET = PREFIX + "CONDONSET"
COND_RECORDER = PREFIX + "CONDRECORDER"
MED_DISPLAY = PREFIX + "MEDDISPLAY"
MED_TEXT = PREFIX + "MEDTEXT"
DOSE_TEXT = PREFIX + "DOSETEXT"
PATIENT_INSTRUCTION = PREFIX + "PATIENTINSTRUCTION"
MED_REQUESTER = PREFIX + "MEDREQUESTER"
MED_NOTE = PREFIX + "MEDNOTE"
ALLERGY_DISPLAY = PREFIX + "ALLERGYDISPLAY"
ALLERGY_TEXT = PREFIX + "ALLERGYTEXT"
REACTION_TEXT = PREFIX + "REACTIONTEXT"
REACTION_DESCRIPTION = PREFIX + "REACTIONDESCRIPTION"
ALLERGY_NOTE = PREFIX + "ALLERGYNOTE"
DR_DISPLAY = PREFIX + "DRDISPLAY"
CONCLUSION = PREFIX + "CONCLUSION"
CONCLUSION_CODE_TEXT = PREFIX + "DRCODEDTEXT"
DR_FORM_TITLE = PREFIX + "DRFORMTITLE"
DOC_DESCRIPTION = PREFIX + "DOCDESCRIPTION"
DOC_MASTER_ID = PREFIX + "DOCMASTERID"
ATTACH_URL = "https://files.example/" + PREFIX + "ATTACHURL"
ATTACH_TITLE = PREFIX + "ATTACHTITLE"
SUBSCRIBER_ID = PREFIX + "SUBSCRIBERID"
DEPENDENT = PREFIX + "DEPENDENT"
CLASS_VALUE = PREFIX + "CLASSVALUE"
PAYOR_DISPLAY = PREFIX + "PAYORDISPLAY"
PRACTITIONER_FAMILY = PREFIX + "PRACTITIONERFAMILY"
ORG_NAME = PREFIX + "ORGNAME"
PROV_WHO = PREFIX + "PROVWHO"
PROV_REASON = PREFIX + "PROVREASON"
PROV_REASON_DISPLAY = PREFIX + "PROVCODEDREASON"
PROV_SUMMARY = PREFIX + "PROVSUMMARY"
PROV_INTENT = PREFIX + "PROVINTENT"
IMM_TEXT = PREFIX + "IMMTEXT"
IMM_OCCURRENCE = PREFIX + "IMMOCCURRENCE"
ENC_REASON = PREFIX + "ENCREASON"
ENC_PROVIDER = PREFIX + "ENCPROVIDER"
# Attachment data is base64, so the prefix does not survive encoding. The
# encoded form is the token, and it is scanned for literally.
ATTACH_DATA = base64.b64encode(
    (PREFIX + "ATTACHDATA").encode()).decode()
DR_FORM_DATA = base64.b64encode(
    (PREFIX + "DRFORMDATA").encode()).decode()
# A date cannot carry a prefix. This one is odd enough to be its own token.
DOB = "1911-11-11"

CANARIES = frozenset(
    value for name, value in dict(globals()).items()
    if name.isupper() and isinstance(value, str)
    and (PREFIX in value or value in (ATTACH_DATA, DR_FORM_DATA, DOB))
    and name not in ("PREFIX",))

PATIENT_ID = "canary-p1"
SUBJECT = {"reference": f"Patient/{PATIENT_ID}", "display": SUBJECT_DISPLAY}
LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
TOTAL_CHOL = "2093-3"
SMOKING = "72166-2"
SSN_SYSTEM = "http://hl7.org/fhir/sid/us-ssn"


def _bundle():
    """Every resource type an agent tool reads, each field carrying a canary.
    Codes are ones r6/terminology.py knows where a label is expected back, so
    a surface that shows a label shows the server's, not the feed's."""
    resources = [
        {"resourceType": "Patient", "id": PATIENT_ID,
         "text": {"status": "generated",
                  "div": f'<div xmlns="http://www.w3.org/1999/xhtml">'
                         f'{NARRATIVE}</div>'},
         "identifier": [{"system": "http://hospital.example/mrn",
                         "value": MRN},
                        {"system": SSN_SYSTEM, "value": SSN}],
         "name": [{"family": FAMILY, "given": [GIVEN], "text": NAME_TEXT}],
         "telecom": [{"system": "phone", "value": PHONE},
                     {"system": "email", "value": EMAIL}],
         "gender": "female", "birthDate": DOB,
         "address": [{"line": [ADDR_LINE], "city": ADDR_CITY,
                      "district": ADDR_DISTRICT, "postalCode": ADDR_POSTAL,
                      "text": ADDR_TEXT, "state": "MA", "country": "US"}],
         "maritalStatus": {"text": MARITAL_TEXT},
         "extension": [{"url": "http://example.org/fhir/free",
                        "valueString": EXT_STRING}],
         "contact": [{"name": {"family": CONTACT_FAMILY},
                      "telecom": [{"system": "phone",
                                   "value": CONTACT_PHONE}],
                      "address": {"line": [CONTACT_LINE]}}],
         "generalPractitioner": [{"reference": "Practitioner/canary-pr1",
                                  "display": GP_DISPLAY}]},
        {"resourceType": "Observation", "id": "canary-obs-chol",
         "status": "final",
         "code": {"coding": [{"system": LOINC, "code": TOTAL_CHOL,
                              "display": OBS_DISPLAY}],
                  "text": OBS_TEXT},
         "subject": SUBJECT,
         "performer": [{"reference": "Practitioner/canary-pr1",
                        "display": OBS_PERFORMER}],
         "effectiveDateTime": "2026-01-15",
         "valueQuantity": {"value": 244, "unit": "mg/dL",
                           "system": "http://unitsofmeasure.org",
                           "code": "mg/dL"},
         "interpretation": [{"text": OBS_INTERP_TEXT}],
         "note": [{"text": OBS_NOTE}]},
        {"resourceType": "Observation", "id": "canary-obs-smoking",
         "status": "final",
         "code": {"coding": [{"system": LOINC, "code": SMOKING}]},
         "subject": SUBJECT, "effectiveDateTime": "2026-01-15",
         "valueString": OBS_VALUE_STRING},
        {"resourceType": "Condition", "id": "canary-cond",
         "clinicalStatus": {"coding": [{
             "system": "http://terminology.hl7.org/CodeSystem/"
                       "condition-clinical", "code": "active"}]},
         "code": {"coding": [{"system": SNOMED, "code": "38341003",
                              "display": COND_DISPLAY}],
                  "text": COND_TEXT},
         "subject": SUBJECT, "onsetString": COND_ONSET,
         "recorder": {"reference": "Practitioner/canary-pr1",
                      "display": COND_RECORDER},
         "note": [{"text": COND_NOTE}]},
        {"resourceType": "MedicationRequest", "id": "canary-med",
         "status": "active", "intent": "order",
         "medicationCodeableConcept": {
             "coding": [{"system": RXNORM, "code": "860975",
                         "display": MED_DISPLAY}],
             "text": MED_TEXT},
         "subject": SUBJECT,
         "requester": {"reference": "Practitioner/canary-pr1",
                       "display": MED_REQUESTER},
         "dosageInstruction": [{"text": DOSE_TEXT,
                                "patientInstruction": PATIENT_INSTRUCTION}],
         "note": [{"text": MED_NOTE}]},
        {"resourceType": "AllergyIntolerance", "id": "canary-allergy",
         "clinicalStatus": {"coding": [{
             "system": "http://terminology.hl7.org/CodeSystem/"
                       "allergyintolerance-clinical", "code": "active"}]},
         "code": {"coding": [{"system": SNOMED, "code": "91936005",
                              "display": ALLERGY_DISPLAY}],
                  "text": ALLERGY_TEXT},
         "patient": SUBJECT,
         "reaction": [{"manifestation": [{"text": REACTION_TEXT}],
                       "description": REACTION_DESCRIPTION}],
         "note": [{"text": ALLERGY_NOTE}]},
        {"resourceType": "DiagnosticReport", "id": "canary-dr",
         "status": "final",
         "code": {"coding": [{"system": LOINC, "code": "24331-1",
                              "display": DR_DISPLAY}]},
         "subject": SUBJECT,
         "conclusion": CONCLUSION,
         "conclusionCode": [{"text": CONCLUSION_CODE_TEXT}],
         "presentedForm": [{"contentType": "text/plain",
                            "data": DR_FORM_DATA, "title": DR_FORM_TITLE}]},
        {"resourceType": "DocumentReference", "id": "canary-doc",
         "status": "current", "subject": SUBJECT,
         "masterIdentifier": {"system": "urn:canary", "value": DOC_MASTER_ID},
         "description": DOC_DESCRIPTION,
         "content": [{"attachment": {"contentType": "text/plain",
                                     "data": ATTACH_DATA,
                                     "url": ATTACH_URL,
                                     "title": ATTACH_TITLE}}]},
        {"resourceType": "Coverage", "id": "canary-cov", "status": "active",
         "beneficiary": {"reference": f"Patient/{PATIENT_ID}"},
         "subscriberId": SUBSCRIBER_ID, "dependent": DEPENDENT,
         "class": [{"type": {"coding": [{"code": "group"}]},
                    "value": CLASS_VALUE}],
         "payor": [{"display": PAYOR_DISPLAY}]},
        {"resourceType": "Practitioner", "id": "canary-pr1",
         "name": [{"family": PRACTITIONER_FAMILY}]},
        {"resourceType": "Organization", "id": "canary-org",
         "name": ORG_NAME},
        {"resourceType": "Immunization", "id": "canary-imm",
         "status": "completed",
         "vaccineCode": {"coding": [{"system": "http://hl7.org/fhir/sid/cvx",
                                     "code": "140"}],
                         "text": IMM_TEXT},
         "patient": SUBJECT, "occurrenceString": IMM_OCCURRENCE},
        {"resourceType": "Encounter", "id": "canary-enc",
         "status": "finished",
         "class": {"system": "http://terminology.hl7.org/CodeSystem/"
                             "v3-ActCode", "code": "AMB"},
         "subject": SUBJECT,
         "reasonCode": [{"text": ENC_REASON}],
         "serviceProvider": {"reference": "Organization/canary-org",
                             "display": ENC_PROVIDER}},
        {"resourceType": "Provenance", "id": "canary-prov",
         "target": [{"reference": "Observation/canary-obs-chol"}],
         "recorded": "2026-01-15T00:00:00Z",
         "agent": [{"who": {"reference": "Practitioner/canary-pr1",
                            "display": PROV_WHO}}],
         "reason": [{"coding": [{"system": "urn:canary", "code": "x",
                                 "display": PROV_REASON_DISPLAY}],
                     "text": PROV_REASON}],
         "extension": [{
             "url": "https://healthclaw.io/fhir/StructureDefinition/"
                    "curatr-correction",
             "extension": [{"url": "change_summary",
                            "valueString": PROV_SUMMARY},
                           {"url": "patient_intent",
                            "valueString": PROV_INTENT}]}]},
    ]
    return {"resourceType": "Bundle", "type": "collection",
            "entry": [{"resource": r} for r in resources]}


RESOURCES = [(e["resource"]["resourceType"], e["resource"]["id"])
             for e in _bundle()["entry"]]

_PREFIXED = re.compile(PREFIX + r"[A-Za-z0-9@.]*")


def canaries_in(body: str) -> set[str]:
    """Every canary in `body`. A prefixed token that is not a known canary
    is a canary cut short, and is reported as found under its own text."""
    found = {c for c in CANARIES if c in body}
    for token in _PREFIXED.findall(body):
        if not any(token in c or c in token for c in found):
            found.add(token)
    return found


# ---------------------------------------------------------------------------
# The seeded tenant. Its own tenant id, so nothing else in the suite shares
# the rows, and a step-up token for it (every surface is called with the
# token an MCP session holds).
# ---------------------------------------------------------------------------

TENANT = "canary-scan"


@pytest.fixture
def scan(client):
    token = generate_step_up_token(TENANT)
    headers = {"X-Tenant-Id": TENANT, "X-Step-Up-Token": token}
    response = client.post("/r6/fhir/Bundle/$ingest-context",
                           headers=headers, json=_bundle())
    body = response.get_json()
    assert response.status_code == 201, response.get_data(as_text=True)
    # ingest_bundle drops an unsupported type silently apart from this
    # count; a dropped type would read as clean everywhere below.
    assert body["resource_count"] == len(RESOURCES), body
    assert body.get("skipped_count", 0) == 0, body
    return {"client": client, "headers": headers,
            "context_id": body["context_id"]}


def _ok(response, what):
    body = response.get_data(as_text=True)
    assert response.status_code == 200, f"{what}: {response.status_code} {body[:300]}"
    return body


# ---------------------------------------------------------------------------
# One driver per READ tool: the same Flask call tools.ts makes, returning
# (label, body) pairs. Each asserts it reached data before returning.
# ---------------------------------------------------------------------------

def _context_get(s):
    c, h, cid = s["client"], s["headers"], s["context_id"]
    plain = _ok(c.get(f"/r6/fhir/context/{cid}", headers=h), "context")
    assert cid in plain, plain[:300]
    # tools.ts sends no _include; the route supports one and it is the form
    # that returns resources, so it is scanned too.
    full = _ok(c.get(f"/r6/fhir/context/{cid}?_include=resources",
                     headers=h), "context+resources")
    assert "canary-obs-chol" in full, full[:300]
    return [("context", plain), ("context?_include=resources", full)]


def _fhir_read(s):
    out = []
    for rtype, rid in RESOURCES:
        body = _ok(s["client"].get(f"/r6/fhir/{rtype}/{rid}",
                                   headers=s["headers"]), rtype)
        assert rid in body, body[:300]
        out.append((f"{rtype}/{rid}", body))
    return out


def _fhir_search(s):
    out = []
    for rtype, rid in RESOURCES:
        body = _ok(s["client"].get(f"/r6/fhir/{rtype}?_count=50",
                                   headers=s["headers"]), rtype)
        assert rid in body, f"{rtype} search never returned {rid}"
        out.append((f"{rtype}?", body))
    for rtype in ("Observation", "Condition", "MedicationRequest"):
        body = _ok(s["client"].get(
            f"/r6/fhir/{rtype}?patient=Patient/{PATIENT_ID}",
            headers=s["headers"]), rtype)
        assert '"entry"' in body, body[:300]
        out.append((f"{rtype}?patient=", body))
    return out


def _fhir_stats(s):
    response = s["client"].get(
        f"/r6/fhir/Observation/$stats?code={TOTAL_CHOL}"
        f"&patient=Patient/{PATIENT_ID}", headers=s["headers"])
    body = _ok(response, "$stats")
    count = next(p["valueInteger"] for p in response.get_json()["parameter"]
                 if p["name"] == "count")
    assert count >= 1, body[:300]
    return [("$stats", body)]


def _fhir_lastn(s):
    body = _ok(s["client"].get(
        f"/r6/fhir/Observation/$lastn?patient=Patient/{PATIENT_ID}&max=5",
        headers=s["headers"]), "$lastn")
    assert "canary-obs-chol" in body, body[:300]
    return [("$lastn", body)]


def _fhir_interpret_labs(s):
    body = _ok(s["client"].post(
        f"/r6/fhir/Observation/$interpret?subject=Patient/{PATIENT_ID}",
        headers=s["headers"], json={}), "$interpret")
    assert TOTAL_CHOL in body, body[:300]
    return [("$interpret", body)]


def _care_gaps(s):
    body = _ok(s["client"].post(
        f"/r6/fhir/Patient/$care-gaps?subject=Patient/{PATIENT_ID}",
        headers=s["headers"], json={}), "$care-gaps")
    assert PATIENT_ID in body, body[:300]
    return [("$care-gaps", body)]


def _guardrail_conformance(s):
    response = s["client"].get("/r6/fhir/$conformance",
                               headers=s["headers"])
    body = response.get_data(as_text=True)
    # 503 is a graded-below-A scorecard; tools.ts returns it as a result.
    assert response.status_code in (200, 503), body[:300]
    assert '"grade"' in body, body[:300]
    return [("$conformance", body)]


def _fhir_permission_evaluate(s):
    body = _ok(s["client"].post(
        "/r6/fhir/Permission/$evaluate", headers=s["headers"],
        json={"subject": f"Patient/{PATIENT_ID}", "action": "read",
              "resource": "Observation/canary-obs-chol"}),
        "Permission/$evaluate")
    assert '"decision"' in body, body[:300]
    return [("Permission/$evaluate", body)]


def _fhir_subscription_topics(s):
    # An allowlisted unredacted exit (tests/test_unredacted_exits.py) for
    # server metadata. No canary is seeded into a SubscriptionTopic; this
    # only checks that patient data does not arrive by it.
    body = _ok(s["client"].get("/r6/fhir/SubscriptionTopic/$list",
                               headers=s["headers"]), "$list")
    assert '"total"' in body, body[:300]
    return [("SubscriptionTopic/$list", body)]


def _wearables_sync_status(s):
    response = s["client"].get(f"/wearables/sync-status?tenant_id={TENANT}",
                               headers=s["headers"])
    body = _ok(response, "sync-status")
    assert response.get_json()["tenant_id"] == TENANT, body[:300]
    assert "connections" in response.get_json(), body[:300]
    return [("wearables/sync-status", body)]


def _sources_check(s):
    response = s["client"].get(
        f"/command-center/api/sources-summary?tenant={TENANT}",
        headers=s["headers"])
    body = _ok(response, "sources-summary")
    # It counts the tenant's rows, so a count of zero is the wrong tenant.
    assert response.get_json()["total_records"] > 0, body[:300]
    return [("sources-summary", body)]


def _fhir_compiled_truth(s):
    out = []
    for rtype, rid in (("Observation", "canary-obs-chol"),
                       ("Patient", PATIENT_ID)):
        body = _ok(s["client"].get(f"/r6/fhir/{rtype}/{rid}/$compiled-truth",
                                   headers=s["headers"]), rtype)
        assert rid in body, body[:300]
        out.append((f"{rtype}/$compiled-truth", body))
    assert "canary-prov" in out[0][1], (
        "the Provenance never reached the timeline, so its canaries were "
        "not measured: " + out[0][1][:300])
    return out


def _curatr_evaluate(s):
    # The terminology lookup is patched so the display-mismatch branch fires
    # without the network (same patch as the coverage inventory).
    out = []
    with patch("r6.curatr.CuratrEngine._lookup_code",
               return_value={"valid": True, "display": "Canonical label",
                             "message": None}):
        for rtype, rid in RESOURCES:
            if rtype not in ("Condition", "Observation", "MedicationRequest",
                             "AllergyIntolerance", "Immunization"):
                continue
            body = _ok(s["client"].get(
                f"/r6/fhir/{rtype}/{rid}/$curatr-evaluate",
                headers=s["headers"]), rtype)
            assert '"issues"' in body, body[:300]
            out.append((f"{rtype}/{rid}/$curatr-evaluate", body))
    return out


def _populate_questionnaire():
    def expr(link_id, expression):
        return {"linkId": link_id, "type": "string", "extension": [{
            "url": "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
                   "sdc-questionnaire-initialExpression",
            "valueExpression": {"language": "text/fhirpath",
                                "expression": expression}}]}
    med = "http://hl7.org/fhir/StructureDefinition/MedicationRequest#MedicationRequest"
    cond = "http://hl7.org/fhir/StructureDefinition/Condition#Condition"
    allergy = ("http://hl7.org/fhir/StructureDefinition/"
               "AllergyIntolerance#AllergyIntolerance")
    items = [expr(k, v) for k, v in {
        "family": "%patient.name.family.first()",
        "given": "%patient.name.given.first()",
        "name-text": "%patient.name.text",
        "mrn": "%patient.identifier.value",
        "phone": "%patient.telecom.where(system='phone').value",
        "district": "%patient.address.district",
        "addr-text": "%patient.address.text",
        "contact": "%patient.contact.name.family",
    }.items()]
    items += [
        {"linkId": "chol", "type": "string",
         "code": [{"system": LOINC, "code": TOTAL_CHOL}]},
        {"linkId": "smoking", "type": "string",
         "code": [{"system": LOINC, "code": SMOKING}]},
        {"linkId": "meds", "type": "group", "repeats": True, "item": [
            {"linkId": "meds.name", "type": "string",
             "definition": f"{med}.medicationCodeableConcept.text"},
            {"linkId": "meds.dose", "type": "string",
             "definition": f"{med}.dosageInstruction.text"}]},
        {"linkId": "conds", "type": "group", "repeats": True, "item": [
            {"linkId": "conds.name", "type": "string",
             "definition": f"{cond}.code.text"}]},
        {"linkId": "allergies", "type": "group", "repeats": True, "item": [
            {"linkId": "allergies.name", "type": "string",
             "definition": f"{allergy}.code.text"}]},
    ]
    return {"resourceType": "Questionnaire", "id": "canary-q",
            "status": "active", "item": items}


def _questionnaire_populate(s):
    body = _ok(s["client"].post(
        "/r6/fhir/Questionnaire/$populate", headers=s["headers"],
        json={"resourceType": "Parameters", "parameter": [
            {"name": "subject",
             "valueReference": {"reference": f"Patient/{PATIENT_ID}"}},
            {"name": "questionnaire",
             "resource": _populate_questionnaire()}]}), "$populate")
    # The projection must actually have run, or every absence below is
    # vacuous: the permitted family name is the proof.
    assert FAMILY in body, body[:400]
    return [("$populate", body)]


def _share_bundle(profile):
    def driver(s):
        body = _ok(s["client"].post(
            "/r6/fhir/$share-bundle", headers=s["headers"],
            json={"profile": profile, "patient_id": PATIENT_ID}),
            f"$share-bundle {profile}")
        assert "canary-obs-chol" in body, body[:300]
        return [(f"$share-bundle profile={profile}", body)]
    return driver


READ, WRITE, OUT = "read", "write", "out-of-scope"

#: Every tool in adapters/tools.manifest.json, classified. The value for a
#: READ tool is its driver; for the others, the reason it is not scanned.
SURFACES = {
    "context_get": (READ, _context_get),
    "fhir_read": (READ, _fhir_read),
    "fhir_search": (READ, _fhir_search),
    "fhir_validate": (OUT, "validates the caller's own payload; reads no "
                           "stored record"),
    "questionnaire_populate": (READ, _questionnaire_populate),
    "questionnaire_extract": (WRITE, "write tier, step-up; extracts from "
                                     "the caller's QuestionnaireResponse"),
    "fhir_propose_write": (WRITE, "write tier; previews the caller's "
                                  "resource"),
    "fhir_commit_write": (WRITE, "write tier, step-up"),
    "fhir_stats": (READ, _fhir_stats),
    "fhir_interpret_labs": (READ, _fhir_interpret_labs),
    "care_gaps": (READ, _care_gaps),
    "guardrail_conformance": (READ, _guardrail_conformance),
    "fhir_lastn": (READ, _fhir_lastn),
    "fhir_permission_evaluate": (READ, _fhir_permission_evaluate),
    "fhir_subscription_topics": (READ, _fhir_subscription_topics),
    "wearables_sync_status": (READ, _wearables_sync_status),
    "sources_check": (READ, _sources_check),
    "fhir_compiled_truth": (READ, _fhir_compiled_truth),
    "curatr_evaluate": (READ, _curatr_evaluate),
    "curatr_apply_fix": (WRITE, "stages a curatr-fix on the action rail; "
                                "its version read is fhir_read's call"),
    "fhir_get_token": (OUT, "operator tool; mints a token, returns no "
                            "record data"),
    "fhir_seed": (WRITE, "writes a fixed demo bundle"),
    "action_propose": (WRITE, "stages a call or text on the action rail"),
    "rx_transfer_request": (WRITE, "stages a call on the action rail; its "
                                   "script is built from medications and "
                                   "is NOT scanned here"),
    "action_commit": (WRITE, "submits an action for out-of-band approval"),
    "action_status": (OUT, "reads an action row that only a write-tier "
                           "tool creates; not scanned"),
    # tools.ts sends no profile by default (intake); a caller may ask for
    # `deidentified`. Each has its own contract, so READ_ROWS scans both.
    "shl_generate": (READ, _share_bundle("intake")),
    "search": (READ, _fhir_search),
    "fetch": (READ, _fhir_read),
}

# ---------------------------------------------------------------------------
# What each surface may carry. Anything not named here must be absent.
# ---------------------------------------------------------------------------

#: The %patient projection (council ruling D10; r6/sdc/expressions.py
#: `_PROJECTED_*`; tests/test_sdc_populate_bounded.py::PERMITTED).
POPULATE_KEEPS = frozenset({FAMILY, GIVEN, PHONE, EMAIL, ADDR_LINE,
                            ADDR_CITY, ADDR_POSTAL, DOB})

#: What `_intake_strip` removes: top-level `note`, the narrative and
#: SSN-class identifiers. The intake share is identified by design
#: (r6/routes.py `share_bundle` docstring; tests/test_share_bundle.py), so
#: every other canary may arrive, and these must not.
INTAKE_STRIPS = frozenset({NARRATIVE, SSN, OBS_NOTE, COND_NOTE, MED_NOTE,
                           ALLERGY_NOTE})

#: NOT a contract: a leak this scan found, pinned by the strict xfail at the
#: end of the file. `$compiled-truth` builds its timeline from the stored
#: Provenance rows without redacting them (r6/routes.py `compiled_truth`,
#: the loop over `prov_rows`), copying `agent[].who.display`,
#: `reason[0].coding[0].display` and the curatr-correction extension's
#: `change_summary` / `patient_intent` strings into the response. Excused on
#: the main row only so the rest of that surface stays gated meanwhile.
COMPILED_TRUTH_TIMELINE_LEAK = frozenset({PROV_WHO, PROV_REASON_DISPLAY,
                                          PROV_SUMMARY, PROV_INTENT})

#: `apply_patient_controlled_redaction` keeps the top-level birthDate
#: verbatim (#617 docstring); nothing else.
DEIDENTIFIED_KEEPS = frozenset({DOB})

READ_ROWS = [
    (name, driver, frozenset())
    for name, (kind, driver) in SURFACES.items()
    if kind == READ and name not in ("questionnaire_populate",
                                     "shl_generate", "fhir_compiled_truth")
] + [
    ("fhir_compiled_truth", _fhir_compiled_truth,
     COMPILED_TRUTH_TIMELINE_LEAK),
    ("questionnaire_populate", _questionnaire_populate, POPULATE_KEEPS),
    ("shl_generate[deidentified]", _share_bundle("deidentified"),
     DEIDENTIFIED_KEEPS),
    ("shl_generate[intake]", _share_bundle("intake"),
     CANARIES - INTAKE_STRIPS),
]


def _manifest_tools():
    manifest = json.loads(MANIFEST.read_text())
    return manifest, [t["name"] for t in manifest["tools"]]


def test_every_manifest_tool_is_classified():
    """The scan's reach is the catalogue's. A tool added to tools.ts and
    regenerated into the manifest is red here until it is classed, and a
    READ tool cannot be quietly moved out of the scan without this diff."""
    manifest, names = _manifest_tools()
    assert manifest["tool_count"] == len(names) == 29
    assert len(set(names)) == len(names)
    assert set(names) == set(SURFACES), (
        f"unclassified: {sorted(set(names) - set(SURFACES))}; "
        f"gone from the manifest: {sorted(set(SURFACES) - set(names))}")
    kinds = [kind for kind, _ in SURFACES.values()]
    assert (kinds.count(READ), kinds.count(WRITE), kinds.count(OUT)) == (
        18, 8, 3)


def test_every_canary_is_distinct():
    """A canary that contains another would report the wrong field."""
    for a in CANARIES:
        for b in CANARIES - {a}:
            assert a not in b, (a, b)
    assert len(CANARIES) > 60


def test_the_scan_finds_a_canary_it_is_shown():
    """The control. If the matcher could not see a canary, every row below
    would pass for that reason alone."""
    assert canaries_in(json.dumps(_bundle())) == set(CANARIES)
    # A canary cut short still counts.
    assert canaries_in('{"x": "' + PREFIX + 'FAM"}') == {PREFIX + "FAM"}


@pytest.mark.parametrize("surface, driver, keeps", READ_ROWS,
                         ids=[r[0] for r in READ_ROWS])
def test_no_canary_leaves_a_read_surface(scan, surface, driver, keeps):
    leaks = {}
    for label, body in driver(scan):
        found = canaries_in(body) - keeps
        if found:
            leaks[label] = sorted(found)
    assert not leaks, (
        f"{surface} returned canary PHI the redaction contract does not "
        f"keep there: {leaks}")


@pytest.mark.xfail(strict=True, reason=(
    "LEAK: $compiled-truth (MCP tool fhir_compiled_truth) copies the stored "
    "Provenance's agent.who.display, reason.coding.display and "
    "curatr-correction change_summary/patient_intent into its timeline "
    "verbatim; the timeline is built from raw rows, never apply_redaction. "
    "GET /Provenance/<id> strips all four. Found by this scan; not fixed "
    "in this PR."))
@pytest.mark.parametrize("field, canary", [
    ("Provenance.agent.who.display", PROV_WHO),
    ("Provenance.reason.coding.display", PROV_REASON_DISPLAY),
    ("curatr-correction change_summary", PROV_SUMMARY),
    ("curatr-correction patient_intent", PROV_INTENT),
])
def test_compiled_truth_timeline_carries_no_upstream_free_text(
        scan, field, canary):
    """One row per field, so fixing one goes red under strict xfail and
    forces its canary out of COMPILED_TRUTH_TIMELINE_LEAK, where the main
    row then gates it. A single row would stay xfailed through a partial
    fix and the excuse would never shrink."""
    (_, body), _ = _fhir_compiled_truth(scan)
    assert canary not in body, f"{field} reached the timeline"
