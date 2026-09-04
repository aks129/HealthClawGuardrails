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

import ast
import base64
import json
import re
import textwrap
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
#: The brief reads medicationCodeableConcept.coding[].display when there
#: is no .text, so the two need separate markers to tell which arrived.
MED_DISPLAY_MARKER = "PHIMEDDISPLAYMARKER"
ALLERGY_TEXT_MARKER = "PHIALLERGYTEXTMARKER"
COMPONENT_DISPLAY_MARKER = "PHICOMPONENTDISPLAYMARKER"
EFFECTIVE_MARKER = "PHIEFFECTIVEMARKER"
OBS_NOTE_MARKER = "PHIOBSNOTEMARKER"
COND_TEXT_MARKER = "PHICONDTEXTMARKER"
COND_DISPLAY_MARKER = "PHICONDDISPLAYMARKER"
COND_NOTE_MARKER = "PHICONDNOTEMARKER"

ALL_MARKERS = (
    NAME_MARKER, SUBJECT_LABEL_MARKER, OBS_DISPLAY_MARKER, OBS_TEXT_MARKER,
    MED_TEXT_MARKER, MED_DISPLAY_MARKER, ALLERGY_TEXT_MARKER,
    COMPONENT_DISPLAY_MARKER,
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

def _confirm_the_form(client, tenant_headers, auth_headers):
    """propose -> commit -> review -> confirm.

    Returns (action_id, confirm_response). Split out of `_download_the_form`
    because the confirm response is an exit in its own right: a completed
    action is answered with `ProposedAction.to_dict()`
    (r6/actions/routes.py:169), which carries `outcome_summary` verbatim —
    so whatever the executor puts in its outcome goes on the wire.

    Asserts each hop, because a 4xx anywhere would leave the caller asserting
    markers against an error page.
    """
    from r6.actions.confirmations import ACTION_APPROVAL_AUDIENCE
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
    return action_id, confirm


def _download_the_form(client, app, tenant_headers, auth_headers):
    """`_confirm_the_form`, then GET the signed link.

    Returns (docref_id, pdf_bytes).
    """
    from r6.actions.models import ProposedAction
    action_id, _confirm = _confirm_the_form(client, tenant_headers,
                                            auth_headers)

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

def _strings_in(value):
    """Every string anywhere in a parsed JSON value, nested JSON included.

    `outcome_summary` is a JSON document stored as a string inside another
    JSON document, so a walker that stops at the first string never reaches
    the outcome's own fields.
    """
    if isinstance(value, str):
        yield value
        try:
            nested = json.loads(value)
        except (TypeError, ValueError):
            return
        if isinstance(nested, (dict, list)):
            yield from _strings_in(nested)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_in(item)


def _decoded_pdfs(value):
    """Every base64 string anywhere in `value` that decodes to a PDF.

    A property, not a phrase: it does not care which key carried the bytes,
    what that key is called, or how deep the JSON is nested. It also cannot
    be defeated by the thing that defeats a marker search — reportlab writes
    Flate streams, so the patient's name is not present as text in the base64
    OR in the decoded bytes until `pdf_text` decompresses them.
    """
    for text in _strings_in(value):
        if len(text) < 64:
            continue
        try:
            blob = base64.b64decode(text, validate=True)
        except Exception:  # noqa: BLE001 — not base64; nothing to decode
            continue
        if blob[:4] == b"%PDF":
            yield blob


def test_the_document_detector_finds_a_base64_pdf():
    """Control for the probe below, in the exact shape of its mutation.

    `_decoded_pdfs` has to reach through JSON-inside-JSON and then through
    Flate compression. If it cannot, "no document in the confirm response" is
    a false negative, which is the defect class this whole file is about.
    """
    from r6.sdc.pdf import render_questionnaire_response_pdf
    rendered = render_questionnaire_response_pdf(
        {"resourceType": "QuestionnaireResponse", "status": "completed",
         "item": []},
        subject_label=SUBJECT_LABEL_MARKER)
    body = {"id": "act-1", "status": "completed",
            "outcome_summary": json.dumps({
                "document_reference_id": "doc-1",
                "document": {"resourceType": "DocumentReference",
                             "content": [{"attachment": {
                                 "contentType": "application/pdf",
                                 "data": base64.b64encode(rendered).decode(),
                             }}]}})}

    assert SUBJECT_LABEL_MARKER not in json.dumps(body), (
        "the marker is readable as text in the response, so this control "
        "cannot distinguish a working detector from a marker search")
    found = list(_decoded_pdfs(body))
    assert len(found) == 1, (
        "the detector did not find the embedded PDF; every 'no document "
        "left' assertion below is unmeasured")
    assert SUBJECT_LABEL_MARKER in pdf_text(found[0])


def test_the_confirm_response_carries_no_rendered_document(
        client, app, tenant_headers, auth_headers, action_registry,
        form_fill_ready):
    """The exit `persist_intake_document`'s return value would leave by.

    form_fill hands the executor's `outcome` dict to the state machine, which
    stores it as `outcome_summary` and answers the confirm POST with
    `to_dict()` — outcome included. So "the return value reaches nobody"
    holds only while the outcome carries ids, not the object.

    Two assertions, deliberately different in kind:
      (a) the PROPERTY — nothing anywhere in the response body decodes to a
          PDF, whatever key it arrives under;
      (b) the named pin — the outcome's key set, exactly, so a new field of
          any kind has to be added here on purpose.

    MUTATION (run 2026-09-04, both directions, see PR): add
    `'document': docref,` to the success outcome in
    `r6/actions/rails/form_fill.py` -> red on both. Under it this route
    answers 200 with a base64 PDF whose title is the patient's name.
    """
    _action_id, confirm = _confirm_the_form(client, tenant_headers,
                                            auth_headers)
    body = confirm.get_json()

    # Non-vacuity: the flow really completed and really produced a document.
    assert body["status"] == "completed", confirm.get_data(as_text=True)[:300]
    outcome = json.loads(body["outcome_summary"])
    assert outcome.get("document_reference_id"), (
        "no document was persisted, so 'the document did not leave' is not a "
        "measurement: " + json.dumps(outcome)[:300])

    leaked = [pdf_text(blob) for blob in _decoded_pdfs(body)]
    assert not leaked, (
        "the rendered intake PDF left through the confirm response; it "
        "carries the patient's name and date of birth. markers found: %s"
        % sorted({m for text in leaked for m in _markers_in(text)}))

    assert set(outcome) == {"document_reference_id", "delivery_link",
                            "questionnaire_response_id"}, (
        "the form-fill outcome grew a field (%s). Everything in it goes on "
        "the wire in the confirm response — add it here deliberately, with a "
        "probe for whatever it carries" % sorted(outcome))

    assert _markers_in(confirm.get_data(as_text=True)) == set()


def test_persist_intake_document_return_value_reaches_no_caller():
    """The static half: form_fill touches nothing but the id.

    `grep -rn persist_intake_document --include='*.py'` finds exactly one
    production caller, and this pins what that caller does with the returned
    dict.

    #630 F1 found the docstring here claiming the "nothing escapes"
    statement above "stops being true loudly". It did not. The assertion was
    a regex for `docref[...]` subscripts, so passing the whole dict BY NAME —
    `outcome={'document': docref, ...}` — added no subscript and the row
    stayed green, along with the rest of the suite. The AST assertion below
    is the repair: it counts every LOAD of the name, not every subscript of
    it. `test_the_confirm_response_carries_no_rendered_document` above is the
    behavioural half, and is the one that would survive the function being
    rewritten.
    """
    import inspect
    from r6.actions.rails import form_fill
    source = inspect.getsource(form_fill.FormFillExecutor.execute)
    uses = set(re.findall(r"docref\[([^\]]+)\]", source))
    assert uses == {"'id'"}, (
        "form_fill now reads more than the id off persist_intake_document's "
        "return value (%s); documents.py:72 may now be an exit point and "
        "needs its own probe" % sorted(uses))

    tree = ast.parse(textwrap.dedent(source))
    loads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Name) and n.id == "docref"
             and isinstance(n.ctx, ast.Load)]
    id_subscripts = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Subscript)
                     and isinstance(n.value, ast.Name)
                     and n.value.id == "docref"
                     and isinstance(n.slice, ast.Constant)
                     and n.slice.value == "id"]
    assert len(id_subscripts) == 1, (
        "expected exactly one docref['id'] read; found %d, so the shape this "
        "row measures changed" % len(id_subscripts))
    assert len(loads) == 1, (
        "`docref` is read %d times in execute() but subscripted for 'id' "
        "once — the dict itself is being passed somewhere, and every field "
        "persist_intake_document built (including the base64 PDF) travels "
        "with it. Lines: %s"
        % (len(loads), sorted({n.lineno for n in loads})))


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
# AppointmentBrief (#382). Not on #282's list of eight, so neither this file
# nor the inventory has ever measured it.
#
# Until #434 the route registered at /r6/fhir/fhir/AppointmentBrief and no
# client could reach it (#386), so the unredacted read below was latent.
# Fixing the path armed it. That is why this probe is being added now rather
# than with the rest of #282: the leak did not change, its reachability did.
# ---------------------------------------------------------------------------

