"""$extract builds one clinical resource per unsourced repeating-group row,
and the raw endpoint refuses to commit them on step-up alone (#572 part 2B2).

Nothing on the human-gated path calls $extract: the form-fill executor never
extracts, and $extract's callers are the raw endpoint (step-up only) and the
MCP write tool. So the engine builds the rows for dryRun previews and for
the executor to commit after confirmation (deferred on #572), and commit
mode on the raw endpoint refuses a bundle carrying them rather than writing
clinical data on a step-up token alone, or dropping silently what the
preview showed.

A row the populate engine sourced from a stored resource carries a marker
(#572 part 2B1) and IS the record: confirming it writes nothing. Only an
unsourced row yields a resource. The no-known-allergies attestation has no
definition and yields nothing; an empty group yields nothing; "no known
allergies" is never synthesized.

MUTATIONS: index rows by linkId (last-wins) -> the two-rows pin red; ignore
the marker -> the sourced-row pin red; drop the refusal in
r6/sdc/routes.py -> the raw-commit pin red; drop the audit call in
_commit_bundle -> the audit pin red.
"""

import json

from r6.sdc.extract import RAIL_ONLY_TYPES, extract_resources
from r6.sdc.populate import POPULATED_ROW_SOURCE_URL

from tests.test_populate_lists import intake_questionnaire

SUBJECT = {"reference": "Patient/p-2b2"}


def _row(link_id, leaves, source=None):
    row = {"linkId": link_id, "item": [
        {"linkId": f"{link_id}.{leaf}", "answer": [{"valueString": value}]}
        for leaf, value in leaves.items()]}
    if source:
        row["extension"] = [{"url": POPULATED_ROW_SOURCE_URL,
                             "valueReference": {"reference": source}}]
    return row


def _qr(*groups, subject=SUBJECT):
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "item": list(groups)}
    if subject:
        qr["subject"] = subject
    return qr


def _allergies(*rows, nka=None):
    children = []
    if nka is not None:
        children.append({"linkId": "allergies.no-known-allergies",
                         "answer": [{"valueBoolean": nka}]})
    children.extend(rows)
    return {"linkId": "allergies", "item": children}


def _by_type(bundle, resource_type):
    return [e for e in bundle["entry"] if e["resource"]["resourceType"] == resource_type]


def test_two_unsourced_allergy_rows_yield_two_allergy_intolerances():
    bundle = extract_resources(_qr(_allergies(
        _row("allergies.item", {"allergen": "peanut-2b2", "reaction": "hives"}),
        _row("allergies.item", {"allergen": "latex-2b2"}))), intake_questionnaire())
    entries = _by_type(bundle, "AllergyIntolerance")
    assert len(entries) == 2
    texts = sorted(e["resource"]["code"]["text"] for e in entries)
    assert texts == ["latex-2b2", "peanut-2b2"]
    for e in entries:
        r = e["resource"]
        assert r["patient"] == SUBJECT
        assert r["clinicalStatus"]["coding"][0]["code"] == "active"
        assert r["verificationStatus"]["coding"][0]["code"] == "unconfirmed"
        assert e["request"] == {"method": "POST", "url": "AllergyIntolerance"}
        assert e["fullUrl"].startswith("urn:uuid:")
    peanut = next(e["resource"] for e in entries if e["resource"]["code"]["text"] == "peanut-2b2")
    assert peanut["reaction"][0]["manifestation"][0]["text"] == "hives"


def test_a_sourced_row_is_never_recreated():
    bundle = extract_resources(_qr(_allergies(
        _row("allergies.item", {"allergen": "peanut-2b2"}, source="AllergyIntolerance/a-1"),
        _row("allergies.item", {"allergen": "latex-2b2"}))), intake_questionnaire())
    entries = _by_type(bundle, "AllergyIntolerance")
    assert [e["resource"]["code"]["text"] for e in entries] == ["latex-2b2"]


def test_nka_never_yields_a_resource_and_an_empty_group_yields_nothing():
    for group in (_allergies(nka=True), _allergies(nka=False), _allergies()):
        bundle = extract_resources(_qr(group), intake_questionnaire())
        assert bundle["entry"] == []
        assert "716186003" not in json.dumps(bundle)


