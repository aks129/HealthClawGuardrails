"""Security-tester probes against PR #1167 (SDC $populate / $extract leaks).

Synthetic canaries only. Tests named `test_exploit_*` assert the SAFE
behaviour, so a red one is a finding. Tests named `test_holds_*` pin an
attack that the PR already defeats.
"""

import json

from models import db
from r6.models import R6Resource
from r6.sdc.extract import DEFINITION_EXTRACT_URL, OBSERVATION_EXTRACT_URL
from r6.sdc.intake import intake_questionnaire

PREFIX = "ZQATK"
FAMILY = PREFIX + "FAMILY"
GIVEN = PREFIX + "GIVEN"
DISPLAY = PREFIX + "QDISPLAY"
EXT_TEXT = PREFIX + "QEXTTEXT"
INITIAL = PREFIX + "INITIAL"
CONTAINED = PREFIX + "CONTAINED"
LOINC = "http://loinc.org"
INITIAL_EXPRESSION_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-initialExpression")


def _store(app, resource, tenant_id):
    with app.app_context():
        db.session.add(R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource["id"], tenant_id=tenant_id))
        db.session.commit()


def _upstream_questionnaire(qid="atk-upstream-q"):
    """A Questionnaire as an ingested feed could hold it: an item.code whose
    display carries a person's name, and an extension inside the Coding."""
    return {
        "resourceType": "Questionnaire", "id": qid, "status": "active",
        "extension": [{"url": OBSERVATION_EXTRACT_URL, "valueBoolean": True}],
        "item": [{"linkId": "chol", "type": "quantity",
                  "initial": [{"valueString": INITIAL}],
                  "code": [{"system": LOINC, "code": "2093-3",
                            "display": DISPLAY,
                            "extension": [{"url": "http://example.org/x",
                                           "valueString": EXT_TEXT}]}]}],
    }


def _inline_qr(qid="atk-upstream-q"):
    return {"resourceType": "QuestionnaireResponse", "status": "completed",
            "questionnaire": f"Questionnaire/{qid}",
            "item": [{"linkId": "chol", "answer": [{"valueQuantity": {
                "value": 1, "unit": "mg/dL"}}]}]}


# ---------------------------------------------------------------------------
# Concern 3: inline response + stored Questionnaire
# ---------------------------------------------------------------------------

