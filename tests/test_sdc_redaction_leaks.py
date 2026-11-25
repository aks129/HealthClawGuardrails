"""The two SDC routes answer an agent without the person's identity.

Spec: docs/specs/2026-11-24-name-removal-before-model.md, section 7, PR 3.
Two leaks were measured on main:

1. `Questionnaire/$populate` over HTTP filled the intake form's demographics
   from the stored Patient: full name, birth date, phone, email and street
   address. The MCP `questionnaire_populate` tool hands that body to the
   model.
2. `QuestionnaireResponse/{id}/$extract?dryRun=true` echoed a stored
   response's answers in full to a read-tier caller, while a plain
   `GET /QuestionnaireResponse/{id}` of the same response hides them.

The form-fill rail still populates in process with the real name, for the
human review page and the PDF; the model gets status and a link. A test
below pins that, so a change that strips both paths is red.

Every value here is synthetic. Each canary shares a prefix, so a canary that
arrives cut short still matches.
"""

import json

from models import db
from r6.models import R6Resource
from r6.sdc.extract import DEFINITION_EXTRACT_URL, OBSERVATION_EXTRACT_URL
from r6.sdc.intake import intake_questionnaire

PREFIX = "ZQSDCLEAK"
GIVEN = PREFIX + "GIVEN"
FAMILY = PREFIX + "FAMILY"
PHONE = PREFIX + "PHONE"
EMAIL = PREFIX + "EMAIL@example.org"
LINE = PREFIX + "LINE"
CITY = PREFIX + "CITY"
POSTAL = PREFIX + "POSTAL"
ANSWER = PREFIX + "ANSWER"
CODE_DISPLAY = PREFIX + "CODEDISPLAY"
#: A date cannot carry the prefix. This one is odd enough to be its own token.
DOB = "1911-11-11"

PATIENT_ID = "sdc-leak-p1"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
LOINC = "http://loinc.org"
PATIENT_DEF = "http://hl7.org/fhir/StructureDefinition/Patient#Patient"


def _patient():
    return {
        "resourceType": "Patient", "id": PATIENT_ID,
        "name": [{"family": FAMILY, "given": [GIVEN]}],
        "gender": "female", "birthDate": DOB,
        "telecom": [{"system": "phone", "value": PHONE},
                    {"system": "email", "value": EMAIL}],
        "address": [{"line": [LINE], "city": CITY, "postalCode": POSTAL,
                     "state": "NY"}],
    }


def _medication():
    # Metformin by RxNorm code. The review page and $populate both name it
    # from r6/terminology.py, so it is the clinical leaf that must survive.
    return {
        "resourceType": "MedicationRequest", "id": "sdc-leak-med",
        "status": "active", "intent": "order",
        "medicationCodeableConcept": {
            "coding": [{"system": RXNORM, "code": "861007"}]},
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
    }


def _store(app, resource, tenant_id):
    with app.app_context():
        db.session.add(R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource["id"], tenant_id=tenant_id))
        db.session.commit()


def _leaks(body):
    """Every canary in `body`, the full birth date included."""
    found = {token for token in (GIVEN, FAMILY, PHONE, EMAIL, LINE, CITY,
                                 POSTAL, ANSWER, CODE_DISPLAY, DOB)
             if token in body}
    if PREFIX in body and not found:
        found.add(PREFIX)
    return found


def _answers(qr):
    out = {}

    def walk(items):
        for item in items:
            if item.get("answer"):
                out.setdefault(item["linkId"], []).extend(item["answer"])
            walk(item.get("item", []))
    walk(qr.get("item", []))
    return out


def _param(body, name):
    return next(p["resource"] for p in body["parameter"]
                if p["name"] == name)


def _populate(client, headers, subject_param):
    return client.post(
        "/r6/fhir/Questionnaire/$populate", headers=headers,
        json={"resourceType": "Parameters", "parameter": [
            subject_param,
            {"name": "questionnaire", "resource": intake_questionnaire()}]})


