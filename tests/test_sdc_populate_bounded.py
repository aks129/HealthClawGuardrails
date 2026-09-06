"""What `$populate` is allowed to read (council ruling D10).

Three council seats independently called the old behaviour a live
non-negotiable violation: an expression in a caller-supplied Questionnaire
evaluated against the whole stored Patient, and every Observation /
MedicationRequest / AllergyIntolerance / Condition the tenant held for the
subject was loaded verbatim into the answer set. Neither bound existed.

This file measures the four properties that replace it, over the real HTTP
route with real store rows — not against the pure engine, because the engine
cannot tell you what left the boundary:

  1. An expression outside the %patient allowlist yields NO answer, and an
     OperationOutcome issue naming the linkId, so the refusal is visible
     rather than indistinguishable from "the patient has no phone number".
  2. The allowlisted expressions still populate. A bound that also breaks the
     intake form is not a fix.
  3. A tombstoned row is not read.
  4. An Observation whose `code.text` is a patient name — which is what real
     feeds send, and the reason CLAUDE.md forbids preserving an upstream
     `display` or `CodeableConcept.text` — never reaches the response.

Every negative assertion here is paired with a positive one on the same
request. A response that 404s, or that populates nothing at all, would make
"the marker is absent" pass while measuring nothing; asserting the form also
filled in correctly is what stops that.
"""

import json

from r6.models import R6Resource, db

INITIAL_EXPR_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-initialExpression"
)

#: One marker per free-text field a real feed writes a patient name into, so
#: a hit names WHICH field leaked rather than only that something did.
OBS_TEXT_NAME_MARKER = "PHIOBSCODETEXTMARKER"
OBS_VALUE_NAME_MARKER = "PHIOBSVALUETEXTMARKER"
MED_NAME_MARKER = "PHIMEDTEXTMARKER"
DOSE_NAME_MARKER = "PHIDOSETEXTMARKER"
ALLERGEN_NAME_MARKER = "PHIALLERGENTEXTMARKER"
CONDITION_NAME_MARKER = "PHICONDTEXTMARKER"

LOINC = "http://loinc.org"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
#: Total cholesterol. Recognised by r6/terminology.py, and that is the point:
#: an UNrecognised code would leave the redacted Observation with no
#: `code.text` at all, so `%resources...code.text` would resolve to nothing
#: for a second reason and the negative test would pass without the
#: projection doing any work.
TOTAL_CHOL = "2093-3"
SMOKING = "72166-2"
METFORMIN = "860975"
DIABETES = "E11.9"

PATIENT_ID = "bounded-p1"

MED_DEF = ("http://hl7.org/fhir/StructureDefinition/"
           "MedicationRequest#MedicationRequest")
ALLERGY_DEF = ("http://hl7.org/fhir/StructureDefinition/"
               "AllergyIntolerance#AllergyIntolerance")
CONDITION_DEF = "http://hl7.org/fhir/StructureDefinition/Condition#Condition"


def _walk(items):
    for item in items:
        yield item
        if "item" in item:
            yield from _walk(item["item"])


def _list_questionnaire():
    """The intake shape: repeating medication / allergy / condition groups
    whose leaves are `definition`-linked, plus the coded Observation item.

    These are the items whose resolvers copy free text out of a stored
    resource and into an answer — the paths redaction has to cover.
    """
    return {
        "resourceType": "Questionnaire", "id": "list-q", "status": "active",
        "item": [
            {"linkId": "smoking", "type": "string",
             "code": [{"system": LOINC, "code": SMOKING}]},
            {"linkId": "medications", "type": "group", "item": [
                {"linkId": "medications.item", "type": "group",
                 "repeats": True, "item": [
                     {"linkId": "medications.item.name", "type": "string",
                      "definition":
                          f"{MED_DEF}.medicationCodeableConcept.text"},
                     {"linkId": "medications.item.dose", "type": "string",
                      "definition": f"{MED_DEF}.dosageInstruction.text"}]}]},
            {"linkId": "allergies", "type": "group", "item": [
                {"linkId": "allergies.no-known-allergies", "type": "boolean"},
                {"linkId": "allergies.item", "type": "group",
                 "repeats": True, "item": [
                     {"linkId": "allergies.item.allergen", "type": "string",
                      "definition": f"{ALLERGY_DEF}.code.text"}]}]},
            {"linkId": "conditions", "type": "group", "item": [
                {"linkId": "conditions.item", "type": "group",
                 "repeats": True, "item": [
                     {"linkId": "conditions.item.name", "type": "string",
                      "definition": f"{CONDITION_DEF}.code.text"}]}]},
        ],
    }


