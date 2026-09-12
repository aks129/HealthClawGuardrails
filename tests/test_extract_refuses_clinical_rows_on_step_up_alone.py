"""$extract commit mode writes nothing on a step-up token alone (#572, #679).

Nothing on the human-gated path calls $extract: the form-fill executor
never extracts, and $extract's callers are the raw endpoint (step-up only)
and the MCP write tool. #668 measured the AllergyIntolerance case: a
Questionnaire whose root definitionExtract names AllergyIntolerance,
answered with plain strings, POSTed to commit mode with a step-up token,
answered 200 with one row stored. #679 measured the class around it —
Observation (a systolic pressure of 191) and Consent both committed on the
same credential, because the refusal was a denylist of three names over a
resource type the caller chooses.

Commit mode now refuses every resource type not explicitly cleared to be
written without a human confirming it, and the cleared set is empty:
there is no caller with a legitimate reason to write on a step-up token
alone. A new extraction target arrives refused; clearing it is a one-line
decision somebody makes on purpose, with a test. dryRun still previews.
The refusal writes no audit row (consistent with the kernel's own
refusals): the audit trail does not cover refused attempts here.

MUTATION: r6/sdc/routes.py, drop the refusal -> red (the row is stored and
the response is 200). r6/sdc/extract.py, clear "Observation" -> red (the
pressure is written).
"""

import pytest

from r6.sdc import extract
from r6.sdc.extract import COMMIT_WITHOUT_CONFIRMATION


@pytest.fixture
def engine_builds_these_types(monkeypatch):
    """#681 stops a definition-based extraction for any type without an
    element list, upstream of the commit gate, so on main the allergy,
    Consent and Procedure cases below never reach it. Teach the engine
    those types for the test: the commit gate's property — nothing is
    written on a step-up token alone — must hold independently of which
    types the engine happens to build."""
    for target, element in (("AllergyIntolerance", "clinicalStatus"),
                            ("Consent", "status"), ("Procedure", "status")):
        monkeypatch.setitem(extract.DEFINITION_ELEMENTS, target,
                            frozenset({element, "verificationStatus",
                                       "patient", "code"}))

SD = "http://hl7.org/fhir/StructureDefinition"
DEF_EXTRACT = ("http://hl7.org/fhir/uv/sdc/StructureDefinition/"
               "sdc-questionnaire-definitionExtract")
OBS_EXTRACT = ("http://hl7.org/fhir/uv/sdc/StructureDefinition/"
               "sdc-questionnaire-observationExtract")
EXTRACT = "/r6/fhir/QuestionnaireResponse/$extract"


def _definition_questionnaire(target, elements):
    return {"resourceType": "Questionnaire", "status": "active",
            "extension": [{"url": DEF_EXTRACT, "valueCode": target}],
            "item": [{"linkId": link, "type": "string",
                      "definition": f"{SD}/{target}#{target}.{path}"}
                     for link, path in elements]}


def _response(answers):
    return {"resourceType": "QuestionnaireResponse", "status": "completed",
            "item": [{"linkId": link, "answer": [{"valueString": value}]}
                     for link, value in answers]}


def _params(qr, questionnaire):
    return {"resourceType": "Parameters", "parameter": [
        {"name": "questionnaire-response", "resource": qr},
        {"name": "questionnaire", "resource": questionnaire}]}


def _allergy_params():
    q = _definition_questionnaire("AllergyIntolerance", [
        ("cs", "clinicalStatus"), ("vs", "verificationStatus"),
        ("pt", "patient"), ("al", "code.text")])
    qr = _response([("cs", "active"), ("vs", "unconfirmed"),
                    ("pt", "Patient/p-hole"), ("al", "peanut-hole")])
    return _params(qr, q)


def _pressure_params():
    # The #679 measurement: an observationExtract item answered 191.
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "sbp", "type": "integer",
                   "code": [{"system": "http://loinc.org", "code": "8480-6"}],
                   "extension": [{"url": OBS_EXTRACT, "valueBoolean": True}]}]}
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "subject": {"reference": "Patient/p-hole"},
          "item": [{"linkId": "sbp", "answer": [{"valueInteger": 191}]}]}
    return _params(qr, q)


def _consent_params():
    q = _definition_questionnaire("Consent", [("st", "status")])
    return _params(_response([("st", "active")]), q)


def _procedure_params():
    # A type nobody enumerated anywhere: refused by default, not written.
    q = _definition_questionnaire("Procedure", [("st", "status")])
    return _params(_response([("st", "completed")]), q)


def _patient_params():
    q = _definition_questionnaire("Patient", [("fam", "name.family")])
    return _params(_response([("fam", "Holefamily")]), q)


def _rows(app, tenant_id, resource_type):
    from r6.models import R6Resource
    with app.app_context():
        return R6Resource.query.filter_by(
            tenant_id=tenant_id, resource_type=resource_type).count()


def _assert_refused(resp, app, tenant_id, resource_type, before, secret):
    assert resp.status_code == 422, resp.get_data(as_text=True)[:200]
    outcome = resp.get_json()
    assert outcome["resourceType"] == "OperationOutcome"
    diag = outcome["issue"][0]["diagnostics"]
    assert "form-fill" in diag and "dryRun" in diag
    assert secret not in diag
    assert _rows(app, tenant_id, resource_type) == before


@pytest.mark.parametrize("resource_type, params, secret", [
    ("AllergyIntolerance", _allergy_params(), "peanut-hole"),
    ("Observation", _pressure_params(), "191"),
    ("Consent", _consent_params(), "p-hole"),
    ("Procedure", _procedure_params(), "p-hole"),
    ("Patient", _patient_params(), "Holefamily"),
], ids=["allergy-668", "pressure-679", "consent-679", "unenumerated",
        "demographics"])
def test_commit_mode_refuses_every_row_on_a_step_up_token_alone(
        client, app, auth_headers, tenant_id, engine_builds_these_types,
        resource_type, params, secret):
    before = _rows(app, tenant_id, resource_type)
    resp = client.post(EXTRACT, headers=auth_headers, json=params)
    _assert_refused(resp, app, tenant_id, resource_type, before, secret)


def test_dry_run_still_previews_the_row(client, app, auth_headers, tenant_id,
                                        engine_builds_these_types):
    before = _rows(app, tenant_id, "AllergyIntolerance")
    resp = client.post(f"{EXTRACT}?dryRun=true", headers=auth_headers,
                       json=_allergy_params())
    assert resp.status_code == 200
    assert "peanut-hole" in resp.get_data(as_text=True)
    assert _rows(app, tenant_id, "AllergyIntolerance") == before


def test_dry_run_previews_the_pressure_too(client, app, auth_headers,
                                           tenant_id):
    before = _rows(app, tenant_id, "Observation")
    resp = client.post(f"{EXTRACT}?dryRun=true", headers=auth_headers,
                       json=_pressure_params())
    assert resp.status_code == 200
    assert resp.get_json()["parameter"][0]["resource"]["entry"][0][
        "resource"]["valueInteger"] == 191
    assert _rows(app, tenant_id, "Observation") == before


def test_nothing_is_cleared_to_commit_without_a_human():
    # Clearing a type is a decision, made here, on purpose, with a test.
    assert COMMIT_WITHOUT_CONFIRMATION == frozenset()