# ---------------------------------------------------------------------------
# 1. $populate over HTTP
# ---------------------------------------------------------------------------

IDENTITY_LEAVES = ("demographics.given-name", "demographics.family-name",
                   "demographics.phone", "demographics.address-line",
                   "demographics.address-city",
                   "demographics.address-postal-code")


def test_http_populate_returns_no_identity_for_a_stored_subject(
        client, app, tenant_id, tenant_headers):
    """The stock intake form, filled over HTTP for a stored Patient, carries
    no name, phone, email, street, city, postal code or full birth date.

    MUTATION: pass the stored subject to populate_questionnaire in
    r6/sdc/routes.py -> red.
    """
    _store(app, _patient(), tenant_id)
    _store(app, _medication(), tenant_id)

    response = _populate(client, tenant_headers, {
        "name": "subject",
        "valueReference": {"reference": f"Patient/{PATIENT_ID}"}})

    assert response.status_code == 200, response.get_data(as_text=True)
    text = response.get_data(as_text=True)
    assert _leaks(text) == set()

    body = response.get_json()
    answers = _answers(_param(body, "response"))
    # Two-sided: the record was read. The medication still populates, named
    # by code, so a route that strips everything is red here.
    names = [a.get("valueString")
             for a in answers.get("medications.item.name", [])]
    assert names and all(names), answers
    # Identity leaves are reported "not populated", not filled with a stub.
    for link_id in IDENTITY_LEAVES:
        assert link_id not in answers, (link_id, answers.get(link_id))
    reported = {i["diagnostics"] for i in _param(body, "issues")["issue"]
                if i["code"] == "incomplete"}
    assert set(IDENTITY_LEAVES) <= reported, reported


def test_http_populate_returns_no_identity_for_an_inline_subject(
        client, tenant_headers):
    """The HTTP route serves agents. A subject passed inline is filled the
    same way as a stored one, so the route has one rule, not two."""
    response = _populate(client, tenant_headers,
                         {"name": "subject", "resource": _patient()})

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _leaks(response.get_data(as_text=True)) == set()
    answers = _answers(_param(response.get_json(), "response"))
    # Two-sided: the projection still ran. gender is not an identifier.
    assert answers.get("demographics.gender"), answers


def test_form_fill_draft_keeps_the_real_name(app, tenant_id):
    """The human path keeps identity. The review page and the PDF populate
    in process from the stored Patient, and the person sees their own name,
    phone and address to check before anything is sent.

    MUTATION: hand r6/actions/review.py the identity-free subject -> red.
    """
    from r6.actions import review
    from r6.actions.models import ProposedAction

    _store(app, _patient(), tenant_id)
    with app.app_context():
        action = ProposedAction(
            tenant_id=tenant_id, kind="form-fill",
            payload={"subject": {"reference": f"Patient/{PATIENT_ID}"}})
        db.session.add(action)
        db.session.commit()
        _questionnaire, _patient_row, qr, _content = review._draft_qr(
            action, tenant_id)

    answers = _answers(qr)
    assert answers["demographics.given-name"] == [{"valueString": GIVEN}]
    assert answers["demographics.family-name"] == [{"valueString": FAMILY}]
    assert answers["demographics.phone"] == [{"valueString": PHONE}]
    assert answers["demographics.address-line"] == [{"valueString": LINE}]
    assert DOB in json.dumps(answers["demographics.birth-date"])


# ---------------------------------------------------------------------------
# 2. $extract on a stored response
# ---------------------------------------------------------------------------