def _store(app, resource, tenant_id, is_deleted=False):
    with app.app_context():
        row = R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource["id"],
            tenant_id=tenant_id,
        )
        # R6Resource.__init__ takes no is_deleted; the flag is set as an
        # attribute, which is also how the one production writer does it
        # (r6/routes.py:2688).
        row.is_deleted = is_deleted
        db.session.add(row)
        db.session.commit()


def _patient():
    """A subject carrying one element of every kind the ruling names, plus the
    identified fields it withholds."""
    return {
        "resourceType": "Patient",
        "id": PATIENT_ID,
        "name": [{"given": ["Ada"], "family": "Lovelace",
                  "text": "Ada Lovelace"}],
        "birthDate": "1815-12-10",
        "gender": "female",
        "identifier": [{"system": "http://hospital.example/mrn",
                        "value": "MRN-000-1234"}],
        "photo": [{"contentType": "image/jpeg",
                   "url": "https://example.org/ada.jpg"}],
        "telecom": [{"system": "phone", "value": "555-0100"},
                    {"system": "email", "value": "ada@example.org"}],
        "address": [{"line": ["1 Analytical Way"], "city": "London",
                     "state": "MA", "postalCode": "01001",
                     "country": "GB"}],
    }


def _expr_item(link_id, expression, item_type="string"):
    return {
        "linkId": link_id, "type": item_type,
        "extension": [{
            "url": INITIAL_EXPR_URL,
            "valueExpression": {"language": "text/fhirpath",
                                "expression": expression},
        }],
    }


#: linkId -> an expression the projection must NOT answer. The first three are
#: the ruling's named cases; the fourth reaches for the auto-loaded content
#: through the `%resources` environment variable the context no longer carries.
WITHHELD = {
    "mrn": "%patient.identifier.value",
    "photo": "%patient.photo.url",
    "name-text": "%patient.name.text",
    "obs-text": "%resources.where(resourceType='Observation').code.text",
}

#: linkId -> an expression that must keep working. An allowlist that breaks
#: the intake form is not a fix, so these ride on the same request.
PERMITTED = {
    "given": "%patient.name.given.first()",
    "dob": "%patient.birthDate",
    "email": "%patient.telecom.where(system='email').value",
    "city": "%patient.address.city.first()",
}


def _questionnaire():
    items = [_expr_item(link_id, expr) for link_id, expr in WITHHELD.items()]
    items += [_expr_item(link_id, expr) for link_id, expr in PERMITTED.items()]
    return {"resourceType": "Questionnaire", "id": "bounded-q",
            "status": "active", "item": items}


def _seed(app, tenant_id):
    _store(app, _patient(), tenant_id)
    _store(app, {
        "resourceType": "Observation", "id": "bounded-obs", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": TOTAL_CHOL,
                             "display": OBS_TEXT_NAME_MARKER}],
                 "text": OBS_TEXT_NAME_MARKER},
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
        "effectiveDateTime": "2026-01-15",
        "valueQuantity": {"value": 244, "unit": "mg/dL"},
    }, tenant_id)
    _store(app, _questionnaire(), tenant_id)


def _populate(client, tenant_headers, questionnaire_id="bounded-q"):
    return client.post(
        f"/r6/fhir/Questionnaire/{questionnaire_id}/$populate",
        headers=tenant_headers,
        json={"resourceType": "Parameters",
              "parameter": [{"name": "subject", "valueReference": {
                  "reference": f"Patient/{PATIENT_ID}"}}]},
    )


def _param(params, name):
    for p in params.get("parameter", []):
        if p.get("name") == name:
            return p.get("resource")
    return None


def _answers(qr):
    """linkId -> the item's single answer value, for items that got one.

    Walks nested groups, so a repeating group's leaves are visible. The tests
    using it seed exactly one repeat per group; a second repeat would
    overwrite the first, and that is deliberate — this helper is for
    asserting VALUES, and repeat counts are asserted with _walk directly.
    """
    out = {}
    for item in _walk(qr.get("item", [])):
        answers = item.get("answer") or []
        if answers:
            out[item["linkId"]] = list(answers[0].values())[0]
    return out


