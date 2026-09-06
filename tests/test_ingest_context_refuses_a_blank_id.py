"""$ingest-context validates a caller-supplied resource id the way the
Fasten ingester does (#548, the #286 shape on a second path).

r6/context_builder.py read the id as `resource.get('id', uuid)`, so a
resource carrying `"id": ""` was stored under (tenant, type, ""), and the
next blank-id resource of the same type upserted over it: a write that
succeeded and quietly did the wrong thing. The Fasten path refuses the same
input as `invalid_id` (#546). The two ingest paths now agree: a present id
must be a string or an int matching the FHIR id shape; blank, bool, float,
list or object is refused for the whole bundle, naming the entry index and
never echoing the value; an absent id gets a UUID; an int is stored as str.

MUTATION: r6/context_builder.py, put `resource.get('id', str(uuid.uuid4()))`
back -> the blank-id, bool-id and list-id tests go red (a 201 and a row
with id "").
"""

import json
import re
import uuid

import pytest

from r6.models import R6Resource

PATH = '/r6/fhir/Bundle/$ingest-context'
_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _bundle(*resources):
    return {'resourceType': 'Bundle', 'type': 'collection',
            'entry': [{'resource': r} for r in resources]}


def _post(client, headers, bundle):
    return client.post(PATH, data=json.dumps(bundle),
                       content_type='application/json', headers=headers)


def _stored(app, tenant_id, resource_type, resource_id):
    with app.app_context():
        return R6Resource.query.filter_by(
            tenant_id=tenant_id, resource_type=resource_type,
            id=resource_id).first()


@pytest.mark.parametrize('bad_id', ['', True, 1.5, ['x'], {'v': 'x'},
                                    'has space', 'a' * 256])
def test_a_present_id_outside_the_fhir_shape_refuses_the_bundle(
        client, app, tenant_headers, tenant_id, bad_id):
    resp = _post(client, tenant_headers, _bundle(
        {'resourceType': 'Patient', 'id': bad_id, 'gender': 'female'}))
    assert resp.status_code == 400
    outcome = resp.get_json()
    assert outcome['resourceType'] == 'OperationOutcome'
    diag = outcome['issue'][0]['diagnostics']
    assert 'entry 0' in diag and 'id' in diag
    # The value is never echoed: it is caller-supplied and may be anything.
    if isinstance(bad_id, str) and bad_id:
        assert bad_id not in diag
    assert _stored(app, tenant_id, 'Patient', '') is None


def test_a_blank_id_in_a_later_entry_refuses_the_whole_bundle(
        client, app, tenant_headers, tenant_id):
    """Atomic, as the builder already was for its other validation errors:
    the good entry before the bad one is not stored either."""
    resp = _post(client, tenant_headers, _bundle(
        {'resourceType': 'Patient', 'id': 'ok-patient-548', 'gender': 'male'},
        {'resourceType': 'Observation', 'id': '', 'status': 'final',
         'code': {'coding': [{'system': 'http://loinc.org', 'code': '2339-0'}]}}))
    assert resp.status_code == 400
    assert 'entry 1' in resp.get_json()['issue'][0]['diagnostics']
    assert _stored(app, tenant_id, 'Patient', 'ok-patient-548') is None
    assert _stored(app, tenant_id, 'Observation', '') is None


def test_an_int_id_is_stored_as_its_string(client, app, tenant_headers, tenant_id):
    resp = _post(client, tenant_headers, _bundle(
        {'resourceType': 'Patient', 'id': 7548, 'gender': 'other'}))
    assert resp.status_code == 201
    assert resp.get_json()['items'][0]['resource_ref'] == 'Patient/7548'
    assert _stored(app, tenant_id, 'Patient', '7548') is not None


def test_an_absent_id_gets_a_uuid(client, tenant_headers):
    resp = _post(client, tenant_headers, _bundle(
        {'resourceType': 'Patient', 'gender': 'unknown'}))
    assert resp.status_code == 201
    ref = resp.get_json()['items'][0]['resource_ref']
    assert ref.startswith('Patient/') and _UUID.match(ref.split('/', 1)[1])
    assert uuid.UUID(ref.split('/', 1)[1])


def test_two_blank_ids_no_longer_collapse_into_one_row(
        client, app, tenant_headers, tenant_id):
    """The defect as observed: the second blank-id resource upserted over the
    first. Neither is stored now."""
    for gender in ('female', 'male'):
        resp = _post(client, tenant_headers, _bundle(
            {'resourceType': 'Patient', 'id': '', 'gender': gender}))
        assert resp.status_code == 400
    assert _stored(app, tenant_id, 'Patient', '') is None
