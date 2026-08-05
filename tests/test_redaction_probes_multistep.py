"""The four #282 sites that are only reachable through a multi-step flow.

`tests/test_redaction_coverage_inventory.py` measured the three sites you can
hit with one request and deliberately named these four as NOT covered:

    r6/actions/rails/form_fill.py   (Patient.name -> rendered intake PDF)
    r6/sdc/documents.py             (the persisted DocumentReference)
    r6/smbp/routes.py               (every tenant Observation -> BP report)
    r6/curatr.py                    ($curatr-apply-fix's updated_resource)

Same method as that file: seed resources whose free-text fields each carry a
DISTINCT marker, drive the real flow to the point where content could reach a
caller, and assert on what leaves the boundary — not on what an internal
function returned.

## Why the flows are driven, not stubbed

Each of these sites is several hops from any response. form_fill's Patient read
only escapes through a PDF that is rendered, base64-embedded in a
DocumentReference, and fetched over a signed link; curatr's read only escapes
after a step-up-gated write. Calling the internal function and asserting on its
return value would measure the function, not the product. So: propose ->
review -> confirm -> download, and enroll -> reading -> report, over the Flask
test client.

## Every probe must be able to fail

A flow that 404s because an id or a payload shape was guessed wrong makes the
marker assertion pass while measuring nothing. Every probe asserts the success
status FIRST with the response body in the message, and
`test_the_pdf_text_extractor_actually_reads_text` is the control for the PDF
extractor — a marker search over bytes we cannot decode is not a measurement.

## What this file is

A characterization of today's behaviour. Rows that assert a marker DOES arrive
are recording content that reaches a caller today; they are written so that
changing it fails here and forces the inventory to be updated deliberately.

## What it measured

    curatr.py:1096      LEAK — the whole stored resource, every free-text
                        field the fix did not touch, in the HTTP response
    form_fill.py:153    reaches a caller — the patient's own name, in the
                        title of the patient's own intake form
    smbp/routes.py      clean but for `effectiveDateTime`, which the report
                        echoes verbatim from the stored Observation
    sdc/documents.py:72 internal; its return value reaches nobody, and the
                        row it writes is redacted on the FHIR read path

Each row was mutation-checked: the guard (or the field) was removed from
production code, the probe was confirmed red, and the change was reverted.
Without that step a green probe is indistinguishable from a probe that never
reached the code it names.

## Still not covered

- `r6/actions/rails/form_fill.py::_resolve_questionnaire` (line 129) reads a
  stored Questionnaire for labels. Only its `title` and item `text` are
  rendered, and the canonical intake Questionnaire is the fallback, so a
  tenant would have to have ingested a *Questionnaire* resource carrying
  free text. Not probed — no ingest path in this repo writes Questionnaire.
- The SMBP `?format=pdf` branch persists a second DocumentReference
  (`r6/smbp/routes.py::_persist_document_reference`). Its size-only shape is
  covered by tests/test_smbp_routes.py; not re-probed here.
- ADJACENT, and not on #282's list: the SDC populate step behind the review
  page copies `Patient.name.family`, birth date, phone and address into the
  draft QuestionnaireResponse unredacted (r6/sdc/intake.py:94-115). That is
  what an intake form is for, so it is not filed as a defect — but it means
  the review page and the delivery link carry full demographics, and no probe
  here bounds what else that step could pick up.
"""

from __future__ import annotations

import base64
import json
import re
import zlib
from urllib.parse import parse_qs, urlparse

import pytest

from models import db
from r6.models import R6Resource
from tests.test_redaction_coverage_inventory import (
    NAME_MARKER, OBS_DISPLAY_MARKER, OBS_TEXT_MARKER,
)

# One marker per field, so a hit names WHICH field leaked. NAME_MARKER,
# OBS_DISPLAY_MARKER and OBS_TEXT_MARKER are the inventory file's, reused so a
# grep for a marker finds every probe that watches that field.
SUBJECT_LABEL_MARKER = "PHISUBJECTLABELMARKER"
MED_TEXT_MARKER = "PHIMEDTEXTMARKER"
ALLERGY_TEXT_MARKER = "PHIALLERGYTEXTMARKER"
COMPONENT_DISPLAY_MARKER = "PHICOMPONENTDISPLAYMARKER"
EFFECTIVE_MARKER = "PHIEFFECTIVEMARKER"
OBS_NOTE_MARKER = "PHIOBSNOTEMARKER"
COND_TEXT_MARKER = "PHICONDTEXTMARKER"
COND_DISPLAY_MARKER = "PHICONDDISPLAYMARKER"
COND_NOTE_MARKER = "PHICONDNOTEMARKER"