def test_baseline_get_questionnaire_hides_upstream_display(
        client, app, tenant_id, tenant_headers):
    """A plain read of the stored Questionnaire hides the display."""
    _store(app, _upstream_questionnaire(), tenant_id)
    r = client.get("/r6/fhir/Questionnaire/atk-upstream-q",
                   headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_data(as_text=True)
    assert DISPLAY not in body
    assert EXT_TEXT not in body


def test_exploit_inline_qr_dry_run_reads_stored_questionnaire_display(
        client, app, tenant_id, tenant_headers):
    """Read tier, no step-up: an inline response naming a stored
    Questionnaire gets that Questionnaire's item.code back verbatim, which
    a GET of the same Questionnaire hides (concern 3 in the PR body)."""
    _store(app, _upstream_questionnaire(), tenant_id)
    r = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract?dryRun=true",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "questionnaire-response", "resource": _inline_qr()}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_data(as_text=True)
    assert DISPLAY not in body, "upstream display echoed to a read-tier caller"
    assert EXT_TEXT not in body, "Coding extension echoed to a read-tier caller"


def test_exploit_inline_qr_on_stored_id_route_skips_exit_redaction(
        client, app, tenant_id, tenant_headers):
    """The id-bearing route with an inline response takes the inline branch,
    so `from_store` is False and the exit redaction is skipped."""
    _store(app, _upstream_questionnaire(), tenant_id)
    r = client.post(
        "/r6/fhir/QuestionnaireResponse/anything/$extract?dryRun=true",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "questionnaire-response", "resource": _inline_qr()}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert DISPLAY not in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Attacks the PR defeats
# ---------------------------------------------------------------------------

def _patient(pid="atk-p1"):
    return {"resourceType": "Patient", "id": pid,
            "name": [{"family": FAMILY, "given": [GIVEN], "text": FAMILY}],
            "gender": "female", "birthDate": "1911-11-11",
            "telecom": [{"system": "email", "value": PREFIX + "@x.org"}],
            "contact": [{"name": {"family": FAMILY}}],
            "contained": [{"resourceType": "Patient", "id": "c",
                           "name": [{"family": CONTAINED}]}],
            "address": [{"line": [PREFIX + "LINE"], "state": "NY"}]}


def _expr_q(expressions):
    return {"resourceType": "Questionnaire", "status": "active",
            "item": [{"linkId": f"e{i}", "type": "string",
                      "initial": [{"valueString": INITIAL}],
                      "extension": [{"url": INITIAL_EXPRESSION_URL,
                                     "valueExpression": {
                                         "language": "text/fhirpath",
                                         "expression": e}}]}
                     for i, e in enumerate(expressions)]}


_SMUGGLE = [
    "%patient.name.family", "%subject.name.given", "Patient.name.family",
    "%patient.telecom.value", "%patient.address.line",
    "%patient.descendants().ofType(string)",
    "%patient.children().children()", "%context.name",
    "%resource.name.family", "%patient.contact.name.family",
    "%patient.contained.name.family", "%patient.birthDate",
    "%patient.name.family.substring(0,1)",
    "iif(%patient.name.exists(), 'Y', 'N')",
]


def test_holds_expression_smuggling_stored_subject(
        client, app, tenant_id, tenant_headers):
    _store(app, _patient(), tenant_id)
    r = client.post(
        "/r6/fhir/Questionnaire/$populate", headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "subject",
             "valueReference": {"reference": "Patient/atk-p1"}},
            {"name": "questionnaire", "resource": _expr_q(_SMUGGLE)}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_data(as_text=True)
    assert PREFIX not in body, body
    assert "1911-11-11" not in body
    # The name-existence oracle answers N: the name is gone, not stubbed.
    assert '"valueString": "N"' in json.dumps(r.get_json())


def test_holds_stored_questionnaire_initial_not_echoed(
        client, app, tenant_id, tenant_headers):
    _store(app, _patient(), tenant_id)
    _store(app, _upstream_questionnaire(), tenant_id)
    r = client.post(
        "/r6/fhir/Questionnaire/atk-upstream-q/$populate",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "subject",
             "valueReference": {"reference": "Patient/atk-p1"}}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert PREFIX not in r.get_data(as_text=True)


def test_holds_cross_tenant_subject_and_response(
        client, app, other_tenant_headers, tenant_id):
    _store(app, _patient(), tenant_id)
    r = client.post(
        "/r6/fhir/Questionnaire/$populate", headers=other_tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "subject",
             "valueReference": {"reference": "Patient/atk-p1"}},
            {"name": "questionnaire", "resource": intake_questionnaire()}]})
    assert PREFIX not in r.get_data(as_text=True)
    _store(app, {"resourceType": "QuestionnaireResponse", "id": "atk-qr",
                 "status": "completed",
                 "item": [{"linkId": "x",
                           "answer": [{"valueString": FAMILY}]}]}, tenant_id)
    r = client.post("/r6/fhir/QuestionnaireResponse/atk-qr/$extract"
                     "?dryRun=true", headers=other_tenant_headers, json={})
    assert r.status_code in (400, 404), r.status_code
    assert PREFIX not in r.get_data(as_text=True)