def test_medication_and_condition_rows_carry_the_minimum_truthful_statuses():
    meds = {"linkId": "medications", "item": [
        _row("medications.item", {"name": "metformin-2b2", "dose": "twice daily"})]}
    conds = {"linkId": "conditions", "item": [
        _row("conditions.item", {"name": "hypertension-2b2"})]}
    bundle = extract_resources(_qr(meds, conds), intake_questionnaire())
    med = _by_type(bundle, "MedicationRequest")[0]["resource"]
    assert med["medicationCodeableConcept"]["text"] == "metformin-2b2"
    assert med["dosageInstruction"][0]["text"] == "twice daily"
    assert med["status"] == "active" and med["intent"] == "plan"
    assert med["reportedBoolean"] is True and med["subject"] == SUBJECT
    cond = _by_type(bundle, "Condition")[0]["resource"]
    assert cond["code"]["text"] == "hypertension-2b2" and cond["subject"] == SUBJECT


def test_a_row_without_a_subject_yields_nothing():
    bundle = extract_resources(_qr(_allergies(
        _row("allergies.item", {"allergen": "peanut-2b2"})), subject=None), intake_questionnaire())
    assert _by_type(bundle, "AllergyIntolerance") == []


def test_a_row_with_no_answered_leaf_yields_nothing():
    bundle = extract_resources(_qr(_allergies(
        {"linkId": "allergies.item", "item": []})), intake_questionnaire())
    assert bundle["entry"] == []


def test_the_rail_only_types_are_named():
    assert RAIL_ONLY_TYPES == frozenset({"AllergyIntolerance", "Condition", "MedicationRequest"})


# --- the raw endpoint -------------------------------------------------------

def _extract_params(qr, questionnaire):
    return {"resourceType": "Parameters", "parameter": [
        {"name": "questionnaire-response", "resource": qr},
        {"name": "questionnaire", "resource": questionnaire}]}


def test_raw_commit_refuses_rail_only_types_and_dry_run_previews_them(
        client, app, auth_headers, tenant_id):
    from r6.models import R6Resource
    qr = _qr(_allergies(_row("allergies.item", {"allergen": "peanut-2b2"})))
    q = intake_questionnaire()
    preview = client.post("/r6/fhir/QuestionnaireResponse/$extract?dryRun=true",
                          headers=auth_headers, json=_extract_params(qr, q))
    assert preview.status_code == 200
    assert "peanut-2b2" in preview.get_data(as_text=True)
    commit = client.post("/r6/fhir/QuestionnaireResponse/$extract",
                         headers=auth_headers, json=_extract_params(qr, q))
    assert commit.status_code == 422
    outcome = commit.get_json()
    assert outcome["resourceType"] == "OperationOutcome"
    diag = outcome["issue"][0]["diagnostics"]
    assert "form-fill" in diag and "dryRun" in diag
    assert "peanut-2b2" not in diag
    with app.app_context():
        assert R6Resource.query.filter_by(
            tenant_id=tenant_id, resource_type="AllergyIntolerance").count() == 0


def test_each_committed_resource_is_audited_in_the_same_transaction(
        client, app, auth_headers, tenant_id):
    """The observation-based path still commits (an Observation from a coded
    item); every written row gets its own audit event, in-transaction."""
    from r6.models import AuditEventRecord, R6Resource
    q = {"resourceType": "Questionnaire", "status": "active",
         "extension": [{"url": "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
                               "sdc-questionnaire-observationExtract",
                        "valueBoolean": True}],
         "item": [{"linkId": "w", "type": "integer",
                   "code": [{"system": "http://loinc.org", "code": "29463-7"}]}]}
    qr = _qr({"linkId": "w", "answer": [{"valueInteger": 70}]})
    resp = client.post("/r6/fhir/QuestionnaireResponse/$extract",
                       headers=auth_headers, json=_extract_params(qr, q))
    assert resp.status_code == 200, resp.get_json()
    with app.app_context():
        rows = R6Resource.query.filter_by(tenant_id=tenant_id, resource_type="Observation").all()
        assert len(rows) == 1
        audits = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type="Observation", resource_id=rows[0].id).all()
        assert len(audits) == 1
        assert audits[0].event_type == "create"
        assert "extract" in audits[0].detail and "70" not in audits[0].detail