def _issue_link_ids(params):
    outcome = _param(params, "issues") or {}
    return [issue["diagnostics"].split(":")[0]
            for issue in outcome.get("issue", [])]


# ---------------------------------------------------------------------------
# 1 + 2. The projection: nothing outside it resolves, everything inside does
# ---------------------------------------------------------------------------

def test_expressions_outside_the_patient_projection_produce_no_answer(
        client, app, tenant_id, tenant_headers):
    """The ruling's negative list, over the wire.

    MUTATION: add 'identifier' to the projection in r6/sdc/expressions.py ->
    the 'mrn' assertion goes red on both halves (an answer appears AND its
    issue disappears).
    """
    _seed(app, tenant_id)

    resp = _populate(client, tenant_headers)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    answered = _answers(_param(body, "response"))
    for link_id in WITHHELD:
        assert link_id not in answered, (
            f"{link_id} was answered from outside the %patient projection: "
            f"{answered.get(link_id)!r}")


def test_a_withheld_expression_is_reported_as_an_issue_naming_its_link_id(
        client, app, tenant_id, tenant_headers):
    """Silence is not an answer a caller can act on.

    An empty item has two causes — the record has no such element, or this
    operation refused to look. A caller told nothing would read the first
    from the second, so every withheld item names itself in an
    OperationOutcome issue. The diagnostics carry the linkId (questionnaire
    structure, authored by the caller) and a fixed sentence; the expression
    text is deliberately NOT echoed, because a `where()` clause is a place an
    author can park a literal.
    """
    _seed(app, tenant_id)

    body = _populate(client, tenant_headers).get_json()

    assert sorted(_issue_link_ids(body)) == sorted(WITHHELD)
    diagnostics = _param(body, "issues")["issue"][0]["diagnostics"]
    assert "%patient projection" in diagnostics
    for expression in WITHHELD.values():
        assert expression not in diagnostics


def test_allowlisted_expressions_still_populate(
        client, app, tenant_id, tenant_headers):
    """The other half of the bound, on the same request as the refusals.

    Without this, deleting the whole expression path would pass every
    negative assertion in this file.
    """
    _seed(app, tenant_id)

    body = _populate(client, tenant_headers).get_json()

    assert _answers(_param(body, "response")) == {
        "given": "Ada",
        "dob": "1815-12-10",
        "email": "ada@example.org",
        "city": "London",
    }


def test_no_issue_is_raised_when_the_record_simply_has_no_value(
        client, app, tenant_id, tenant_headers):
    """An allowlisted element the patient does not have is NOT a refusal.

    This is the line between the two causes above. A patient with no telecom
    gets an empty item and no issue; conflating that with a withheld path
    would put an OperationOutcome on every sparse record and make the issue
    list meaningless.
    """
    bare = {"resourceType": "Patient", "id": PATIENT_ID,
            "name": [{"given": ["Ada"]}]}
    _store(app, bare, tenant_id)
    _store(app, {"resourceType": "Questionnaire", "id": "sparse-q",
                 "status": "active",
                 "item": [_expr_item("email", PERMITTED["email"]),
                          _expr_item("given", PERMITTED["given"])]}, tenant_id)

    body = _populate(client, tenant_headers, "sparse-q").get_json()

    assert _answers(_param(body, "response")) == {"given": "Ada"}
    assert _param(body, "issues") is None


# ---------------------------------------------------------------------------
# 3. Tombstoned rows
# ---------------------------------------------------------------------------

def test_a_tombstoned_observation_is_not_loaded(
        client, app, tenant_id, tenant_headers):
    """#422 on this read path.

    MUTATION: drop is_deleted=False from _load_resources_for_patient -> red.
    """
    _store(app, _patient(), tenant_id)
    _store(app, {
        "resourceType": "Observation", "id": "tombstoned-obs",
        "status": "final",
        "code": {"coding": [{"system": LOINC, "code": TOTAL_CHOL}]},
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
        "effectiveDateTime": "2026-02-01",
        "valueQuantity": {"value": 999, "unit": "mg/dL"},
    }, tenant_id, is_deleted=True)
    _store(app, {"resourceType": "Questionnaire", "id": "chol-q",
                 "status": "active",
                 "item": [{"linkId": "chol", "type": "quantity",
                           "code": [{"system": LOINC, "code": TOTAL_CHOL}]}]},
           tenant_id)

    body = _populate(client, tenant_headers, "chol-q").get_json()

    assert _answers(_param(body, "response")) == {}
    assert "999" not in json.dumps(body)