def test_holds_stored_response_with_contained_patient(
        client, app, tenant_id, tenant_headers):
    _store(app, {
        "resourceType": "QuestionnaireResponse", "id": "atk-qr",
        "status": "completed",
        # Subject-less on purpose: a subject suppresses the Patient row
        # (#572), which would make this check vacuous.
        "contained": [_patient("cp")],
        "author": {"display": FAMILY},
        "item": [{"linkId": "demographics.family-name", "text": FAMILY,
                  "answer": [{"valueString": FAMILY}]},
                 {"linkId": "demographics.gender",
                  "answer": [{"valueCoding": {"code": "female",
                                              "display": FAMILY}}]}],
    }, tenant_id)
    q = {"resourceType": "Questionnaire", "status": "active",
         "extension": [{"url": DEFINITION_EXTRACT_URL,
                        "valueCode": "Patient"}],
         "item": [{"linkId": "demographics.family-name",
                   "definition": "Patient#Patient.name.family"},
                  {"linkId": "demographics.gender",
                   "definition": "Patient#Patient.gender"}]}
    r = client.post(
        "/r6/fhir/QuestionnaireResponse/atk-qr/$extract?dryRun=true",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "questionnaire", "resource": q}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert PREFIX not in r.get_data(as_text=True)
    # Non-vacuous: the Patient row is built (from the coded gender).
    entries = next(p["resource"] for p in r.get_json()["parameter"]
                   if p["name"] == "return")["entry"]
    assert [e["resource"]["resourceType"] for e in entries] == ["Patient"]


def test_holds_populate_fails_closed_on_audit_error(
        client, app, tenant_id, tenant_headers, monkeypatch):
    import r6.sdc.routes as routes

    def boom(*a, **k):
        raise RuntimeError("audit down")
    monkeypatch.setattr(routes, "add_audit_event", boom)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    _store(app, _patient(), tenant_id)
    r = client.post(
        "/r6/fhir/Questionnaire/$populate", headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "subject",
             "valueReference": {"reference": "Patient/atk-p1"}},
            {"name": "questionnaire", "resource": _expr_q(
                ["%patient.gender"])}]})
    assert r.status_code >= 500
    assert "female" not in r.get_data(as_text=True)
    r = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract?dryRun=true",
        headers=tenant_headers,
        json={"resourceType": "Parameters", "parameter": [
            {"name": "questionnaire-response", "resource": _inline_qr()},
            {"name": "questionnaire",
             "resource": _upstream_questionnaire()}]})
    assert r.status_code >= 500
    assert "2093-3" not in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Form-fill: the human path keeps the name; the agent gets none of it
# ---------------------------------------------------------------------------

from tests.test_redaction_probes_multistep import (  # noqa: E402
    NAME_MARKER, _confirm_the_form, _markers_in, form_fill_ready)  # noqa: F401


def test_holds_form_fill_status_and_reviewed_qr_extract_carry_no_name(
        client, app, tenant_headers, auth_headers, action_registry,
        form_fill_ready):  # noqa: F811
    action_id, confirm = _confirm_the_form(client, tenant_headers,
                                           auth_headers)
    outcome = json.loads(confirm.get_json()["outcome_summary"])
    qr_id = outcome["questionnaire_response_id"]

    # Non-vacuous: the reviewed response the human signed holds the name.
    with app.app_context():
        row = R6Resource.query.filter_by(
            resource_type="QuestionnaireResponse", id=qr_id).first()
        assert NAME_MARKER in row.resource_json

    # The agent's view of the action: status and the full action record.
    status = client.get(f"/r6/actions/{action_id}", headers=tenant_headers)
    assert status.status_code == 200, status.get_data(as_text=True)
    assert _markers_in(status.get_data(as_text=True)) == set()
    assert _markers_in(confirm.get_data(as_text=True)) == set()

    # The reviewed response, read and dry-run extracted at read tier, with
    # the stock form and with a relocating Questionnaire.
    read = client.get(f"/r6/fhir/QuestionnaireResponse/{qr_id}",
                      headers=tenant_headers)
    assert _markers_in(read.get_data(as_text=True)) == set()
    for q in (intake_questionnaire(), {
              "resourceType": "Questionnaire", "status": "active",
                     "extension": [{"url": OBSERVATION_EXTRACT_URL,
                                    "valueBoolean": True}],
                     "item": [{"linkId": "demographics.family-name",
                               "code": [{"system": LOINC,
                                         "code": "8302-2"}]}]}):
        params = {"resourceType": "Parameters", "parameter": (
            [{"name": "questionnaire", "resource": q}] if q else [])}
        r = client.post(
            f"/r6/fhir/QuestionnaireResponse/{qr_id}/$extract?dryRun=true",
            headers=tenant_headers, json=params)
        assert r.status_code == 200, r.get_data(as_text=True)
        assert _markers_in(r.get_data(as_text=True)) == set(), q