def _stored_response(app, tenant_id, questionnaire_id="sdc-leak-intake"):
    """A subject-less stored response answering the intake demographics,
    the shape section 2.2 of the spec measured. A response bound to a
    subject yields no Patient row (#572), so this is the shape that leaked.
    """
    questionnaire = dict(intake_questionnaire(), id=questionnaire_id)
    _store(app, questionnaire, tenant_id)
    qr = {
        "resourceType": "QuestionnaireResponse", "id": "sdc-leak-qr",
        "status": "completed",
        "questionnaire": f"Questionnaire/{questionnaire_id}",
        "item": [{"linkId": "demographics", "item": [
            {"linkId": "demographics.given-name",
             "answer": [{"valueString": GIVEN}]},
            {"linkId": "demographics.family-name",
             "answer": [{"valueString": FAMILY}]},
            {"linkId": "demographics.birth-date",
             "answer": [{"valueDate": DOB}]},
            {"linkId": "demographics.gender",
             "answer": [{"valueCoding": {
                 "system": "http://hl7.org/fhir/administrative-gender",
                 "code": "female"}}]},
            {"linkId": "demographics.phone",
             "answer": [{"valueString": PHONE}]},
            {"linkId": "demographics.address-line",
             "answer": [{"valueString": LINE}]},
        ]}],
    }
    _store(app, qr, tenant_id)
    return qr


def _extract_stored(client, headers, params=None):
    return client.post(
        "/r6/fhir/QuestionnaireResponse/sdc-leak-qr/$extract?dryRun=true",
        headers=headers,
        json=params or {"resourceType": "Parameters", "parameter": []})


def _entries(response):
    return _param(response.get_json(), "return")["entry"]