def test_a_tombstoned_questionnaire_is_not_resolved(
        client, app, tenant_id, tenant_headers):
    """MUTATION: drop is_deleted=False from _load_stored -> red."""
    _store(app, _patient(), tenant_id)
    _store(app, _questionnaire(), tenant_id, is_deleted=True)

    assert _populate(client, tenant_headers).status_code == 404


# ---------------------------------------------------------------------------
# 4. Redaction of auto-loaded clinical content
# ---------------------------------------------------------------------------

def test_upstream_free_text_never_reaches_an_answer(
        client, app, tenant_id, tenant_headers):
    """The non-negotiable, on every path that copies free text into an answer.

    Real feeds write patient names into `code.text` and `coding.display`
    (#207, #209), which is why CLAUDE.md forbids preserving either. This has
    to be measured on the paths that actually carry that text into the
    response, and there are five: the three list-group resolvers
    (medicationCodeableConcept.text, AllergyIntolerance.code.text,
    Condition.code.text), dosageInstruction.text, and an Observation whose
    VALUE is a CodeableConcept (r6/sdc/populate.py::_observation_answer
    returns `valueCodeableConcept.text`).

    The first version of this test used an item that reads a valueQuantity.
    It passed with apply_redaction deleted, because no source resource is
    echoed in a $populate response at all — a green check measuring nothing,
    the shape docs/2026-08-02-retro.md is about. Every questionnaire item
    below therefore has a resolver that copies text, and the request is
    asserted to have populated something, so "the marker is absent" cannot
    pass by the response being empty.

    MUTATION: replace apply_redaction with a passthrough in
    _redacted_for_populate -> red, three markers deep.
    """
    _store(app, _patient(), tenant_id)
    _store(app, {
        "resourceType": "Observation", "id": "marked-obs", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": SMOKING},
                            ],
                 "text": OBS_TEXT_NAME_MARKER},
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
        "effectiveDateTime": "2026-01-15",
        "valueCodeableConcept": {"text": OBS_VALUE_NAME_MARKER},
    }, tenant_id)
    _store(app, {
        "resourceType": "MedicationRequest", "id": "marked-med",
        "status": "active", "intent": "order",
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
        "medicationCodeableConcept": {
            "coding": [{"system": RXNORM, "code": METFORMIN,
                        "display": MED_NAME_MARKER}],
            "text": MED_NAME_MARKER},
        "dosageInstruction": [{"text": DOSE_NAME_MARKER}],
    }, tenant_id)
    _store(app, {
        "resourceType": "AllergyIntolerance", "id": "marked-allergy",
        "patient": {"reference": f"Patient/{PATIENT_ID}"},
        "code": {"text": ALLERGEN_NAME_MARKER},
    }, tenant_id)
    _store(app, {
        "resourceType": "Condition", "id": "marked-condition",
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "verificationStatus": {"coding": [{"code": "confirmed"}]},
        "code": {"coding": [{"system": ICD10, "code": DIABETES,
                             "display": CONDITION_NAME_MARKER}],
                 "text": CONDITION_NAME_MARKER},
    }, tenant_id)
    _store(app, _list_questionnaire(), tenant_id)

    resp = _populate(client, tenant_headers, "list-q")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    raw = resp.get_data(as_text=True)
    for marker in (OBS_TEXT_NAME_MARKER, OBS_VALUE_NAME_MARKER,
                   MED_NAME_MARKER, DOSE_NAME_MARKER, ALLERGEN_NAME_MARKER,
                   CONDITION_NAME_MARKER):
        assert marker not in raw, (
            f"{marker} reached the caller through $populate — an upstream "
            f"display/CodeableConcept.text survived redaction")
    # ...and the operation still did its job: the codes the server knows come
    # back labelled from r6/terminology.py, keyed by code, after the strip.
    answers = _answers(_param(resp.get_json(), "response"))
    assert answers["medications.item.name"] == "Metformin 500 mg"
    assert answers["conditions.item.name"] == (
        "Type 2 diabetes mellitus, without complications")