ALL_MARKERS = (
    NAME_MARKER, SUBJECT_LABEL_MARKER, OBS_DISPLAY_MARKER, OBS_TEXT_MARKER,
    MED_TEXT_MARKER, ALLERGY_TEXT_MARKER, COMPONENT_DISPLAY_MARKER,
    EFFECTIVE_MARKER, OBS_NOTE_MARKER, COND_TEXT_MARKER, COND_DISPLAY_MARKER,
    COND_NOTE_MARKER,
)

PROBE_PATIENT_ID = "probe-patient-1"
PROBE_PATIENT_REF = "Patient/%s" % PROBE_PATIENT_ID

LOINC = "http://loinc.org"
SYSTOLIC = "8480-6"
DIASTOLIC = "8462-4"


# ---------------------------------------------------------------------------
# Seeding — resources go straight into R6Resource, which is how an upstream
# feed arrives (redaction in this codebase is applied on read, not on ingest).
# ---------------------------------------------------------------------------

def _store(resource, tenant_id):
    row = R6Resource(resource_type=resource["resourceType"],
                     resource_json=json.dumps(resource),
                     resource_id=resource.get("id"), tenant_id=tenant_id)
    db.session.add(row)
    db.session.commit()
    return row.id


def _marked_patient():
    """Two markers on the name, because two different reads consume it.

    `name[0].family` is what the SDC populate step copies into the intake
    QuestionnaireResponse (`%patient.name.family`, r6/sdc/intake.py:104).
    `name[0].text` is what `FormFillExecutor._subject_label` prefers
    (form_fill.py:157) and the populate step never touches. Splitting them is
    what lets the form_fill probe below attribute a hit to line 153 rather
    than to the questionnaire body — with one marker the probe stays green
    when line 153 is deleted, which is how it was caught here.
    """
    return {
        "resourceType": "Patient", "id": PROBE_PATIENT_ID,
        "name": [{"family": NAME_MARKER, "given": ["Josephine"],
                  "text": SUBJECT_LABEL_MARKER}],
        "gender": "female", "birthDate": "1962-03-04",
    }


def _marked_medication():
    return {
        "resourceType": "MedicationRequest", "id": "probe-med-1",
        "status": "active", "intent": "order",
        "medicationCodeableConcept": {"text": MED_TEXT_MARKER},
        "dosageInstruction": [{"text": "Take 1 tablet twice daily"}],
        "subject": {"reference": PROBE_PATIENT_REF},
    }


def _marked_allergy():
    return {
        "resourceType": "AllergyIntolerance", "id": "probe-allergy-1",
        "code": {"text": ALLERGY_TEXT_MARKER},
        "reaction": [{"manifestation": [{"text": "Hives"}]}],
        "patient": {"reference": PROBE_PATIENT_REF},
    }


def _marked_bp_observation():
    """A BP-panel Observation with a marker in every free-text field the SMBP
    report reader could conceivably carry through: the panel code's display
    and text, each component code's display, the note, and the timestamp
    (`effectiveDateTime` is a string the report echoes verbatim)."""
    def component(code, marker_display, value):
        return {
            "code": {"coding": [{"system": LOINC, "code": code,
                                 "display": marker_display}]},
            "valueQuantity": {"value": value, "unit": "mm[Hg]"},
        }
    return {
        "resourceType": "Observation", "id": "probe-bp-1", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": "85354-9",
                             "display": OBS_DISPLAY_MARKER}],
                 "text": OBS_TEXT_MARKER},
        "subject": {"reference": PROBE_PATIENT_REF},
        "effectiveDateTime": "2026-01-15T08:00:00Z " + EFFECTIVE_MARKER,
        "note": [{"text": OBS_NOTE_MARKER}],
        "component": [
            component(SYSTOLIC, COMPONENT_DISPLAY_MARKER + "SYS", 148),
            component(DIASTOLIC, COMPONENT_DISPLAY_MARKER + "DIA", 92),
        ],
    }