BRIEF_URL = "/r6/fhir/AppointmentBrief"


def _marked_medication_request():
    """`_code_text` reads medicationCodeableConcept for MedicationRequest."""
    return {
        "resourceType": "MedicationRequest", "id": "probe-medreq-1",
        "status": "active", "intent": "order",
        "medicationCodeableConcept": {
            "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                        "code": "860975", "display": MED_DISPLAY_MARKER}],
            "text": MED_TEXT_MARKER},
        "subject": {"reference": PROBE_PATIENT_REF},
    }


def _marked_active_condition():
    """`build_problems` keeps only conditions whose clinicalStatus is active
    (engine.py:199). The shared `_marked_condition` has no clinicalStatus, so
    seeding it alone leaves the problems section EMPTY and every assertion
    about Condition labelling passes while measuring nothing."""
    condition = dict(_marked_condition())
    condition["id"] = "probe-cond-brief-1"
    condition["clinicalStatus"] = {"coding": [{
        "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
        "code": "active"}]}
    return condition


@pytest.fixture
def brief_resources(app, tenant_headers):
    with app.app_context():
        _store(_marked_patient(), tenant_headers["X-Tenant-Id"])
        _store(_marked_active_condition(), tenant_headers["X-Tenant-Id"])
        _store(_marked_medication_request(), tenant_headers["X-Tenant-Id"])


