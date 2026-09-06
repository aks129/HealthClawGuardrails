"""$extract commit mode refuses to write allergies, conditions or
medications on a step-up token alone (#572, the class the CTO design pass
named worse than #214's header).

Nothing on the human-gated path calls $extract: the form-fill executor
never extracts, and $extract's callers are the raw endpoint (step-up only)
and the MCP write tool. Measured on main before this change: a
Questionnaire whose root definitionExtract names AllergyIntolerance, with
definitions for clinicalStatus, verificationStatus, patient and code.text,
answered with plain strings, POSTed to commit mode with a step-up token
and answered 200 with one AllergyIntolerance row stored, its statuses and
patient reference written as bare strings the validator waved through by
truthiness. A clinical write with no human confirmation, on the credential
an agent holds.

Commit mode now refuses a bundle carrying any of the three types with a
422 naming the form-fill rail and dryRun; dryRun still previews them. The
refusal writes no audit row (consistent with the kernel's own refusals):
the audit trail does not cover refused attempts here.

MUTATION: r6/sdc/routes.py, drop the refusal -> red (the row is stored and
the response is 200).
"""

from r6.sdc.extract import RAIL_ONLY_TYPES

SD = "http://hl7.org/fhir/StructureDefinition/AllergyIntolerance"
DEF_EXTRACT = ("http://hl7.org/fhir/uv/sdc/StructureDefinition/"
               "sdc-questionnaire-definitionExtract")


def _legacy_allergy_questionnaire():
    return {"resourceType": "Questionnaire", "status": "active",
            "extension": [{"url": DEF_EXTRACT, "valueCode": "AllergyIntolerance"}],
            "item": [
                {"linkId": "cs", "type": "string", "definition": f"{SD}#AllergyIntolerance.clinicalStatus"},
                {"linkId": "vs", "type": "string", "definition": f"{SD}#AllergyIntolerance.verificationStatus"},
                {"linkId": "pt", "type": "string", "definition": f"{SD}#AllergyIntolerance.patient"},
                {"linkId": "al", "type": "string", "definition": f"{SD}#AllergyIntolerance.code.text"},
            ]}


def _response():
    return {"resourceType": "QuestionnaireResponse", "status": "completed",
            "item": [{"linkId": "cs", "answer": [{"valueString": "active"}]},
                     {"linkId": "vs", "answer": [{"valueString": "unconfirmed"}]},
                     {"linkId": "pt", "answer": [{"valueString": "Patient/p-hole"}]},
                     {"linkId": "al", "answer": [{"valueString": "peanut-hole"}]}]}


def _params():
    return {"resourceType": "Parameters", "parameter": [
        {"name": "questionnaire-response", "resource": _response()},
        {"name": "questionnaire", "resource": _legacy_allergy_questionnaire()}]}


def _rows(app, tenant_id):
    from r6.models import R6Resource
    with app.app_context():
        return R6Resource.query.filter_by(
            tenant_id=tenant_id, resource_type="AllergyIntolerance").count()


def test_commit_mode_refuses_a_clinical_row_on_a_step_up_token_alone(
        client, app, auth_headers, tenant_id):
    before = _rows(app, tenant_id)
    resp = client.post("/r6/fhir/QuestionnaireResponse/$extract",
                       headers=auth_headers, json=_params())
    assert resp.status_code == 422, resp.get_data(as_text=True)[:200]
    outcome = resp.get_json()
    assert outcome["resourceType"] == "OperationOutcome"
    diag = outcome["issue"][0]["diagnostics"]
    assert "form-fill" in diag and "dryRun" in diag
    assert "peanut-hole" not in diag
    assert _rows(app, tenant_id) == before


def test_dry_run_still_previews_the_row(client, app, auth_headers, tenant_id):
    before = _rows(app, tenant_id)
    resp = client.post("/r6/fhir/QuestionnaireResponse/$extract?dryRun=true",
                       headers=auth_headers, json=_params())
    assert resp.status_code == 200
    assert "peanut-hole" in resp.get_data(as_text=True)
    assert _rows(app, tenant_id) == before


def test_the_refused_types_are_the_three_rail_only_types():
    assert RAIL_ONLY_TYPES == frozenset({"AllergyIntolerance", "Condition",
                                         "MedicationRequest"})