def _marked_condition():
    """An ICD-9-coded Condition — the shape curatr proposes a fix for — with
    markers in the fields a fix does NOT touch."""
    return {
        "resourceType": "Condition", "id": "probe-cond-1",
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-9-cm",
                             "code": "250.00",
                             "display": COND_DISPLAY_MARKER}],
                 "text": COND_TEXT_MARKER},
        "subject": {"reference": PROBE_PATIENT_REF},
        "note": [{"text": COND_NOTE_MARKER}],
    }


def _markers_in(text: str) -> set[str]:
    return {m for m in ALL_MARKERS if m in text}


# ---------------------------------------------------------------------------
# PDF text extraction. reportlab writes ASCII85 + Flate content streams, so a
# marker search over raw bytes finds nothing whether or not the marker is in
# the document — which is exactly the kind of probe that passes while
# measuring nothing. Decode, then read the text-showing operands.
# ---------------------------------------------------------------------------

_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.S)
_LITERAL = re.compile(rb"\((?:\\.|[^\\()])*\)", re.S)


def pdf_text(pdf_bytes: bytes) -> str:
    """Concatenate every literal string drawn in `pdf_bytes`.

    Reportlab splits a wrapped line across several `Tj` operands, so the
    pieces are joined with a space and callers should search for a marker that
    survives being adjacent to other words (all markers here are one token).
    """
    out = []
    for match in _STREAM.finditer(pdf_bytes):
        data = match.group(1).strip()
        for decode in (lambda b: base64.a85decode(b, adobe=True),
                       zlib.decompress):
            try:
                data = decode(data)
            except Exception:  # noqa: BLE001 — stream may use either, or neither
                pass
        if not isinstance(data, bytes):
            continue
        for literal in _LITERAL.findall(data):
            out.append(literal[1:-1].decode("latin-1"))
    return " ".join(out)


def test_the_pdf_text_extractor_actually_reads_text():
    """The control for every PDF probe below.

    If this fails, a marker search over a rendered PDF is measuring nothing
    and the "no marker found" rows are all false negatives.
    """
    from r6.sdc.pdf import render_questionnaire_response_pdf
    rendered = render_questionnaire_response_pdf(
        {"resourceType": "QuestionnaireResponse", "status": "completed",
         "item": []},
        subject_label=NAME_MARKER)
    assert NAME_MARKER not in rendered.decode("latin-1", "replace"), (
        "the raw PDF bytes contain the marker, so this control cannot "
        "distinguish a working extractor from a broken one — pick a "
        "different fixture")
    assert NAME_MARKER in pdf_text(rendered), (
        "the extractor could not read back a string reportlab definitely "
        "drew; every PDF assertion in this file is unmeasured")


# ---------------------------------------------------------------------------
# Site 1: r6/actions/rails/form_fill.py — _subject_label reads Patient.name
# with no redaction and puts it in the rendered PDF's title. The PDF leaves
# over a signed link that carries NO tenant or step-up header: the signature
# is the whole credential.
# ---------------------------------------------------------------------------

