"""A data-quality fix may touch only what the evaluator can propose (#739).

`_apply_field_fix` walked any dot path and created what was missing, so an
approved "fix" could rewrite `subject.reference`, `id`, `meta` or
`extension`: a record moving between patients under a fix label. Refused as
a whole before any mutation; the linkage fields are settable only when
absent, and only to a Patient in this tenant.
"""
import json

import pytest

from r6.curatr import FIXABLE_ROOTS, fix_refusal


def _confirmed(headers):
    # X-Human-Confirmed is the known gap (#214), used only to seed records.
    return {**headers, "X-Human-Confirmed": "true"}


def _seed(client, auth_headers, resource):
    r = client.post(f"/r6/fhir/{resource['resourceType']}",
                    data=json.dumps(resource), content_type="application/json",
                    headers=_confirmed(auth_headers))
    assert r.status_code == 201, r.get_data(as_text=True)


def _fix(client, auth_headers, rtype, rid, fixes):
    return client.post(f"/r6/fhir/{rtype}/{rid}/$curatr-apply-fix",
                       data=json.dumps({"fixes": fixes,
                                        "patient_intent": "test"}),
                       content_type="application/json",
                       headers=_confirmed(auth_headers))


CONDITION = {"resourceType": "Condition", "id": "c-bound",
             "subject": {"reference": "Patient/alice"},
             "code": {"coding": [{"system": "http://snomed.info/sct",
                                  "code": "44054006"}]},
             "clinicalStatus": {"coding": [{"code": "active"}]}}


@pytest.mark.parametrize("path,value", [
    ("Condition.subject.reference", "Patient/mallory"),
    ("Condition.id", "c-other"),
    ("Condition.meta.tag", "x"),
    ("Condition.extension", [{"url": "http://x", "valueString": "y"}]),
    ("Condition.resourceType", "Patient"),
    ("Condition.note", [{"text": "free text"}]),
])
def test_a_fix_outside_the_vocabulary_is_refused_whole(client, auth_headers,
                                                       tenant_headers,
                                                       path, value):
    _seed(client, auth_headers, CONDITION)
    r = _fix(client, auth_headers, "Condition", "c-bound",
             [{"field_path": "Condition.clinicalStatus.coding[0].code",
               "new_value": "active"},
              {"field_path": path, "new_value": value}])
    assert r.status_code == 422, r.get_data(as_text=True)
    assert r.get_json()["issue"][0]["code"] == "invalid"
    read = client.get("/r6/fhir/Condition/c-bound", headers=tenant_headers)
    body = read.get_json()
    assert body["subject"] == {"reference": "Patient/alice"}
    assert body["id"] == "c-bound"
    assert "meta" not in body or "tag" not in body["meta"]


def test_a_fix_the_evaluator_could_have_proposed_still_applies(
        client, auth_headers, tenant_headers):
    _seed(client, auth_headers, dict(CONDITION, id="c-ok"))
    r = _fix(client, auth_headers, "Condition", "c-ok",
             [{"field_path": "Condition.verificationStatus",
               "new_value": {"coding": [{"code": "confirmed"}]}}])
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["issues_fixed"] == 1


def _seed_unlinked_allergy(app, tenant_id, rid):
    """An allergy with no patient link cannot be created through the API
    (the validator requires it), which is the point: it arrives by ingest.
    Stored the way the ingester stores it."""
    from models import db
    from r6.models import R6Resource
    with app.app_context():
        db.session.add(R6Resource(
            "AllergyIntolerance",
            json.dumps({"resourceType": "AllergyIntolerance", "id": rid,
                        "code": {"coding": [{"code": "91935009"}]}}),
            resource_id=rid, tenant_id=tenant_id))
        db.session.commit()


def test_a_missing_patient_link_can_be_set_to_a_patient_in_this_tenant(
        client, app, auth_headers, tenant_id):
    _seed(client, auth_headers, {"resourceType": "Patient", "id": "p-here",
                                 "gender": "other"})
    _seed_unlinked_allergy(app, tenant_id, "a-unlinked")
    r = _fix(client, auth_headers, "AllergyIntolerance", "a-unlinked",
             [{"field_path": "AllergyIntolerance.patient",
               "new_value": {"reference": "Patient/p-here"}}])
    assert r.status_code == 200, r.get_data(as_text=True)


def test_a_missing_patient_link_cannot_name_a_patient_that_is_not_here(
        client, app, auth_headers, tenant_id):
    _seed_unlinked_allergy(app, tenant_id, "a-unlinked-2")
    r = _fix(client, auth_headers, "AllergyIntolerance", "a-unlinked-2",
             [{"field_path": "AllergyIntolerance.patient",
               "new_value": {"reference": "Patient/nobody"}}])
    assert r.status_code == 422


def test_a_present_patient_link_is_never_re_pointed():
    refusal = fix_refusal("AllergyIntolerance",
                          {"patient": {"reference": "Patient/alice"}},
                          [{"field_path": "AllergyIntolerance.patient",
                            "new_value": {"reference": "Patient/alice"}}],
                          "t")
    assert refusal and "already set" in refusal


def test_every_type_the_evaluator_knows_has_a_vocabulary():
    import r6.curatr
    src = open(r6.curatr.__file__).read()
    for rtype in ("Condition", "AllergyIntolerance", "MedicationRequest",
                  "Immunization", "Procedure", "DiagnosticReport"):
        assert f'resource_type == "{rtype}"' in src
        assert rtype in FIXABLE_ROOTS, rtype