def test_an_allergen_the_server_cannot_label_populates_empty(
        client, app, tenant_id, tenant_headers):
    """The cost of the bound, pinned rather than left to be discovered.

    r6/terminology.py has no allergen vocabulary — no SNOMED entries at all —
    so once the upstream `code.text` is stripped there is nothing to put
    back, and the allergen row populates with no answer. The repeat is still
    emitted, so the form says "an allergy is on file that I could not name"
    rather than dropping it, and `no-known-allergies` is still never touched.

    This is a terminology-coverage gap, not a guard defect. It is written
    down here so that closing it (adding allergen codes, or an allergen
    resolver) is a deliberate change with a test that goes green, instead of
    a surprise on an intake form.
    """
    _store(app, _patient(), tenant_id)
    _store(app, {
        "resourceType": "AllergyIntolerance", "id": "marked-allergy",
        "patient": {"reference": f"Patient/{PATIENT_ID}"},
        "code": {"coding": [{"system": "http://snomed.info/sct",
                             "code": "91936005"}],
                 "text": ALLERGEN_NAME_MARKER},
    }, tenant_id)
    _store(app, _list_questionnaire(), tenant_id)

    body = _populate(client, tenant_headers, "list-q").get_json()
    qr = _param(body, "response")

    allergen_items = [i for i in _walk(qr.get("item", []))
                      if i["linkId"] == "allergies.item.allergen"]
    assert len(allergen_items) == 1, "the allergy repeat itself was dropped"
    assert "answer" not in allergen_items[0]
    nka = [i for i in _walk(qr.get("item", []))
           if i["linkId"] == "allergies.no-known-allergies"]
    assert nka and "answer" not in nka[0], (
        "no-known-allergies must never be inferred, least of all from an "
        "allergy we failed to label")


def test_the_most_recent_observation_still_wins_after_redaction(
        client, app, tenant_id, tenant_headers):
    """Recency selection survives the redaction pass.

    r6/sdc/populate.py::_observation_answer picks the most recent match by
    `effectiveDateTime`, and redaction now runs before it sees the rows. A
    redaction profile that truncated that field — it truncates birthDate and
    valueDateTime, and effectiveDateTime is one line away from the same set
    (r6/redaction.py:_DATE_KEYS) — would leave same-year results
    indistinguishable and the form pre-filled with an arbitrary one, which is
    a wrong clinical value on a page a human is about to sign.

    It does not truncate it today. This pins that, so adding effectiveDateTime
    to _DATE_KEYS goes red HERE, where the consequence is legible, rather
    than silently on an intake form.
    """
    _store(app, _patient(), tenant_id)
    for rid, day, value in (("chol-jan", "2026-01-15", 244),
                            ("chol-nov", "2026-11-02", 180)):
        _store(app, {
            "resourceType": "Observation", "id": rid, "status": "final",
            "code": {"coding": [{"system": LOINC, "code": TOTAL_CHOL}]},
            "subject": {"reference": f"Patient/{PATIENT_ID}"},
            "effectiveDateTime": day,
            "valueQuantity": {"value": value, "unit": "mg/dL"},
        }, tenant_id)
    _store(app, {"resourceType": "Questionnaire", "id": "chol-q",
                 "status": "active",
                 "item": [{"linkId": "chol", "type": "quantity",
                           "code": [{"system": LOINC, "code": TOTAL_CHOL}]}]},
           tenant_id)

    body = _populate(client, tenant_headers, "chol-q").get_json()

    assert _answers(_param(body, "response"))["chol"]["value"] == 180