def _download_the_form(client, app, tenant_headers, auth_headers):
    """propose -> commit -> review -> confirm -> GET the signed link.

    Returns (docref_id, pdf_bytes). Asserts each hop, because a 4xx anywhere
    would leave the caller asserting markers against an error page.
    """
    from r6.actions.confirmations import ACTION_APPROVAL_AUDIENCE
    from r6.actions.models import ProposedAction
    from r6.stepup import generate_step_up_token
    tenant = tenant_headers["X-Tenant-Id"]

    proposed = client.post("/r6/actions/propose", headers=tenant_headers,
                           json={"kind": "form-fill",
                                 "payload": {"to": "Intake portal",
                                             "questionnaire": "healthclaw-intake",
                                             "body": "new patient intake form"}})
    assert proposed.status_code == 201, proposed.get_data(as_text=True)
    action_id = proposed.get_json()["id"]

    committed = client.post("/r6/actions/%s/commit" % action_id,
                            headers=auth_headers)
    assert committed.status_code == 202, committed.get_data(as_text=True)

    # Act on the populated medication and confirm the real allergy, so the
    # attestation is satisfied affirmatively rather than by an NKA checkbox.
    review = client.post("/r6/actions/%s/review" % action_id,
                         headers=auth_headers,
                         json={"med-0": "yes", "allergy-0": "confirm"})
    assert review.status_code == 200, review.get_data(as_text=True)
    assert review.get_json()["reviewed_qr_id"], review.get_data(as_text=True)

    approval = dict(auth_headers)
    approval["X-Step-Up-Token"] = generate_step_up_token(
        tenant, audience=ACTION_APPROVAL_AUDIENCE, operation=action_id)
    confirm = client.post("/r6/actions/%s/confirm" % action_id,
                          headers=approval, json={})
    assert confirm.status_code == 200, confirm.get_data(as_text=True)
    assert confirm.get_json()["status"] == "completed", \
        confirm.get_data(as_text=True)

    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        outcome = json.loads(row.outcome_summary)
    link, docref_id = outcome["delivery_link"], outcome["document_reference_id"]

    parsed = urlparse(link)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    # No tenant/step-up headers: the signature IS the credential.
    downloaded = client.get(parsed.path, query_string=query)
    assert downloaded.status_code == 200, downloaded.get_data(as_text=True)[:200]
    assert downloaded.mimetype == "application/pdf"
    return docref_id, downloaded.data


