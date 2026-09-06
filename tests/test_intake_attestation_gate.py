"""The intake review gate, where "no known allergies" is decided (#667).

The gate has always refused silence: an absence of allergies is never
inferred, so a submit that neither confirms a row nor affirms the attestation
is a 422. What it also accepted, until this module, was the opposite defect —
both answers at once. A response carrying a confirmed allergy *and* the
attestation is not a stricter statement of the same fact; the two contradict,
and the extraction engine resolves the contradiction structurally by writing
the row and ignoring the attestation. The record would then hold an allergy
while the response it came from says there are none.
"""
import json

import pytest

from models import db
from r6.models import R6Resource

TENANT = 'test-tenant'
PATIENT_ID = 'gate-patient-1'
PATIENT_REF = 'Patient/%s' % PATIENT_ID


def _store(resource, tenant_id):
    row = R6Resource(resource_type=resource['resourceType'],
                     resource_json=json.dumps(resource),
                     resource_id=resource.get('id'), tenant_id=tenant_id)
    db.session.add(row)
    db.session.commit()


def _patient():
    return {'resourceType': 'Patient', 'id': PATIENT_ID,
            'name': [{'family': 'Gatekeeper', 'given': ['Ada']}],
            'gender': 'female', 'birthDate': '1962-03-04'}


def _medication():
    return {'resourceType': 'MedicationRequest', 'id': 'gate-med-1',
            'status': 'active', 'intent': 'order',
            'medicationCodeableConcept': {
                'coding': [{'system': 'http://www.nlm.nih.gov/research/umls/rxnorm',
                            'code': '860975'}]},
            'subject': {'reference': PATIENT_REF}}


def _allergy():
    return {'resourceType': 'AllergyIntolerance', 'id': 'gate-allergy-1',
            'code': {'coding': [{'system': 'http://snomed.info/sct',
                                 'code': '227493005'}]},
            'patient': {'reference': PATIENT_REF}}


@pytest.fixture
def intake_ready(app, tenant_headers):
    """A patient with one medication and one allergy to review."""
    with app.app_context():
        for resource in (_patient(), _medication(), _allergy()):
            _store(resource, tenant_headers['X-Tenant-Id'])


def _committed_action(client, tenant_headers, auth_headers):
    proposed = client.post('/r6/actions/propose', headers=tenant_headers,
                           json={'kind': 'form-fill',
                                 'payload': {'to': 'Intake portal',
                                             'questionnaire': 'healthclaw-intake',
                                             'body': 'new patient intake form'}})
    assert proposed.status_code == 201, proposed.get_data(as_text=True)
    action_id = proposed.get_json()['id']
    committed = client.post('/r6/actions/%s/commit' % action_id,
                            headers=auth_headers)
    assert committed.status_code == 202, committed.get_data(as_text=True)
    return action_id


def _review(client, auth_headers, action_id, **answers):
    return client.post('/r6/actions/%s/review' % action_id,
                       headers=auth_headers, json=answers)


def test_the_gate_refuses_the_attestation_beside_a_confirmed_allergy(
        client, app, tenant_headers, auth_headers, action_registry,
        intake_ready):
    """MUTATION: drop the (3a) branch in r6/actions/review.py -> green here."""
    action_id = _committed_action(client, tenant_headers, auth_headers)

    refused = _review(client, auth_headers, action_id,
                      **{'med-0': 'yes', 'allergy-0': 'confirm', 'nka': 'on'})

    assert refused.status_code == 422, refused.get_data(as_text=True)
    message = refused.get_json()['error']
    # The refusal has to say which two things collide and what to do about
    # them; "invalid submission" sends the person back to guess.
    assert 'confirmed an allergy' in message, message
    assert 'No known allergies' in message, message
    assert 'uncheck the box' in message, message


def test_nothing_is_recorded_when_the_contradiction_is_refused(
        client, app, tenant_headers, auth_headers, action_registry,
        intake_ready):
    """A refusal that still writes the response is the defect with a 422 on
    top: the extraction engine reads the stored response, not the status."""
    action_id = _committed_action(client, tenant_headers, auth_headers)
    with app.app_context():
        before = R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse', tenant_id=TENANT).count()

    refused = _review(client, auth_headers, action_id,
                      **{'med-0': 'yes', 'allergy-0': 'confirm', 'nka': 'on'})
    assert refused.status_code == 422, refused.get_data(as_text=True)

    with app.app_context():
        after = R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse', tenant_id=TENANT).count()
    assert after == before, (
        'the refused submit stored a reviewed QuestionnaireResponse; the '
        'contradiction reaches the extractor whatever the caller was told')


def test_a_confirmed_allergy_alone_is_still_accepted(
        client, app, tenant_headers, auth_headers, action_registry,
        intake_ready):
    action_id = _committed_action(client, tenant_headers, auth_headers)

    accepted = _review(client, auth_headers, action_id,
                       **{'med-0': 'yes', 'allergy-0': 'confirm'})

    assert accepted.status_code == 200, accepted.get_data(as_text=True)
    assert accepted.get_json()['reviewed_qr_id']


def test_the_attestation_alone_is_still_accepted(
        client, app, tenant_headers, auth_headers, action_registry,
        intake_ready):
    """The reverse case the issue keeps: every row removed, NKA affirmed."""
    action_id = _committed_action(client, tenant_headers, auth_headers)

    accepted = _review(client, auth_headers, action_id,
                       **{'med-0': 'yes', 'allergy-0': 'remove', 'nka': 'on'})

    assert accepted.status_code == 200, accepted.get_data(as_text=True)
    assert accepted.get_json()['reviewed_qr_id']


def test_silence_about_allergies_is_still_refused(
        client, app, tenant_headers, auth_headers, action_registry,
        intake_ready):
    """The older half of the gate, pinned here so this module fails if the
    new branch is written in a way that swallows it."""
    action_id = _committed_action(client, tenant_headers, auth_headers)

    refused = _review(client, auth_headers, action_id,
                      **{'med-0': 'yes', 'allergy-0': 'remove'})

    assert refused.status_code == 422, refused.get_data(as_text=True)
    assert 'never assumed' in refused.get_json()['error']
