"""The intake profile builds a Patient's identity fields element by element
(#964).

Intake keeps a Patient's name, birthDate, address and telecom so a clinic can
identify the patient. Copying those four fields by key also shipped
everything nested in them: an extension's `valueReference.display`,
`name.text`, `address.text`, a telecom extension. Upstream `display`/`text`
is never preserved (owner ruling), so both copies of the profile — the
route's `_intake_strip` and the kernel's `_intake_profile` — now rebuild the
fields from the allow-list the check-in export uses (#963 F5). That rule is
`r6.access.patient_identity`, the one copy both call.

A HumanName or Address that carries only `text` has nothing on the
allow-list, so the whole entry is dropped, on purpose: the alternative is to
ship upstream text.
test_a_name_or_address_with_only_text_is_dropped_whole pins it.

MUTATION: copy name/address/telecom by key in _intake_strip again ->
test_the_intake_share_bundle_drops_nested_display_and_text reddens.
MUTATION: copy them by key in _intake_profile again ->
test_the_intake_profile_drops_nested_display_and_text reddens.
"""

import json

from flask import Response

from models import db
from r6.access import Profile, fhir_response, patient_identity
from r6.models import R6Resource

PATIENT_ID = 'intake-964-pt'

CANARIES = (
    'CANARY-964-address-ext-display',
    'CANARY-964-name-text',
    'CANARY-964-address-text',
    'CANARY-964-telecom-ext',
    'CANARY-964-name-ext',
    'CANARY-964-name-period',
    'CANARY-964-address-period',
    'CANARY-964-telecom-period',
)


def _patient():
    """A synthetic Patient with a canary in every nested string the
    allow-list must drop."""
    return {
        'resourceType': 'Patient',
        'id': PATIENT_ID,
        'name': [{'use': 'official', 'family': 'Testperson',
                  'given': ['Ana'], 'text': 'CANARY-964-name-text',
                  'period': {'start': 'CANARY-964-name-period'},
                  'extension': [{'url': 'https://x.example/n',
                                 'valueString': 'CANARY-964-name-ext'}]}],
        'birthDate': '1980-02-03',
        'address': [{'line': ['1 Synthetic Way'], 'city': 'Testville',
                     'state': 'TX', 'postalCode': '00000',
                     'text': 'CANARY-964-address-text',
                     'period': {'start': 'CANARY-964-address-period'},
                     'extension': [{
                         'url': 'https://x.example/a',
                         'valueReference': {
                             'reference': 'RelatedPerson/rp1',
                             'display': 'CANARY-964-address-ext-display'}}]}],
        'telecom': [{'system': 'phone', 'value': '555-0100', 'use': 'home',
                     'period': {'start': 'CANARY-964-telecom-period'},
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


def _intake_share(app, client, auth_headers, tenant_id, patient):
    """The one Patient an intake `$share-bundle` returns, and the raw body."""
    with app.app_context():
        db.session.add(R6Resource(resource_type='Patient',
                                  resource_id=PATIENT_ID,
                                  resource_json=json.dumps(patient),
                                  tenant_id=tenant_id))
        db.session.commit()
    resp = client.post('/r6/fhir/$share-bundle', headers=auth_headers,
                       json={'patient_id': PATIENT_ID,
                             'resource_types': ['Patient'],
                             'profile': 'intake'})
    assert resp.status_code == 200
    raw = resp.get_data(as_text=True)
    out, = [e['resource'] for e in json.loads(raw)['entry']]
    return out, raw


def test_the_intake_share_bundle_drops_nested_display_and_text(
        app, client, auth_headers, tenant_id):
    patient, raw = _intake_share(app, client, auth_headers, tenant_id,
                                 _patient())
    _assert_identity_rebuilt(patient, raw)


def test_the_intake_profile_drops_nested_display_and_text(app):
    with app.test_request_context():
        response = fhir_response(_patient(), profile=Profile.INTAKE)
    assert isinstance(response, Response)
    raw = response.get_data(as_text=True)
    _assert_identity_rebuilt(json.loads(raw), raw)


def test_patient_identity_returns_exactly_the_allow_listed_shape():
    """The public rule r6/routes.py imports; its output is a contract."""
    assert patient_identity(_patient()) == KEPT


def test_a_name_or_address_with_only_text_is_dropped_whole(
        app, client, auth_headers, tenant_id):
    """Deliberate (#964): `text` is upstream free text and is never shipped,
    so an entry with nothing else leaves no entry, and a field with no entry
    is absent rather than empty. The rest of the Patient still ships."""
    patient = _patient()
    patient['name'] = [{'text': 'CANARY-964-name-text'}]
    patient['address'] = [{'text': 'CANARY-964-address-text'}]
    out, raw = _intake_share(app, client, auth_headers, tenant_id, patient)
    assert 'name' not in out and 'address' not in out
    assert out['id'] == PATIENT_ID
    assert (out['birthDate'], out['telecom']) == (KEPT['birthDate'],
                                                  KEPT['telecom'])
    assert 'CANARY' not in raw


def test_a_value_of_the_wrong_type_under_an_allow_listed_key_is_dropped():
    """An allow-listed key is not enough; the value must be a string or a
    list of strings. A dict inside `given` drops `given`, not the name."""
    patient = _patient()
    patient['name'][0]['given'] = ['Ana', {'text': 'CANARY-964-given-dict'}]
    patient['address'][0]['city'] = {'text': 'CANARY-964-city-dict'}
    out = patient_identity(patient)
    assert out['name'] == [{'use': 'official', 'family': 'Testperson'}]
    assert out['address'] == [{'line': ['1 Synthetic Way'], 'state': 'TX',
                               'postalCode': '00000'}]
    assert 'CANARY' not in json.dumps(out)