def test_the_caller_s_own_inline_content_is_passed_through(
        client, app, tenant_id, tenant_headers):
    """The inline `content` Bundle is not redacted, on purpose.

    It is the caller's own data, sent in this request body a moment ago.
    Redacting it would hand back something less than what was sent and
    protects nobody — the caller already holds it. Only rows this route
    loaded from the tenant store on the caller's behalf are redacted.
    """
    _store(app, _patient(), tenant_id)
    _store(app, {"resourceType": "Questionnaire", "id": "chol-q",
                 "status": "active",
                 "item": [{"linkId": "chol", "type": "string",
                           "code": [{"system": LOINC, "code": TOTAL_CHOL}]}]},
           tenant_id)

    resp = client.post(
        "/r6/fhir/Questionnaire/chol-q/$populate",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "subject", "valueReference": {
                "reference": f"Patient/{PATIENT_ID}"}},
            {"name": "content", "resource": {
                "resourceType": "Bundle", "type": "collection", "entry": [
                    {"resource": {
                        "resourceType": "Observation", "status": "final",
                        "code": {"coding": [{"system": LOINC,
                                             "code": TOTAL_CHOL}]},
                        "effectiveDateTime": "2026-03-03",
                        "valueString": "inline-caller-value"}}]}},
        ]},
    )

    assert resp.status_code == 200
    answers = _answers(_param(resp.get_json(), "response"))
    assert answers["chol"] == "inline-caller-value"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_the_audit_detail_carries_counts_and_no_patient_data(
        client, app, tenant_id, tenant_headers):
    """`detail` stays PHI-free — the constitution's rule, checked on the row
    this operation actually writes rather than on the code that writes it."""
    from r6.models import AuditEventRecord

    _seed(app, tenant_id)
    _populate(client, tenant_headers)

    with app.app_context():
        rows = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type="Questionnaire",
            event_type="read").all()
        details = [r.detail or "" for r in rows]
    assert details, "no audit row was written for $populate"
    assert any("issues=" in d for d in details)
    for detail in details:
        for phi in ("Lovelace", "Ada", "1815-12-10", "MRN-000-1234",
                    "ada@example.org", OBS_TEXT_NAME_MARKER):
            assert phi not in detail


# ---------------------------------------------------------------------------
# 5. The issue channel is not a read of the record it refuses to show
# ---------------------------------------------------------------------------

def test_the_withheld_issue_cannot_be_used_to_guess_the_withheld_value(
        client, app, tenant_id, tenant_headers):
    """A refusal that depends on the data is a read of the data.

    The first cut of this bound decided whether to emit the issue by
    evaluating the caller's own expression against the UNBOUNDED record and
    reporting whether it came back non-empty. That is a one-bit oracle over
    exactly what the projection withholds: send
    `%patient.identifier.value.where($this.startsWith('M'))` on one item,
    read whether an issue named that linkId, and walk the identifier out one
    character at a time. Eleven HTTP requests recovered a nine-digit
    identifier that way during the review of PR #562, and the value never
    once appeared in an answer.

    So the issue has to depend on the EXPRESSION and nothing else: a right
    guess and a wrong guess must be indistinguishable from outside.

    MUTATION: pass the real subject back into resolves_outside_projection ->
    `recovered` becomes the stored MRN and this goes red on the first
    character.
    """
    _seed(app, tenant_id)
    secret = _patient()["identifier"][0]["value"]
    alphabet = sorted(set(secret))

    recovered = ""
    for _ in range(len(secret)):
        items = [
            _expr_item(f"guess-{n}",
                       "%patient.identifier.value.where($this.startsWith('"
                       + recovered + ch + "'))")
            for n, ch in enumerate(alphabet)
        ]
        _store(app, {"resourceType": "Questionnaire", "id": "oracle-q",
                     "status": "active", "item": items}, tenant_id)
        body = _populate(client, tenant_headers,
                         questionnaire_id="oracle-q").get_json()
        named = set(_issue_link_ids(body))
        hits = [ch for n, ch in enumerate(alphabet) if f"guess-{n}" in named]
        # All refused or none refused. Anything between is the oracle.
        assert len(hits) in (0, len(alphabet)), (
            f"the issue list singled out {hits} out of {alphabet}: the "
            f"refusal is a function of the patient's data, not of the "
            f"expression")
        if not hits:
            break
        recovered += hits[0]

    assert recovered == "", (
        f"the withheld identifier leaked through the issue channel: "
        f"{recovered!r} of {secret!r}")


def test_a_refused_item_still_names_itself_after_the_oracle_is_closed(
        client, app, tenant_id, tenant_headers):
    """Closing the oracle must not cost the ruling's UX.

    D10 asks for an OperationOutcome issue naming the linkId on
    `%patient.identifier`, `%patient.photo` and `%resources...code.text`.
    Answering from a constant probe keeps all of them — those paths are
    outside the allowlist for every patient, which is a fact about the
    allowlist and not about anyone's record.
    """
    _seed(app, tenant_id)
    items = [_expr_item(link_id, expr) for link_id, expr in WITHHELD.items()]
    _store(app, {"resourceType": "Questionnaire", "id": "after-q",
                 "status": "active", "item": items}, tenant_id)

    body = _populate(client, tenant_headers,
                     questionnaire_id="after-q").get_json()

    assert sorted(_issue_link_ids(body)) == sorted(WITHHELD)
