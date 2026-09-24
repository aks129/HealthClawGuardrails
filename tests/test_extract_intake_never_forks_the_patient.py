"""A committed intake form never forks the patient (#572), over HTTP.

The defect as filed: POST $extract (step-up token, not dryRun) with the
intake form stored a NEW Patient with a fresh uuid, and wrote the answer to
`allergies.item.allergen` into `Patient.code.text`, an element Patient does
not have. Each submission forked the record again.

The pins that closed it are engine-level (tests/test_sdc_extract.py,
tests/test_extract_definition_names_a_real_element.py) or use a hand-built
questionnaire (tests/test_extract_refuses_clinical_rows_on_step_up_alone.py).
This drives the path the issue names end to end: the canonical intake
Questionnaire stored in the tenant, `$populate` for a stored Patient, the
person typing an allergen into the populated form, and commit-mode
`$extract` submitted twice, then reads the tenant's rows back.

MUTATION: r6/sdc/extract.py, drop the `target_type == "Patient" and
subject_ref` early return in _extract_by_definition -> red (the bundle
carries a Patient; commit mode refuses it with 422, since nothing is
cleared to commit without a human). Drop the refusal in r6/sdc/routes.py
too -> red (each submission stores a new Patient). Drop, as well, the
definition type check and the element list, and let any path write as a
nested dict -> red, and the forked Patient carries `code.text` holding the
allergen: the defect as filed. The refusal dropped alone stays green: for a
subject-bound response the subject check is what holds, and the refusal
is the second line behind it.
"""

import json

from r6.models import R6Resource, db
from r6.sdc.intake import intake_questionnaire

POPULATE = "/r6/fhir/Questionnaire/healthclaw-intake/$populate"
EXTRACT = "/r6/fhir/QuestionnaireResponse/$extract"
ALLERGEN = "allergen-answer-572"


def _store(app, resource, tenant_id):
    with app.app_context():
        db.session.add(R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource["id"],
            tenant_id=tenant_id))
        db.session.commit()


def _rows(app, tenant_id):
    # The id is the column's: a forked row's JSON carries none. Sorted, since
    # a query without ORDER BY promises no order and the Postgres lane runs.
    with app.app_context():
        return sorted(
            ((r.resource_type, r.id, json.loads(r.resource_json))
             for r in R6Resource.query.filter_by(tenant_id=tenant_id)),
            key=lambda row: (row[0], row[1]))


def _walk(items):
    for item in items:
        yield item
        yield from _walk(item.get("item", []))


def test_submitting_the_intake_twice_leaves_one_patient_and_no_allergen_on_it(
        client, app, tenant_id, tenant_headers, auth_headers):
    _store(app, {"resourceType": "Patient", "id": "p-572",
                 "name": [{"given": ["Synthia"], "family": "Synthetic"}],
                 "birthDate": "1990-01-01"}, tenant_id)
    # Text-only on purpose: the server has no allergen vocabulary, so the
    # populated row has no allergen answer and the person types one in.
    _store(app, {"resourceType": "AllergyIntolerance", "id": "a-572",
                 "patient": {"reference": "Patient/p-572"},
                 "code": {"text": "text-only allergy"}}, tenant_id)
    _store(app, intake_questionnaire(), tenant_id)

    pop = client.post(POPULATE, headers=tenant_headers, json={
        "resourceType": "Parameters", "parameter": [
            {"name": "subject",
             "valueReference": {"reference": "Patient/p-572"}}]})
    assert pop.status_code == 200, pop.get_data(as_text=True)[:300]
    qr = next(p["resource"] for p in pop.get_json()["parameter"]
              if p["name"] == "response")
    assert qr["subject"]["reference"] == "Patient/p-572"
    allergen = next(i for i in _walk(qr["item"])
                    if i["linkId"] == "allergies.item.allergen")
    allergen["answer"] = [{"valueString": ALLERGEN}]
    qr["status"] = "completed"

    before = _rows(app, tenant_id)
    for _ in range(2):
        ext = client.post(EXTRACT, headers=auth_headers, json={
            "resourceType": "Parameters", "parameter": [
                {"name": "questionnaire-response", "resource": qr}]})
        assert ext.status_code == 200, ext.get_data(as_text=True)[:300]
        bundle = ext.get_json()["parameter"][0]["resource"]
        assert bundle["entry"] == []
    after = _rows(app, tenant_id)

    patients = [(rid, r) for t, rid, r in after if t == "Patient"]
    assert [rid for rid, _ in patients] == ["p-572"]     # never forked
    assert all("code" not in r for _, r in patients)     # no Patient.code
    assert after == before                               # nothing written
    assert ALLERGEN not in json.dumps(after)