@pytest.fixture
def form_fill_ready(app, tenant_headers, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    with app.app_context():
        for resource in (_marked_patient(), _marked_medication(),
                         _marked_allergy()):
            _store(resource, tenant_headers["X-Tenant-Id"])


def test_form_fill_subject_label_puts_the_patient_name_in_the_pdf(
        client, app, tenant_headers, auth_headers, action_registry,
        form_fill_ready):
    """CHARACTERIZATION — content DOES reach a caller from form_fill.py:153.

    `FormFillExecutor._subject_label` loads the subject Patient, takes
    `name[0].text` verbatim with no redaction in between, and
    `r6/sdc/pdf.py::_title` renders it as "<questionnaire title> — <label>".
    The PDF leaves over the signed delivery link, which carries no tenant and
    no step-up header — the signature is the whole credential.

    The marker is `name[0].text`, a field the populate step never reads, so a
    hit here can only have come from line 153. The redacting single-resource
    read drops `name.text` outright (r6/redaction.py:55), so the two paths
    disagree about the same field. Whether the patient's own name belongs on
    the patient's own intake form is a product call, not a test's; this row
    exists so the call gets made deliberately.
    """
    _, pdf = _download_the_form(client, app, tenant_headers, auth_headers)
    text = pdf_text(pdf)
    assert "Intake" in text, (
        "the extracted text does not look like the intake form, so a marker "
        "search over it proves nothing: " + text[:300])
    assert SUBJECT_LABEL_MARKER in text, (
        "expected the unredacted Patient.name.text in the PDF title; not "
        "finding it means the flow changed and this probe stopped measuring "
        "form_fill.py:153 — re-derive it rather than deleting the row")


def test_form_fill_pdf_carries_only_the_reviewed_answers_and_the_name(
        client, app, tenant_headers, auth_headers, action_registry,
        form_fill_ready):
    """Pins the full marker set that reaches the delivery link.

    `NAME_MARKER` (the family name) arrives through the SDC populate step —
    `demographics.family-name` is an item of the intake questionnaire — not
    through form_fill's Patient read. Medication and allergy free text is
    there because a human confirmed each row on the review page. All three
    are the form's content.

    Asserting the set EXACTLY is the point: a future change that starts
    rendering some other upstream field (a code `display`, say) fails here
    instead of shipping.
    """
    _, pdf = _download_the_form(client, app, tenant_headers, auth_headers)
    assert _markers_in(pdf_text(pdf)) == {
        NAME_MARKER, SUBJECT_LABEL_MARKER, MED_TEXT_MARKER,
        ALLERGY_TEXT_MARKER}


# ---------------------------------------------------------------------------
# Site 2: r6/sdc/documents.py:72 — persist_intake_document returns the
# DocumentReference it just built. Every field of that dict was constructed in
# that function from its own arguments, and its only production caller
# (form_fill.py:90) reads `['id']`. So the RETURN VALUE reaches nobody. The
# stored row, however, is readable through the ordinary FHIR path, and that is
# where its content can leave.
# ---------------------------------------------------------------------------

def test_persist_intake_document_return_value_reaches_no_caller():
    """Evidence, not assertion: the call chain from the read to a response.

    `grep -rn persist_intake_document --include='*.py'` finds exactly one
    production caller. This pins that caller's use of the returned dict, so
    the "nothing escapes" claim above stops being true loudly rather than
    silently if someone starts returning more of it.
    """
    import inspect
    from r6.actions.rails import form_fill
    source = inspect.getsource(form_fill.FormFillExecutor.execute)
    uses = set(re.findall(r"docref\[([^\]]+)\]", source))
    assert uses == {"'id'"}, (
        "form_fill now reads more than the id off persist_intake_document's "
        "return value (%s); documents.py:72 may now be an exit point and "
        "needs its own probe" % sorted(uses))


def test_document_reference_read_drops_the_embedded_pdf(
        client, app, tenant_headers, auth_headers, action_registry,
        form_fill_ready):
    """CLEAN — the boundary documents.py:72 writes to does redact.

    The DocumentReference persisted at documents.py:66-72 embeds the rendered
    PDF as base64 in `content[0].attachment.data`, and that PDF contains the
    patient's name. Redaction cannot see inside base64, so the guard that
    matters is `r6/redaction.py::_redact_recursive` dropping `data`/`url`/
    `title` from any object that has a `contentType`. It does, so a
    tenant-authenticated `GET /r6/fhir/DocumentReference/<id>` returns the
    envelope without the document.

    That leaves the signed delivery link as the only exit for those bytes,
    which is the intended one. Pinned because the guard is a shape test on a
    sibling key, not a rule about DocumentReference — a renderer that emitted
    the attachment without `contentType` would silently bypass it.
    """
    docref_id, _ = _download_the_form(client, app, tenant_headers, auth_headers)
    response = client.get("/r6/fhir/DocumentReference/%s" % docref_id,
                          headers=tenant_headers)
    assert response.status_code == 200, response.get_data(as_text=True)[:200]
    body = response.get_json()
    attachment = body["content"][0]["attachment"]
    assert attachment.get("contentType") == "application/pdf", (
        "the attachment envelope is not the shape this probe measures, so "
        "'data was dropped' proves nothing: " + json.dumps(attachment)[:300])
    assert "data" not in attachment, (
        "the embedded PDF survived the redacting read; it carries the "
        "patient's name in its title (see the form_fill probe above)")
    assert _markers_in(json.dumps(body)) == set()


# ---------------------------------------------------------------------------
# Site 3: r6/smbp/routes.py — the report handler loads EVERY Observation in
# the tenant, filters by subject reference, and renders HTML or PDF.
# ---------------------------------------------------------------------------

@pytest.fixture
def smbp_session(client, app, tenant_headers, auth_headers):
    """enroll, then seed a marked upstream BP Observation for the subject."""
    enroll = client.post("/r6/smbp/enroll", headers=auth_headers,
                         json={"patient_ref": PROBE_PATIENT_REF,
                               "language": "en", "days": 14})
    assert enroll.status_code == 201, enroll.get_data(as_text=True)
    with app.app_context():
        _store(_marked_patient(), tenant_headers["X-Tenant-Id"])
        _store(_marked_bp_observation(), tenant_headers["X-Tenant-Id"])
    return enroll.get_json()["id"]


def test_smbp_html_report_carries_only_the_observation_timestamp(
        client, tenant_headers, smbp_session):
    """CHARACTERIZATION — one field of the unredacted read reaches the report.

    `report()` reads every tenant Observation and hands them to
    `build_report`, which keeps the numeric components plus
    `effectiveDateTime`. `render_html` HTML-escapes that timestamp and prints
    it in the "When" column, so an upstream string in `Observation.
    effectiveDateTime` reaches the clinician-facing report verbatim.

    The code `display`/`text`, the component displays and the note do NOT
    survive — the report is built from a fixed set of numeric fields. That is
    a property of `build_report`, not of a redaction call, so this pins it.
    """
    response = client.get("/r6/smbp/report/%s" % smbp_session,
                          headers=tenant_headers)
    assert response.status_code == 200, response.get_data(as_text=True)[:200]
    text = response.get_data(as_text=True)
    assert "148/92" in text, (
        "the seeded Observation never reached the report — the marker "
        "assertion below would pass while measuring nothing: " + text[:400])
    assert _markers_in(text) == {EFFECTIVE_MARKER}


def test_smbp_pdf_report_carries_only_the_observation_timestamp(
        client, tenant_headers, smbp_session):
    """The PDF branch of the same handler, which also persists a second
    DocumentReference. Same fixed field set, so the same single marker."""
    response = client.get("/r6/smbp/report/%s?format=pdf" % smbp_session,
                          headers=tenant_headers)
    assert response.status_code == 200, response.get_data(as_text=True)[:200]
    assert response.data[:4] == b"%PDF", response.get_data(as_text=True)[:200]
    text = pdf_text(response.data)
    assert "148/92" in text, (
        "the seeded Observation never reached the PDF — this probe is "
        "measuring nothing: " + text[:400])
    assert _markers_in(text) == {EFFECTIVE_MARKER}


# ---------------------------------------------------------------------------
# Site 4: r6/curatr.py:1097 — apply_fix returns the stored resource as
# `updated_resource`, and r6/routes.py:3118 `jsonify`s that result straight
# back to the caller.
# ---------------------------------------------------------------------------

def test_curatr_apply_fix_echoes_the_whole_stored_resource(
        client, app, tenant_headers, auth_headers):
    """LEAK — every free-text field of the resource leaves at curatr.py:1097.

    `$curatr-apply-fix` writes the approved field and then returns
    `resource.to_fhir_json()` as `updated_resource`; `r6/routes.py::
    curatr_apply_fix` returns that dict verbatim. Nothing between the read and
    the response calls `apply_redaction`, so the fields the fix did NOT touch
    — here `code.text`, `code.coding[0].display` and `note[0].text` — come
    back to the caller exactly as the upstream feed wrote them.

    The fix in this probe rewrites only `code.coding[0].code`, which is the
    shape curatr itself proposes for a retired ICD-9 code.
    """
    with app.app_context():
        resource_id = _store(_marked_condition(), tenant_headers["X-Tenant-Id"])

    response = client.post(
        "/r6/fhir/Condition/%s/$curatr-apply-fix" % resource_id,
        headers={**auth_headers, "X-Human-Confirmed": "true"},
        json={"fixes": [{"field_path": "Condition.code.coding[0].code",
                         "new_value": "E11.9"}],
              "patient_intent": "Updating from retired ICD-9 to ICD-10-CM"})
    assert response.status_code == 200, (
        "the probe never reached apply_fix — a 403/404 here would make the "
        "assertion below pass while measuring nothing: "
        + response.get_data(as_text=True)[:300])
    body = response.get_json()
    assert body["issues_fixed"] == 1, response.get_data(as_text=True)[:300]

    leaked = _markers_in(json.dumps(body["updated_resource"]))
    assert leaked == {COND_TEXT_MARKER, COND_DISPLAY_MARKER, COND_NOTE_MARKER}, (
        "the set of upstream free-text fields echoed by $curatr-apply-fix "
        "changed; if it shrank because redaction was added, delete this row "
        "and say so in the inventory")


def test_curatr_apply_fix_response_is_what_the_http_caller_sees(
        client, app, tenant_headers, auth_headers):
    """The escape is the HTTP body, not an internal return.

    Asserting on the whole serialized response (rather than on
    `body['updated_resource']`) is what makes this a boundary measurement: an
    MCP tool or agent calling this endpoint receives these bytes.
    """
    with app.app_context():
        resource_id = _store(_marked_condition(), tenant_headers["X-Tenant-Id"])

    response = client.post(
        "/r6/fhir/Condition/%s/$curatr-apply-fix" % resource_id,
        headers={**auth_headers, "X-Human-Confirmed": "true"},
        json={"fixes": [{"field_path": "Condition.code.coding[0].code",
                         "new_value": "E11.9"}],
              "patient_intent": "Updating from retired ICD-9 to ICD-10-CM"})
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    assert COND_TEXT_MARKER in response.get_data(as_text=True)