def test_the_brief_probe_actually_reaches_the_brief(
        client, tenant_headers, brief_resources):
    """Control. A 404 or a 400 here makes every marker assertion below pass
    while measuring nothing — the failure mode #386 hid behind for weeks."""
    response = client.get(BRIEF_URL, headers=tenant_headers)
    assert response.status_code == 200, response.get_data(as_text=True)[:400]


def test_appointment_brief_carries_no_upstream_free_text(
        client, tenant_headers, brief_resources):
    """#382 closed: the brief reads REDACTED resources.

    `_resources_for` now returns `apply_redaction(r.to_fhir_json())`. That
    fixes the #391 crash (`r.resource` is not an attribute) and the missing
    redaction in the SAME change, because repairing the attribute alone turns
    a 500 into a leak — the crash was the only thing preventing it.

    MUTATION: drop `apply_redaction` and keep `to_fhir_json` -> COND_TEXT and
    MED_TEXT arrive and this goes red.
    """
    response = client.get(BRIEF_URL, headers=tenant_headers)
    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    text = response.get_data(as_text=True)
    assert _markers_in(text) == set(), (
        "upstream free text reached the brief: %s" % sorted(_markers_in(text)))


def test_a_recognised_code_still_reads_as_a_name(
        client, tenant_headers, brief_resources):
    """The positive half, and the reason this is not a one-line fix.

    Redaction strips `text` and `display`; `r6/terminology.py` puts back a
    label keyed by code. Asserting only that the markers are gone is what let
    #376 hide — a document full of "Unknown" passes a leak check perfectly.

    RxNorm 860975 is in the terminology table, so the medication line must
    carry the SERVER's name for it. That the marker is absent is asserted
    above; that a real name is present is asserted here, and neither
    assertion can stand in for the other.
    """
    response = client.get(BRIEF_URL, headers=tenant_headers)
    text = response.get_data(as_text=True)
    assert "Metformin" in text, (
        "the medication degraded to an unnamed entry: redaction stripped the "
        "feed's text and nothing put a server-derived label back, which is "
        "the #376 hole #382 warns the naive fix creates: " + text[:600])
    assert MED_TEXT_MARKER not in text


def test_an_unnameable_code_is_not_reported_as_no_data(app, tenant_headers):
    """Three states, not two. A code we hold but cannot label must not read
    the same as a record that never had a code."""
    from r6.brief.engine import UNKNOWN, UNLABELLED, _code_text

    assert _code_text({"code": {"coding": [{"system": "urn:oid:1.2.3",
                                            "code": "ZZ99"}]}}) == UNLABELLED
    assert _code_text({}) == UNKNOWN
    assert UNLABELLED != UNKNOWN
