"""Redaction sites that are only reachable through a multi-step flow.

`tests/test_redaction_coverage_inventory.py` measured the three sites you can
hit with one request and deliberately named these four as NOT covered:

    r6/actions/rails/form_fill.py   (Patient.name -> rendered intake PDF)
    r6/sdc/documents.py             (the persisted DocumentReference)
    r6/smbp/routes.py               (every tenant Observation -> BP report)
    r6/curatr.py                    ($curatr-apply-fix's updated_resource)

A fifth was added afterwards (#382), because it was never on #282's list of
eight and so was on neither file:

    r6/brief/routes.py              (every tenant Condition / MedicationRequest
                                     / Observation / Encounter -> the
                                     AppointmentBrief a patient and their
                                     clinic read)

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

    curatr.py:1096      LEAK, now closed — `$curatr-apply-fix` returned the
                        whole stored resource, every free-text field the fix
                        did not touch, in the HTTP response. The route
                        redacts the outbound copy as of this PR; the rows
                        below guard that and the ordering it depends on.
    form_fill.py:153    reaches a caller — the patient's own name, in the
                        title of the patient's own intake form
    smbp/routes.py      clean but for `effectiveDateTime`, which the report
                        echoes verbatim from the stored Observation
    sdc/documents.py:72 internal; its return value reaches nobody, and the
                        row it writes is redacted on the FHIR read path
    brief/routes.py:38  LEAK, LATENT — the brief renders upstream `code.text`,
                        `coding[].display`, `dosageInstruction.text`,
                        `valueString` and `medicationReference.display` into a
                        document meant for a clinician. Nothing reaches a
                        caller TODAY only because the handler is unreachable
                        (registered at a doubled path) and crashes when it is
                        reached (`R6Resource` has no `.resource`). Both are
                        pinned below; fixing either publishes the leak.

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

Not covered for the brief specifically:

- The CareAgents page. `careagents/app.py::brief` (line 654) renders whatever
  `fetch_appointment_brief` returns, and CareAgents' own tests fake the
  HealthClaw client. The probes here stop at HealthClaw's response bytes and
  parse them with the consumer's real parser; the rendered HTML was checked
  by hand against a running pair, not pinned by a test.
- The brief's own caps and filters: `_MAX_LABS = 10`, five visits,
  `clinicalStatus`-inactive Conditions and non-active MedicationRequests are
  all dropped before rendering. Nothing here bounds a tenant large enough for
  the caps to bite, so "12 markers arrive" is a floor, not a ceiling.
- `_obs_value`'s `valueCodeableConcept` branch (engine.py:88-92) reads a
  `text` and a `display` that no fixture here seeds.
- `_encounter_display`'s `meta.lastUpdated` fallback (engine.py:123).
- `BriefField.source_id` echoes the upstream resource id verbatim, and
  `apply_redaction` never touches `id`. An id is not free text, so it is not
  probed with a marker — but it is upstream-controlled and it does leave.
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

# Brief-only fields. The brief renders a label and a value per record and the
# value is a different field per section, so each gets its own marker.
BRIEF_DOSAGE_MARKER = "PHIBRIEFDOSAGEMARKER"
BRIEF_MED_DISPLAY_MARKER = "PHIBRIEFMEDDISPLAYMARKER"
BRIEF_MED_REF_MARKER = "PHIBRIEFMEDREFMARKER"
BRIEF_OBS_VALUE_MARKER = "PHIBRIEFOBSVALUEMARKER"
BRIEF_ENC_TEXT_MARKER = "PHIBRIEFENCTEXTMARKER"
BRIEF_ENC_DISPLAY_MARKER = "PHIBRIEFENCDISPLAYMARKER"
# `_code_text` / `_medication_display` return the FIRST field they find, so a
# resource carrying both text and display leaks only one of them. These sit in
# the losing field and are expected absent — which is why the fix has to cover
# both fields, not the one a fixture happens to use.
BRIEF_COND_SHADOWED_MARKER = "PHIBRIEFCONDSHADOWEDMARKER"
BRIEF_MED_SHADOWED_MARKER = "PHIBRIEFMEDSHADOWEDMARKER"
# Exactly 10 characters: `_effective_display` returns `dt[:10]`, so this is
# what an Observation.effectiveDateTime that is not a date gives up.
BRIEF_DATE_MARKER = "PHIBRIEFDT"

ALL_MARKERS = (
    NAME_MARKER, SUBJECT_LABEL_MARKER, OBS_DISPLAY_MARKER, OBS_TEXT_MARKER,
    MED_TEXT_MARKER, ALLERGY_TEXT_MARKER, COMPONENT_DISPLAY_MARKER,
    EFFECTIVE_MARKER, OBS_NOTE_MARKER, COND_TEXT_MARKER, COND_DISPLAY_MARKER,
    COND_NOTE_MARKER, BRIEF_DOSAGE_MARKER, BRIEF_MED_DISPLAY_MARKER,
    BRIEF_MED_REF_MARKER, BRIEF_OBS_VALUE_MARKER, BRIEF_ENC_TEXT_MARKER,
    BRIEF_ENC_DISPLAY_MARKER, BRIEF_COND_SHADOWED_MARKER,
    BRIEF_MED_SHADOWED_MARKER, BRIEF_DATE_MARKER,
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


def test_no_marker_is_a_substring_of_another():
    """The control for every `_markers_in` result in this file.

    `_markers_in` is a substring search, so a marker contained in another one
    is reported as present whenever the longer one leaks. That is not
    hypothetical: the first draft of the brief probes below used
    ...DISPLAY / ...DISPLAY2 and reported a leak from a field that was never
    read. Every attribution in this file depends on this holding.
    """
    nested = sorted((a, b) for a in ALL_MARKERS for b in ALL_MARKERS
                    if a != b and a in b)
    assert nested == [], (
        "these markers cannot be told apart by a substring search, so every "
        "row that names one of them is unattributable: %s" % nested)
    assert len(set(ALL_MARKERS)) == len(ALL_MARKERS), "duplicate marker"


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
# `updated_resource`, and r6/routes.py `jsonify`s that result back to the
# caller. It used to go out verbatim; the route now redacts that copy, AFTER
# the curation re-evaluation has scored the real one. Three rows: the leak is
# closed, the HTTP body is what was measured, and the ordering holds.
# ---------------------------------------------------------------------------

def _apply_the_icd9_fix(client, resource_id, auth_headers):
    """Drive `$curatr-apply-fix` with the fix curatr itself proposes for a
    retired ICD-9 code: rewrite `code.coding[0].code`, touch nothing else."""
    return client.post(
        "/r6/fhir/Condition/%s/$curatr-apply-fix" % resource_id,
        headers={**auth_headers, "X-Human-Confirmed": "true"},
        json={"fixes": [{"field_path": "Condition.code.coding[0].code",
                         "new_value": "E11.9"}],
              "patient_intent": "Updating from retired ICD-9 to ICD-10-CM"})


def test_curatr_apply_fix_returns_no_upstream_free_text(
        client, app, tenant_headers, auth_headers):
    """`updated_resource` carries none of the feed's free text.

    This leaked. `$curatr-apply-fix` wrote the approved field and returned
    `resource.to_fhir_json()` as `updated_resource`, which `r6/routes.py`
    jsonified verbatim — nothing between the read and the response called
    `apply_redaction`. So `code.text`, `code.coding[0].display` and
    `note[0].text`, none of which the fix touches, went back to the caller
    exactly as upstream wrote them. The realistic caller is the
    `curatr_apply_fix` MCP tool, so that text landed in a model's context;
    `note[].text` is the one that matters, because free-text clinical notes
    are where real feeds put names.

    What closed it: the route redacts the outbound copy just before
    `jsonify`. The fix's own value still comes back — redaction strips
    upstream free text, it does not gut the response — so this asserts that
    too, otherwise an endpoint that returned `{}` would pass.
    """
    with app.app_context():
        resource_id = _store(_marked_condition(), tenant_headers["X-Tenant-Id"])

    response = _apply_the_icd9_fix(client, resource_id, auth_headers)
    assert response.status_code == 200, (
        "the probe never reached apply_fix — a 403/404 here would make the "
        "assertions below pass while measuring nothing: "
        + response.get_data(as_text=True)[:300])
    body = response.get_json()
    assert body["issues_fixed"] == 1, response.get_data(as_text=True)[:300]

    updated = body["updated_resource"]
    assert updated["code"]["coding"][0]["code"] == "E11.9", (
        "the approved fix is not in the returned resource, so 'no markers' "
        "below proves nothing: " + json.dumps(updated)[:300])
    assert _markers_in(json.dumps(updated)) == set(), (
        "$curatr-apply-fix echoed upstream free text again — the fields are "
        "named by which marker came back")


def test_curatr_apply_fix_http_body_carries_no_upstream_free_text(
        client, app, tenant_headers, auth_headers):
    """The escape was the HTTP body, not an internal return.

    Asserting on the whole serialized response (rather than on
    `body['updated_resource']`) is what makes this a boundary measurement: an
    MCP tool or agent calling this endpoint receives these bytes. It also
    covers the sibling keys — `provenance` and `change_summary` are built
    from the request, not the feed, and this pins that they stay that way.
    """
    with app.app_context():
        resource_id = _store(_marked_condition(), tenant_headers["X-Tenant-Id"])

    response = _apply_the_icd9_fix(client, resource_id, auth_headers)
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    assert _markers_in(response.get_data(as_text=True)) == set()


def _text_only_condition():
    """A Condition whose `code` has a text label and NO coding.

    Redaction strips that text, which leaves `code` an empty dict. Curatr
    scores the two shapes differently — "no structured coding" is a warning,
    a missing `code` is critical — so this resource is what makes the
    ordering in `test_curatr_scores_the_unredacted_resource` observable.
    `clinicalStatus` carries an invalid code so there is a real fix to apply
    that does not touch `code`.
    """
    return {
        "resourceType": "Condition", "id": "probe-cond-2",
        "code": {"text": COND_TEXT_MARKER},
        "clinicalStatus": {"coding": [{
            "system": ("http://terminology.hl7.org/CodeSystem/"
                       "condition-clinical"),
            "code": "activ"}]},
        "verificationStatus": {"coding": [{
            "system": ("http://terminology.hl7.org/CodeSystem/"
                       "condition-ver-status"),
            "code": "confirmed"}]},
        "subject": {"reference": PROBE_PATIENT_REF},
        "note": [{"text": COND_NOTE_MARKER}],
    }


def test_curatr_scores_the_unredacted_resource(
        client, app, tenant_headers, auth_headers):
    """The redaction goes AFTER the curation re-evaluation, not before it.

    `r6/routes.py::curatr_apply_fix` feeds `result['updated_resource']` to
    `_curatr_engine.evaluate` to recompute `quality_score` and promote
    `curation_state`. Redacting before that would score stripped fields, and
    nothing else in the suite would notice — the score is just a number in
    the response, and for most resources redaction does not change it.

    So this uses a resource where it does. `code` here is text-only:
    unredacted that is one warning (0.8), redacted it is a missing `code`,
    which is critical (0.6). If the redaction is ever moved into
    `r6/curatr.py::apply_fix`, or above the promotion block, this row goes to
    0.6 and reddens.

    The persisted `quality_score` is checked too, because
    `_persist_curation_state` writes the same evaluation to the row that the
    consumer-facing curation views read.
    """
    with app.app_context():
        resource_id = _store(_text_only_condition(),
                             tenant_headers["X-Tenant-Id"])

    response = client.post(
        "/r6/fhir/Condition/%s/$curatr-apply-fix" % resource_id,
        headers={**auth_headers, "X-Human-Confirmed": "true"},
        json={"fixes": [{
            "field_path": "Condition.clinicalStatus.coding[0].code",
            "new_value": "active"}],
            "patient_intent": "Correcting an invalid clinical status"})
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    body = response.get_json()
    assert body["issues_fixed"] == 1, response.get_data(as_text=True)[:300]

    assert body["curation_state"] == "curated", (
        "the promotion block did not run, so the score below is not the one "
        "this row is about: " + response.get_data(as_text=True)[:300])
    assert body["quality_score"] == 0.8, (
        "the quality score was computed on a REDACTED resource. 0.6 means "
        "redaction ran before _curatr_engine.evaluate and the stripped "
        "`code.text` was read as a missing code")

    with app.app_context():
        row = R6Resource.query.filter_by(id=resource_id).first()
        assert row.quality_score == 0.8, (
            "the persisted score came from a redacted evaluation")

    # And the response itself is still redacted — the ordering fix must not
    # have been achieved by dropping the redaction.
    assert _markers_in(response.get_data(as_text=True)) == set()


# ---------------------------------------------------------------------------
# Site 5: r6/brief/routes.py — `_resources_for` (line 28-38) hands every
# Condition, MedicationRequest, Observation and Encounter in the tenant to
# `r6/brief/engine.py`, which reads `code.text` first and `coding[].display`
# second (engine.py:41-50) — the two fields CLAUDE.md names because real feeds
# put patient names in them. There is no `apply_redaction` call anywhere in
# `r6/brief/`.
#
# TWO DEFECTS SIT IN FRONT OF THAT, so nothing reaches a caller today:
#
#   D1 the handler is registered at /r6/fhir/fhir/AppointmentBrief. The
#      blueprint's url_prefix is already /r6/fhir (r6/routes.py:74) and the
#      route adds "/fhir/AppointmentBrief" (brief/routes.py:90). The URL
#      CareAgents builds (careagents/healthclaw.py:334) therefore lands on the
#      generic search route and 400s.
#   D2 `_resources_for` returns `r.resource`, and `R6Resource` has no such
#      attribute — it exposes `to_fhir_json()`. Any tenant holding one of the
#      four types 500s the handler.
#
# Both are pinned first, so that fixing either turns a green row red and this
# section has to be re-derived rather than silently starting to pass. The leak
# itself is then measured with D2 shimmed and D1 routed around, because "the
# fix publishes it" is the fact Dev needs.
# ---------------------------------------------------------------------------

BRIEF_PATH = "/r6/fhir/fhir/AppointmentBrief"          # where it IS registered
CAREAGENTS_BRIEF_PATH = "/r6/fhir/AppointmentBrief"    # where it is CALLED

LOINC_CHOL = "2093-3"          # in r6/terminology.py's static table
LOINC_UNLABELLED = "99999-9"   # not in it, and not a real LOINC code
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
ICD10_HTN = "I10"              # in the table
ICD10_UNLABELLED = "Z99.89"    # not in it
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
RXNORM_LISINOPRIL = "29046"    # in the table
RXNORM_UNLABELLED = "999999"   # not in it
SNOMED = "http://snomed.info/sct"


def _brief_seed() -> list[dict]:
    """Ten resources: every field the brief reads, plus the two it does not.

    Codes are split deliberately: `*_HTN` / `_CHOL` / `_LISINOPRIL` are in
    `r6/terminology.py`'s static table and `*_UNLABELLED` are not, so the
    redact-only projection below can report both outcomes.
    """
    return [
        # --- problems -----------------------------------------------------
        {"resourceType": "Condition", "id": "brief-cond-text",
         "clinicalStatus": {"coding": [{"code": "active"}]},
         "code": {"coding": [{"system": ICD10, "code": ICD10_HTN,
                              "display": BRIEF_COND_SHADOWED_MARKER}],
                  "text": COND_TEXT_MARKER},
         "subject": {"reference": PROBE_PATIENT_REF},
         "onsetDateTime": "2021-03-04",
         "note": [{"text": COND_NOTE_MARKER}]},
        {"resourceType": "Condition", "id": "brief-cond-display",
         "clinicalStatus": {"coding": [{"code": "active"}]},
         "code": {"coding": [{"system": ICD10, "code": ICD10_UNLABELLED,
                              "display": COND_DISPLAY_MARKER}]},
         "subject": {"reference": PROBE_PATIENT_REF},
         "onsetDateTime": "2019-07-11"},
        # --- medications --------------------------------------------------
        {"resourceType": "MedicationRequest", "id": "brief-med-text",
         "status": "active", "intent": "order",
         "medicationCodeableConcept": {
             "coding": [{"system": RXNORM, "code": RXNORM_LISINOPRIL,
                         "display": BRIEF_MED_SHADOWED_MARKER}],
             "text": MED_TEXT_MARKER},
         "dosageInstruction": [{"text": BRIEF_DOSAGE_MARKER}],
         "subject": {"reference": PROBE_PATIENT_REF}},
        {"resourceType": "MedicationRequest", "id": "brief-med-display",
         "status": "active", "intent": "order",
         "medicationCodeableConcept": {
             "coding": [{"system": RXNORM, "code": RXNORM_UNLABELLED,
                         "display": BRIEF_MED_DISPLAY_MARKER}]},
         "subject": {"reference": PROBE_PATIENT_REF}},
        {"resourceType": "MedicationRequest", "id": "brief-med-ref",
         "status": "active", "intent": "order",
         "medicationReference": {"reference": "Medication/brief-med-1",
                                 "display": BRIEF_MED_REF_MARKER},
         "subject": {"reference": PROBE_PATIENT_REF}},
        # --- labs -----------------------------------------------------------
        {"resourceType": "Observation", "id": "brief-obs-text",
         "status": "final",
         "code": {"coding": [{"system": LOINC, "code": LOINC_CHOL}],
                  "text": OBS_TEXT_MARKER},
         "subject": {"reference": PROBE_PATIENT_REF},
         "effectiveDateTime": "2026-01-15T08:00:00Z " + EFFECTIVE_MARKER,
         "valueQuantity": {"value": 244, "unit": "mg/dL"},
         "note": [{"text": OBS_NOTE_MARKER}]},
        {"resourceType": "Observation", "id": "brief-obs-display",
         "status": "final",
         "code": {"coding": [{"system": LOINC, "code": LOINC_UNLABELLED,
                              "display": OBS_DISPLAY_MARKER}]},
         "subject": {"reference": PROBE_PATIENT_REF},
         "effectiveDateTime": BRIEF_DATE_MARKER,
         "valueString": BRIEF_OBS_VALUE_MARKER},
        # --- visits ---------------------------------------------------------
        {"resourceType": "Encounter", "id": "brief-enc-text",
         "status": "finished",
         "type": [{"text": BRIEF_ENC_TEXT_MARKER}],
         "subject": {"reference": PROBE_PATIENT_REF},
         "period": {"start": "2026-02-01T09:00:00Z"}},
        {"resourceType": "Encounter", "id": "brief-enc-display",
         "status": "planned",
         "type": [{"coding": [{"system": SNOMED, "code": "185349003",
                               "display": BRIEF_ENC_DISPLAY_MARKER}]}],
         "subject": {"reference": PROBE_PATIENT_REF},
         "period": {"start": "2026-03-01T09:00:00Z"}},
        # --- the two the brief never loads ----------------------------------
        _marked_patient(),
        _marked_allergy(),
    ]


def _seed_the_brief(tenant_id) -> None:
    for resource in _brief_seed():
        _store(resource, tenant_id)


def _brief_headers(auth_headers) -> dict:
    """Exactly what CareAgents sends (careagents/healthclaw.py:134-137)."""
    return {**auth_headers, "X-Agent-Id": "careagents"}


def _parsed_sections(response) -> dict[str, list[dict]]:
    """Deserialize with the CONSUMER's parser, not a re-implementation.

    `careagents/app.py::_parse_brief_sections` is what turns this response
    into the rows rendered on the patient's brief page, so running the real
    one keeps the probe measuring what a patient sees rather than what a
    hand-written parser thinks the wire format is.
    """
    from careagents.app import _parse_brief_sections
    return _parse_brief_sections(response.get_json())


def _section_text(sections: dict[str, list[dict]], name: str) -> str:
    return json.dumps(sections.get(name, []))


# --- D1 and D2: why nothing leaks today ------------------------------------

def test_the_url_careagents_calls_does_not_reach_the_brief_handler(
        client, app, tenant_headers, auth_headers):
    """CHARACTERIZATION — D1. The brief is registered one segment too deep.

    `r6_blueprint` already carries url_prefix="/r6/fhir" (r6/routes.py:74) and
    `register_brief_routes` adds "/fhir/AppointmentBrief"
    (r6/brief/routes.py:90), so the handler lives at
    /r6/fhir/fhir/AppointmentBrief. `HealthClawClient.fetch_appointment_brief`
    builds f"{base}/r6/fhir/AppointmentBrief", which matches the generic
    `search_resources` rule instead — and "AppointmentBrief" is not in
    `R6Resource.SUPPORTED_TYPES`, so it 400s. That client swallows every
    non-200 and returns None, so the patient's brief page renders empty with
    no error anywhere.

    When this row goes red the leak measured below is live.
    """
    with app.app_context():
        _seed_the_brief(tenant_headers["X-Tenant-Id"])

    response = client.get(CAREAGENTS_BRIEF_PATH,
                          headers=_brief_headers(auth_headers))
    assert response.status_code == 400, (
        "the URL CareAgents calls now reaches a handler — re-derive this "
        "section, the brief leak below is no longer latent: "
        + response.get_data(as_text=True)[:300])
    body = response.get_json()
    assert body["resourceType"] == "OperationOutcome", \
        response.get_data(as_text=True)[:300]
    assert "not supported" in json.dumps(body).lower(), (
        "the 400 is not the 'unsupported resource type' one this row names, "
        "so it is measuring some other failure: "
        + response.get_data(as_text=True)[:300])
    assert _markers_in(response.get_data(as_text=True)) == set()


def test_the_registered_brief_path_crashes_on_any_stored_resource(
        client, app, tenant_headers, auth_headers):
    """CHARACTERIZATION — D2. `_resources_for` reads an attribute that is not
    there.

    `r6/brief/routes.py:38` returns `r.resource`; `R6Resource` (r6/models.py:33)
    has `resource_json`, `to_fhir_json()` and no `resource`. One row of any of
    the four types is enough. TESTING=True propagates the exception, so this
    asserts the raise rather than a 500 page — a tenant with data gets a 500
    in production, which is also why no probe here could have caught the
    redaction gap by driving the endpoint alone.
    """
    with app.app_context():
        _store(_brief_seed()[0], tenant_headers["X-Tenant-Id"])

    with pytest.raises(AttributeError, match="has no attribute 'resource'"):
        client.get(BRIEF_PATH, headers=_brief_headers(auth_headers))


def test_the_brief_answers_for_a_tenant_with_no_records(
        client, tenant_headers, auth_headers):
    """The empty-tenant path is the only one that works end to end today.

    It is also the control for D2: the handler, the audit call and the
    serializer are all fine, so the crash above is the resource read and
    nothing else.
    """
    response = client.get(BRIEF_PATH, headers=_brief_headers(auth_headers))
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    sections = _parsed_sections(response)
    assert set(sections) == {"problems", "medications", "labs", "care-gaps",
                             "visits"}, json.dumps(sections)[:300]
    assert all(rows == [] for rows in sections.values()), \
        json.dumps(sections)[:300]


# --- the leak, with D2 shimmed and D1 routed around ------------------------

@pytest.fixture
def brief_reachable(app, tenant_headers, monkeypatch):
    """Supply the one attribute `_resources_for` is missing, seed, and stop.

    The shim is `R6Resource.resource -> to_fhir_json()`: the stored resource
    with its envelope, which is what every other read path in this codebase
    starts from and what any plausible fix for D2 will produce. Production
    `_resources_for` — the line #382 is about — runs unchanged, so the rows
    below measure the real read, the real engine and the real serializer.

    Requests still go to BRIEF_PATH, the path the route is really registered
    at, so nothing here depends on D1 being fixed one way or the other.
    """
    monkeypatch.setattr(R6Resource, "resource",
                        property(lambda self: self.to_fhir_json()),
                        raising=False)
    with app.app_context():
        _seed_the_brief(tenant_headers["X-Tenant-Id"])


def _get_brief(client, auth_headers):
    response = client.get(BRIEF_PATH, headers=_brief_headers(auth_headers))
    assert response.status_code == 200, (
        "the brief request failed, so every marker assertion against it "
        "would pass while measuring nothing: "
        + response.get_data(as_text=True)[:400])
    return response


def test_brief_problems_carry_the_conditions_own_free_text(
        client, auth_headers, brief_reachable):
    """LEAK — `Condition.code.text`, then `code.coding[].display`.

    `engine.py::_code_text` (line 41-50) reads `code.text` first and falls
    back to the first `coding[].display`, and `build_problems` puts the result
    in `BriefField.label`, which `routes.py::_field_to_dict` serializes into
    the response. Neither field ever met `apply_redaction` — there is no call
    to it anywhere under `r6/brief/`.

    Both fixtures are here because which field leaks depends on the feed: the
    resource carrying `text` leaks the text and NOT its `display`
    (BRIEF_COND_SHADOWED_MARKER), the one carrying only `display` leaks the
    display. A fix that covers one field and not the other closes half of it.
    """
    sections = _parsed_sections(_get_brief(client, auth_headers))
    problems = sections["problems"]
    assert len(problems) == 2, (
        "the seeded active Conditions did not reach the brief, so the marker "
        "assertions below prove nothing: " + json.dumps(sections)[:400])

    text = _section_text(sections, "problems")
    assert COND_TEXT_MARKER in text, "Condition.code.text did not reach the brief"
    assert COND_DISPLAY_MARKER in text, \
        "Condition.code.coding[].display did not reach the brief"
    assert BRIEF_COND_SHADOWED_MARKER not in text, (
        "_code_text returned BOTH text and display for one resource; this "
        "row's attribution assumes it returns the first it finds")
    assert COND_NOTE_MARKER not in text, (
        "Condition.note reached the brief — the engine renders a fixed field "
        "set, and a new field in it needs its own row here")


def test_brief_medications_carry_the_feeds_name_and_dosage_free_text(
        client, auth_headers, brief_reachable):
    """LEAK — `medicationCodeableConcept.text`, `.coding[].display`,
    `medicationReference.display` (engine.py:96-108) and
    `dosageInstruction[].text` (engine.py:169-172).

    The dosage line is the one to look at twice: it is the field a feed uses
    for free-text sig ("take 1 tablet twice daily, per Dr Alvarez, call the
    office at ..."), it is the brief's `value` rather than its label, and
    `apply_redaction` strips it outright — so it is both the widest exposure
    here and the field a redact-only fix silently deletes.
    """
    sections = _parsed_sections(_get_brief(client, auth_headers))
    meds = sections["medications"]
    assert len(meds) == 3, (
        "the seeded active MedicationRequests did not reach the brief: "
        + json.dumps(sections)[:400])

    text = _section_text(sections, "medications")
    assert MED_TEXT_MARKER in text, "medicationCodeableConcept.text leaked?"
    assert BRIEF_MED_DISPLAY_MARKER in text, \
        "medicationCodeableConcept.coding[].display did not reach the brief"
    assert BRIEF_MED_REF_MARKER in text, \
        "medicationReference.display did not reach the brief"
    assert BRIEF_DOSAGE_MARKER in text, \
        "dosageInstruction[].text did not reach the brief"
    assert BRIEF_MED_SHADOWED_MARKER not in text, (
        "_medication_display returned both text and display for one resource")


def test_brief_labs_carry_the_feeds_label_and_free_text_value(
        client, auth_headers, brief_reachable):
    """LEAK — `Observation.code.text` / `.coding[].display` and `valueString`.

    `valueString` is the free-text result field: a feed that sends a narrative
    result ("Positive — called patient at home, spoke to her husband") puts it
    here, and `_obs_value` (engine.py:79-93) returns it verbatim as the
    brief's value.

    `note[].text` does not survive, and `effectiveDateTime` gets its own row.
    """
    sections = _parsed_sections(_get_brief(client, auth_headers))
    labs = sections["labs"]
    assert len(labs) == 2, (
        "the seeded Observations did not reach the brief: "
        + json.dumps(sections)[:400])

    text = _section_text(sections, "labs")
    assert OBS_TEXT_MARKER in text, "Observation.code.text did not reach it"
    assert OBS_DISPLAY_MARKER in text, \
        "Observation.code.coding[].display did not reach it"
    assert BRIEF_OBS_VALUE_MARKER in text, \
        "Observation.valueString did not reach it"
    assert OBS_NOTE_MARKER not in text, (
        "Observation.note reached the brief — new field, new row")


def test_brief_visits_carry_the_encounter_type_free_text(
        client, auth_headers, brief_reachable):
    """LEAK — `Encounter.type[].text` and `.type[].coding[].display`
    (engine.py:111-126), rendered as "<label> (<date>)".

    Encounter type is where a feed writes the visit reason, and a visit reason
    is as identifying as a diagnosis.
    """
    sections = _parsed_sections(_get_brief(client, auth_headers))
    assert len(sections["visits"]) == 2, (
        "the seeded Encounters did not reach the brief: "
        + json.dumps(sections)[:400])

    text = _section_text(sections, "visits")
    assert BRIEF_ENC_TEXT_MARKER in text, "Encounter.type[].text did not reach it"
    assert BRIEF_ENC_DISPLAY_MARKER in text, \
        "Encounter.type[].coding[].display did not reach it"


def test_brief_truncates_effective_date_time_but_does_not_sanitize_it(
        client, auth_headers, brief_reachable):
    """CHARACTERIZATION — the bound on `effectiveDateTime` is positional.

    `_effective_display` (engine.py:66-76) returns `dt[:10]`. That is why the
    marker appended to a real timestamp does NOT arrive — and why the
    10-character marker seeded as the whole field DOES. The field is echoed,
    not validated; a feed whose `effectiveDateTime` is not a date gives up its
    first ten characters. `apply_redaction` does not touch this field either
    (`_DATE_KEYS`, r6/redaction.py:141-144, does not list it), which is the
    same finding the SMBP report carries above.
    """
    sections = _parsed_sections(_get_brief(client, auth_headers))
    text = _section_text(sections, "labs")
    assert "244 mg/dL (2026-01-15)" in text, (
        "the dated Observation did not render its value+date, so the "
        "truncation claim below is unmeasured: " + text[:400])
    assert EFFECTIVE_MARKER not in text, (
        "the tail of effectiveDateTime survived — truncation to 10 chars is "
        "the only thing bounding this field")
    assert BRIEF_DATE_MARKER in text, (
        "the 10-character effectiveDateTime did not arrive, so this row is "
        "not measuring the echo it claims to")


def test_brief_never_loads_patient_or_allergy_intolerance(
        client, auth_headers, brief_reachable):
    """CLEAN, by omission — and the omission is itself worth reading.

    `appointment_brief` (routes.py:108-111) loads four resource types.
    `Patient` is not one of them (`_care_gap_result` passes `patient=None`),
    so `Patient.name` cannot reach the brief; `AllergyIntolerance` is not one
    either, so a seeded allergy is absent — as is any allergies section.

    A pre-appointment brief with no allergy list is a product decision this
    row does not litigate, but it does record it: the clinician reading this
    document gets problems, medications, labs and visits, and nothing about
    what the patient reacts to. Cross-reference the NKA rule in CLAUDE.md —
    "no known allergies" is never inferred, and an absent section is exactly
    the kind of silence a reader infers it from.
    """
    response = _get_brief(client, auth_headers)
    sections = _parsed_sections(response)
    assert "allergies" not in sections, (
        "the brief grew an allergies section; it needs its own leak row: "
        + json.dumps(sections)[:400])

    body = response.get_data(as_text=True)
    assert NAME_MARKER not in body, "Patient.name.family reached the brief"
    assert SUBJECT_LABEL_MARKER not in body, "Patient.name.text reached the brief"
    assert ALLERGY_TEXT_MARKER not in body, \
        "AllergyIntolerance.code.text reached the brief"


def test_brief_care_gaps_section_is_always_empty(
        client, auth_headers, brief_reachable):
    """CLEAN because it never renders — for two independent reasons.

    Filed as a probe rather than a redaction row because it is why "no marker
    in care-gaps" proves nothing about redaction: nothing at all is there.
    The two causes are pinned separately in the next row; either one alone
    keeps this section empty, so fixing one and shipping would look like
    progress and change nothing a patient sees.
    """
    sections = _parsed_sections(_get_brief(client, auth_headers))
    assert sections["care-gaps"] == [], (
        "the care-gaps section populated — it needs a redaction row of its "
        "own now: " + json.dumps(sections["care-gaps"])[:400])


def test_brief_care_gaps_are_empty_for_two_independent_reasons():
    """The two causes, separately, so neither hides behind the other.

    C1 `_care_gap_result` (routes.py:69-83) calls `evaluate_care_gaps` with
       `patient=None`. Every rule is age- or sex-gated, so with no
       demographics all of them come back "indeterminate", and
       `build_consumer_summary` only emits lines for "due" / "up_to_date".
       The result is an empty summary before any brief code runs.
    C2 `build_care_gaps` (engine.py:212-226) then reads `consumer["due"]`.
       `build_consumer_summary` returns `{"lines": [...], "note": ...}`
       (r6/caregaps/report.py:43-48) and has no "due" key at all.

    Measured, not argued: the same records with a patient produce 7 due rules.
    """
    from r6.brief.engine import build_care_gaps
    from r6.brief.routes import _care_gap_result
    from r6.caregaps.evaluate import evaluate_care_gaps
    from r6.caregaps.report import build_consumer_summary

    conditions = [_marked_condition()]
    patient = {"resourceType": "Patient", "gender": "female",
               "birthDate": "1962-03-04"}
    with_patient = build_consumer_summary(evaluate_care_gaps(
        patient=patient, conditions=conditions, observations=[],
        as_of="2026-08-04"))
    assert len(with_patient["lines"]) > 0, (
        "the evaluator finds nothing for these records even WITH "
        "demographics, so this row cannot show what dropping them costs")

    # C1: the brief's own call, with the patient it actually passes.
    result = _care_gap_result(conditions, [])
    assert result["consumer"]["lines"] == [], (
        "patient=None no longer empties the summary — re-derive C1: "
        + json.dumps(result)[:300])

    # C2: even handed a populated summary, the reader looks for another key.
    assert build_care_gaps({"consumer": with_patient}) == [], (
        "build_care_gaps now reads the key build_consumer_summary emits — "
        "C2 is fixed and this row needs re-deriving")


def test_brief_response_carries_exactly_this_marker_set(
        client, auth_headers, brief_reachable):
    """Pins the whole boundary, the way the form_fill row does.

    Asserting the set EXACTLY is what makes a future change visible: a new
    field rendered from an upstream record fails here rather than shipping,
    and a fix that closes some fields but not others reports precisely which
    ones are left.
    """
    response = _get_brief(client, auth_headers)
    assert _markers_in(response.get_data(as_text=True)) == {
        COND_TEXT_MARKER, COND_DISPLAY_MARKER,
        MED_TEXT_MARKER, BRIEF_MED_DISPLAY_MARKER, BRIEF_MED_REF_MARKER,
        BRIEF_DOSAGE_MARKER,
        OBS_TEXT_MARKER, OBS_DISPLAY_MARKER, BRIEF_OBS_VALUE_MARKER,
        BRIEF_DATE_MARKER,
        BRIEF_ENC_TEXT_MARKER, BRIEF_ENC_DISPLAY_MARKER,
    }


def test_no_redaction_call_exists_anywhere_under_r6_brief(
        client, auth_headers, brief_reachable):
    """Evidence for the "never met apply_redaction" claim, not a restatement.

    The rows above show upstream text arriving; this shows there is no guard
    to have failed. If a call appears, this row goes red and the section has
    to be re-measured — which is the point, because a redaction call added in
    the wrong place (before the terminology re-label) is the #376 shape the
    projection rows below are about.
    """
    import inspect
    from r6.brief import engine as brief_engine
    from r6.brief import routes as brief_routes_mod
    source = (inspect.getsource(brief_routes_mod)
              + inspect.getsource(brief_engine))
    assert "apply_redaction" not in source, (
        "r6/brief/ now calls apply_redaction — re-derive this whole section")


# --- the trap: what a redact-only fix would render --------------------------

@pytest.fixture
def brief_redacted(app, tenant_headers, monkeypatch):
    """The candidate one-line fix, run for real: redact in `_resources_for`.

    Substituting the whole function (rather than shimming `.resource` as
    `brief_reachable` does) is deliberate — this fixture is not measuring
    today's code, it is measuring what the obvious fix produces, so that the
    "Unknown" question is answered with a response body instead of an
    argument. Everything downstream — the engine, the serializer, the HTTP
    boundary and CareAgents' own parser — is real.

    The runtime terminology resolver is forced off: it is opt-in per
    deployment (`TERMINOLOGY_LOOKUP_ENABLED`), budgeted, and returns None on
    any failure, so a fix may not depend on it. These rows report what the
    STATIC table in r6/terminology.py guarantees.
    """
    from r6 import terminology_resolver
    from r6.brief import routes as brief_routes_mod
    from r6.redaction import apply_redaction

    monkeypatch.setattr(terminology_resolver, "resolve",
                        lambda system, code: None)

    def _redacting_resources_for(tenant_id, resource_type):
        rows = (R6Resource.query
                .filter(R6Resource.tenant_id == tenant_id,
                        R6Resource.resource_type == resource_type,
                        R6Resource.is_deleted.is_(False))
                .all())
        return [apply_redaction(r.to_fhir_json()) for r in rows]

    monkeypatch.setattr(brief_routes_mod, "_resources_for",
                        _redacting_resources_for)
    with app.app_context():
        _seed_the_brief(tenant_headers["X-Tenant-Id"])


def test_redact_only_fix_relabels_the_codes_the_table_knows(
        client, auth_headers, brief_redacted):
    """PROJECTION — for codes IN r6/terminology.py, redaction alone is enough.

    `apply_redaction` already calls `label_codings` after stripping
    (r6/redaction.py:36), so a recognised coding comes back with a
    server-derived `display` and, because the CodeableConcept has no `text`
    left, a server-derived `text` too — which is the field `_code_text` reads
    first. No second call is needed for these.
    """
    sections = _parsed_sections(_get_brief(client, auth_headers))
    labels = {row["label"] for rows in sections.values() for row in rows}
    assert "High blood pressure (essential hypertension)" in labels, labels
    assert "Lisinopril" in labels, labels
    assert "Cholesterol (total)" in labels, labels


def test_redact_only_fix_renders_unknown_for_everything_else(
        client, auth_headers, brief_redacted):
    """PROJECTION — the #376 shape, measured. This is the answer to "is the
    fix one line?": no, not on its own.

    For a code the static table does not carry, redaction removes the only
    label the record had and nothing puts one back, so `_code_text` returns
    its literal fallback and the clinician reads "Unknown". The same happens
    to a medication carried as `medicationReference.display` even when its
    RxNorm code IS known, because `label_codings` labels `coding` arrays and a
    Reference has none — so `Unknown medication` can appear for a drug the
    server could have named. Encounters fall back to "Visit".

    How often is "the table does not carry it"? Measured on a live MEDENT
    import, 2026-08-04 (r6/terminology_resolver.py:12-19): 1 of 15 distinct
    ICD-10-CM codes and 0 of 11 SNOMED codes had a label — and the static
    table contains no SNOMED entries at all, which is what
    `test_the_static_table_has_no_snomed_entries` below pins.

    "Unknown" is indistinguishable from "no data" to the reader, which is the
    hole #376 is about: a document full of Unknown, handed to a clinician,
    with nothing saying whether the record was missing or stripped.
    """
    sections = _parsed_sections(_get_brief(client, auth_headers))
    labels = {row["label"] for rows in sections.values() for row in rows}
    assert "Unknown" in labels, (
        "the unlabelled Condition/Observation did not fall back to Unknown, "
        "so this row is not measuring the trap: %s" % labels)
    assert "Unknown medication" in labels, labels
    assert any(label.startswith("Visit (") for label in labels), labels

    med_rows = {row["sourceId"]: row["label"]
                for row in sections["medications"]}
    assert med_rows["brief-med-ref"] == "Unknown medication", (
        "a medicationReference.display drug is expected to lose its name "
        "entirely under a redact-only fix: %s" % med_rows)


def test_redact_only_fix_deletes_the_dosage_line(
        client, auth_headers, brief_redacted):
    """PROJECTION — redaction strips `dosageInstruction[].text` too.

    `_redact_fields` pops `text` from every non-resource dict
    (r6/redaction.py:67-70), and a dosageInstruction is one. So every
    medication row's value becomes the placeholder "See record for dosage" —
    for all three seeded medications, including the one whose sig was a plain
    "take one tablet daily" with nothing patient-specific in it.

    A brief that names three drugs and gives no dose for any of them is worse
    than useless at the appointment it is written for. This is the second half
    of the "one line or two" question, and it is not answered by relabelling
    codes: no code table can restore a sig.
    """
    sections = _parsed_sections(_get_brief(client, auth_headers))
    values = {row["value"] for row in sections["medications"]}
    assert values == {"See record for dosage"}, (
        "the dosage projection changed; re-derive it: %s" % values)


def test_redact_only_fix_does_not_close_the_effective_date_echo(
        client, auth_headers, brief_redacted):
    """PROJECTION — one leak survives the redact-only fix.

    `effectiveDateTime` is not in `_FREE_TEXT_KEYS` and not in `_DATE_KEYS`
    (r6/redaction.py:137-144), so redaction passes it through untouched and
    the brief still echoes its first ten characters. Whatever else the fix
    does, this field needs handling of its own.
    """
    response = _get_brief(client, auth_headers)
    assert _markers_in(response.get_data(as_text=True)) == {BRIEF_DATE_MARKER}, (
        "the redact-only projection's residue changed — re-derive which "
        "fields survive: %s"
        % sorted(_markers_in(response.get_data(as_text=True))))


def test_the_static_table_has_no_snomed_entries():
    """The evidence behind "SNOMED-coded records become Unknown".

    `r6/terminology.py` defines the SNOMED system URI and its aliases but has
    no SNOMED rows, so every SNOMED-coded Condition, AllergyIntolerance and
    Encounter type is a miss. Pinned as a number rather than a claim, because
    the fix's cost depends on it: with the resolver off, a SNOMED-coded
    problem list redacts to a page of "Unknown".
    """
    from r6 import terminology
    snomed = [key for key in terminology._LABELS if key[0] == terminology.SNOMED]
    assert snomed == [], (
        "the static table grew SNOMED labels — the Unknown projection above "
        "is now optimistic and should be re-measured: %s" % snomed[:10])