def test_a_read_of_the_stored_response_already_hides_its_answers(
        client, app, tenant_id, tenant_headers):
    """The baseline the dry run is measured against: a plain read of the
    response hides the name and full birth date. Without this, the next
    test would only show $extract is strict, not that it agrees."""
    _stored_response(app, tenant_id)
    response = client.get("/r6/fhir/QuestionnaireResponse/sdc-leak-qr",
                          headers=tenant_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert _leaks(response.get_data(as_text=True)) == set()


def test_stored_response_dry_run_extract_returns_no_identity(
        client, app, tenant_id, tenant_headers):
    """A read-tier dry run sees what a read of the response sees, no more.

    MUTATION: skip the redaction on the stored-response branch of $extract
    in r6/sdc/routes.py -> red.
    """
    _stored_response(app, tenant_id)

    response = _extract_stored(client, tenant_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _leaks(response.get_data(as_text=True)) == set()
    # Two-sided: the dry run still extracts. The Patient row is built from
    # what a read shows (the coded gender), not dropped.
    entries = _entries(response)
    assert [e["resource"]["resourceType"] for e in entries] == ["Patient"]
    assert "female" in json.dumps(entries[0]["resource"]["gender"])


def test_stored_response_answers_cannot_be_moved_into_a_kept_element(
        client, app, tenant_id, tenant_headers):
    """The dry run takes an inline Questionnaire beside a stored response,
    and the walker keeps `gender` and `address.state`. Redacting only the
    extracted rows would let a caller map a free-text answer into one of
    those and read it back. The stored response is redacted before
    extraction, so there is nothing to move."""
    _stored_response(app, tenant_id)
    relocating = {
        "resourceType": "Questionnaire", "status": "active",
        "extension": [{"url": DEFINITION_EXTRACT_URL,
                       "valueCode": "Patient"}],
        "item": [
            {"linkId": "demographics.family-name", "type": "string",
             "definition": f"{PATIENT_DEF}.gender"},
            {"linkId": "demographics.given-name", "type": "string",
             "definition": f"{PATIENT_DEF}.address.state"},
            {"linkId": "demographics.birth-date", "type": "string",
             "definition": f"{PATIENT_DEF}.address.country"},
        ],
    }

    response = _extract_stored(client, tenant_headers, {
        "resourceType": "Parameters",
        "parameter": [{"name": "questionnaire", "resource": relocating}]})

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _leaks(response.get_data(as_text=True)) == set()


def test_stored_response_observation_answers_are_not_echoed(
        client, app, tenant_id, tenant_headers):
    """Observation-based extraction on a stored response: a free-text
    answer is hidden by a read, so the dry run hides it too. A coded
    quantity is not, and still extracts."""
    questionnaire = {
        "resourceType": "Questionnaire", "id": "sdc-leak-obs-q",
        "status": "active",
        "extension": [{"url": OBSERVATION_EXTRACT_URL, "valueBoolean": True}],
        "item": [
            {"linkId": "note", "type": "string",
             "code": [{"system": LOINC, "code": "8302-2"}]},
            {"linkId": "chol", "type": "quantity",
             "code": [{"system": LOINC, "code": "2093-3"}]},
        ],
    }
    _store(app, questionnaire, tenant_id)
    _store(app, {
        "resourceType": "QuestionnaireResponse", "id": "sdc-leak-qr",
        "status": "completed",
        "questionnaire": "Questionnaire/sdc-leak-obs-q",
        "item": [
            {"linkId": "note", "answer": [{"valueString": ANSWER}]},
            {"linkId": "chol", "answer": [{"valueQuantity": {
                "value": 187, "unit": "mg/dL"}}]},
        ],
    }, tenant_id)

    response = _extract_stored(client, tenant_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _leaks(response.get_data(as_text=True)) == set()
    values = [e["resource"].get("valueQuantity", {}).get("value")
              for e in _entries(response)]
    assert 187 in values, values


def test_stored_response_rows_carry_no_upstream_display(
        client, app, tenant_id, tenant_headers):
    """Redacting the stored response is not enough on its own. Observation
    extraction copies the Questionnaire's `item.code` into `Observation.code`
    verbatim, so a stored Questionnaire's upstream `display` would reach the
    rows. The stored Questionnaire is redacted when it is loaded, like the
    response. tests/test_sdc_1167_attack.py covers the same Questionnaire
    behind an inline response.

    MUTATION: load the referenced Questionnaire unredacted -> red.
    """
    _store(app, {
        "resourceType": "Questionnaire", "id": "sdc-leak-display-q",
        "status": "active",
        "extension": [{"url": OBSERVATION_EXTRACT_URL, "valueBoolean": True}],
        "item": [{"linkId": "chol", "type": "quantity",
                  "code": [{"system": LOINC, "code": "2093-3",
                            "display": CODE_DISPLAY}]}],
    }, tenant_id)
    _store(app, {
        "resourceType": "QuestionnaireResponse", "id": "sdc-leak-qr",
        "status": "completed",
        "questionnaire": "Questionnaire/sdc-leak-display-q",
        "item": [{"linkId": "chol", "answer": [{"valueQuantity": {
            "value": 187, "unit": "mg/dL"}}]}],
    }, tenant_id)

    response = _extract_stored(client, tenant_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _leaks(response.get_data(as_text=True)) == set()
    # Two-sided: the row is there, still coded, with its value.
    [entry] = _entries(response)
    assert entry["resource"]["code"]["coding"][0]["code"] == "2093-3"
    assert entry["resource"]["valueQuantity"]["value"] == 187


def test_an_inline_response_still_previews_in_full(client, tenant_headers):
    """An inline response is the caller's own data, just sent in the body.
    It is echoed as before; redacting it would hand the caller back their
    own form unusable and reveal nothing they did not hold."""
    qr = {
        "resourceType": "QuestionnaireResponse", "status": "completed",
        "item": [{"linkId": "demographics", "item": [
            {"linkId": "demographics.family-name",
             "answer": [{"valueString": FAMILY}]},
            {"linkId": "demographics.birth-date",
             "answer": [{"valueDate": DOB}]},
        ]}],
    }
    response = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract?dryRun=true",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "questionnaire-response", "resource": qr},
            {"name": "questionnaire", "resource": intake_questionnaire()}]})

    assert response.status_code == 200, response.get_data(as_text=True)
    patient = _entries(response)[0]["resource"]
    assert patient["name"][0]["family"] == FAMILY
    assert patient["birthDate"] == DOB
