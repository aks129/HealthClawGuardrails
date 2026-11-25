"""The intake profile builds a Patient's identity fields element by element
(#964).

Intake keeps a Patient's name, birthDate, address and telecom so a clinic can
identify the patient. Copying those four fields by key also shipped
everything nested in them: an extension's `valueReference.display`,
`name.text`, `address.text`, a telecom extension. Upstream `display`/`text`
is never preserved (owner ruling), so both copies of the profile — the
route's `_intake_strip` and the kernel's `_intake_profile` — now rebuild the
fields from the allow-list the check-in export uses (#963 F5).

MUTATION: copy name/address/telecom by key in _intake_strip again ->
test_the_intake_share_bundle_drops_nested_display_and_text reddens.
MUTATION: copy them by key in _intake_profile again ->
test_the_intake_profile_drops_nested_display_and_text reddens.
"""

import json

from flask import Response

from models import db
from r6.access import Profile, fhir_response
from r6.models import R6Resource

PATIENT_ID = 'intake-964-pt'

CANARIES = (
    'CANARY-964-address-ext-display',
    'CANARY-964-name-text',
    'CANARY-964-address-text',
    'CANARY-964-telecom-ext',
    'CANARY-964-name-ext',
)


def _patient():
    """A synthetic Patient with a canary in every nested string the
    allow-list must drop."""
    return {
        'resourceType': 'Patient',
        'id': PATIENT_ID,
        'name': [{'use': 'official', 'family': 'Testperson',
                  'given': ['Ana'], 'text': 'CANARY-964-name-text',
                  'period': {'start': '2001-01-01'},
                  'extension': [{'url': 'https://x.example/n',
                                 'valueString': 'CANARY-964-name-ext'}]}],
        'birthDate': '1980-02-03',
        'address': [{'line': ['1 Synthetic Way'], 'city': 'Testville',
                     'state': 'TX', 'postalCode': '00000',
                     'text': 'CANARY-964-address-text',
                     'extension': [{
                         'url': 'https://x.example/a',
                         'valueReference': {
                             'reference': 'RelatedPerson/rp1',
                             'display': 'CANARY-964-address-ext-display'}}]}],
        'telecom': [{'system': 'phone', 'value': '555-0100', 'use': 'home',
                     'extension': [{'url': 'https://x.example/t',
                                    'valueString': 'CANARY-964-telecom-ext'}]}],
    }


KEPT = {
    'name': [{'use': 'official', 'family': 'Testperson', 'given': ['Ana']}],
    'birthDate': '1980-02-03',
    'address': [{'line': ['1 Synthetic Way'], 'city': 'Testville',
                 'state': 'TX', 'postalCode': '00000'}],
    'telecom': [{'system': 'phone', 'value': '555-0100', 'use': 'home'}],
}


def _assert_identity_rebuilt(patient, raw):
    for canary in CANARIES:
        assert canary not in raw, canary
    for key, value in KEPT.items():
        assert patient[key] == value, key


def test_the_intake_share_bundle_drops_nested_display_and_text(
        app, client, auth_headers, tenant_id):
    with app.app_context():
        db.session.add(R6Resource(resource_type='Patient',
                                  resource_id=PATIENT_ID,
                                  resource_json=json.dumps(_patient()),
                                  tenant_id=tenant_id))
        db.session.commit()
    resp = client.post('/r6/fhir/$share-bundle', headers=auth_headers,
                       json={'patient_id': PATIENT_ID,
                             'resource_types': ['Patient'],
                             'profile': 'intake'})
    assert resp.status_code == 200
    raw = resp.get_data(as_text=True)
    patient, = [e['resource'] for e in json.loads(raw)['entry']]
    _assert_identity_rebuilt(patient, raw)


def test_the_intake_profile_drops_nested_display_and_text(app):
    with app.test_request_context():
        response = fhir_response(_patient(), profile=Profile.INTAKE)
    assert isinstance(response, Response)
    raw = response.get_data(as_text=True)
    _assert_identity_rebuilt(json.loads(raw), raw)
