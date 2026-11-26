"""Contact and place inside extensions, and device serials (#1161, PR 2).

The walker matched `address` and `telecom` by key, so the same datatypes
under another key left whole: a street in `valueAddress`, a phone in
`valueContactPoint`, home latitude and longitude in an Address's
geolocation extension, `Location.position`, and a contained Device's
`serialNumber` and UDI carrier. `valueUuid` and `valueExpression` are free
strings that can carry the same. Each canary is placed at the top level, in
an extension nested in an extension, in a contained resource and in a list,
because the rule has to hold at every depth.

Two-sided: `state` and `country` stay on every Address, a ContactPoint keeps
its `system`, and a Device keeps `lotNumber` and its coded type. A redactor
that deleted everything would fail here.
"""

import json

import pytest

from models import db
from r6.models import R6Resource
from r6.redaction import apply_redaction

ADDR_LINE = 'ZQPR2VALUEADDRLINE'
ADDR_CITY = 'ZQPR2VALUEADDRCITY'
ADDR_POSTAL = 'ZQPR2VALUEADDRPOSTAL'
ADDR_TEXT = 'ZQPR2VALUEADDRTEXT'
PHONE = 'ZQPR2VALUECONTACTPOINT'
UUID = 'urn:uuid:5a0e2d3c-0000-4000-8000-zqpr2uuid'
EXPRESSION = 'ZQPR2EXPRESSION'
SERIAL = 'ZQPR2SERIALNUMBER'
DISTINCT = 'ZQPR2DISTINCTIDENTIFIER'
HRF = 'ZQPR2CARRIERHRF'
AIDC = 'ZQPR2CARRIERAIDC'
# Numbers cannot carry a prefix; these are odd enough to be their own token.
LAT, LON = 42.3601987, -71.0589123
POS_LAT, POS_LON = 41.7654321, -70.1234567

STRING_CANARIES = (ADDR_LINE, ADDR_CITY, ADDR_POSTAL, ADDR_TEXT, PHONE, UUID,
                   EXPRESSION, SERIAL, DISTINCT, HRF, AIDC)
NUMBER_CANARIES = (str(LAT), str(LON), str(POS_LAT), str(POS_LON))

GEOLOCATION = {
    'url': 'http://hl7.org/fhir/StructureDefinition/geolocation',
    'extension': [{'url': 'latitude', 'valueDecimal': LAT},
                  {'url': 'longitude', 'valueDecimal': LON}]}


def _address():
    return {'line': [ADDR_LINE], 'city': ADDR_CITY, 'postalCode': ADDR_POSTAL,
            'text': ADDR_TEXT, 'state': 'MA', 'country': 'US',
            'extension': [GEOLOCATION]}


def _payload_extensions():
    """One extension per leaking value type."""
    return [
        {'url': 'http://example.org/birthplace', 'valueAddress': _address()},
        {'url': 'http://example.org/phone',
         'valueContactPoint': {'system': 'phone', 'value': PHONE}},
        {'url': 'http://example.org/uuid', 'valueUuid': UUID},
        {'url': 'http://example.org/expr',
         'valueExpression': {'language': 'text/fhirpath',
                             'expression': EXPRESSION}},
    ]


def _device():
    return {'resourceType': 'Device', 'id': 'dev1',
            'serialNumber': SERIAL, 'distinctIdentifier': DISTINCT,
            'lotNumber': 'LOT-42',
            'udiCarrier': [{'deviceIdentifier': '00844588003288',
                            'carrierHRF': HRF, 'carrierAIDC': AIDC}],
            'type': {'coding': [{'system': 'http://snomed.info/sct',
                                 'code': '706767009'}]}}


def _location():
    return {'resourceType': 'Location', 'id': 'home', 'mode': 'instance',
            'address': _address(),
            'position': {'latitude': POS_LAT, 'longitude': POS_LON}}


def _top_level():
    return {'resourceType': 'Patient', 'id': 'p1', 'gender': 'female',
            'extension': _payload_extensions(),
            'address': [_address()],
            'contact': [{'address': _address()}],
            'contained': [_device(), _location()]}


def _nested_extension():
    return {'resourceType': 'Patient', 'id': 'p1', 'gender': 'female',
            'extension': [{'url': 'http://example.org/outer',
                           'extension': [{'url': 'inner',
                                          'extension': _payload_extensions()}]}]}


def _contained():
    related = {'resourceType': 'RelatedPerson', 'id': 'rp1',
               'extension': _payload_extensions(),
               'address': [_address()]}
    return {'resourceType': 'Observation', 'id': 'o1', 'status': 'final',
            'contained': [related, _device(), _location()]}


def _bundle():
    return {'resourceType': 'Bundle', 'type': 'collection',
            'entry': [{'resource': _top_level()},
                      {'resource': _contained()},
                      {'resource': _location()},
                      {'resource': _device()}]}


SHAPES = {'top-level': _top_level, 'nested-extension': _nested_extension,
          'contained': _contained, 'bundle-entries': _bundle}


def _store(app, tenant_id, *resources):
    """Put records in the store as they are. `$ingest-context` is an
    optional module (r6/extensions.py), and the read and share surfaces
    measured below are core, so they are seeded without it and the core-only
    CI job runs them."""
    with app.app_context():
        for resource in resources:
            db.session.add(R6Resource(
                resource_type=resource['resourceType'],
                resource_json=json.dumps(resource),
                resource_id=resource['id'], tenant_id=tenant_id))
        db.session.commit()


def _walk(obj):
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _addresses(obj):
    for node in _walk(obj):
        if isinstance(node, dict):
            for key in ('address', 'valueAddress'):
                value = node.get(key)
                for a in (value if isinstance(value, list) else [value]):
                    if isinstance(a, dict):
                        yield a


@pytest.mark.parametrize('shape', SHAPES)
def test_no_contact_place_or_serial_canary_survives(shape):
    source = SHAPES[shape]()
    assert any(c in json.dumps(source) for c in STRING_CANARIES)
    out = json.dumps(apply_redaction(source))
    leaked = [c for c in STRING_CANARIES + NUMBER_CANARIES if c in out]
    assert leaked == []


@pytest.mark.parametrize('shape', SHAPES)
def test_every_address_loses_its_extension_and_keeps_state(shape):
    """G1, G2. A number matches only literally, so the key is checked too."""
    addresses = list(_addresses(apply_redaction(SHAPES[shape]())))
    assert addresses
    for a in addresses:
        assert 'extension' not in a, a
        assert set(a) == {'state', 'country'}, a
        assert (a['state'], a['country']) == ('MA', 'US')


@pytest.mark.parametrize('shape', SHAPES)
def test_contact_point_value_is_redacted_and_its_system_kept(shape):
    """T1, as `telecom` does: the kind of contact stays, the number goes."""
    points = [n['valueContactPoint'] for n in _walk(apply_redaction(
        SHAPES[shape]())) if isinstance(n, dict) and 'valueContactPoint' in n]
    assert points
    assert all(p == {'system': 'phone', 'value': '[Redacted]'}
               for p in points), points


def test_a_contact_point_value_of_any_shape_is_replaced():
    """A value that is not a string is still not the original."""
    out = apply_redaction({'resourceType': 'Patient', 'extension': [
        {'url': 'u', 'valueContactPoint': {'value': [PHONE]}}]})
    assert out['extension'][0]['valueContactPoint']['value'] == '[Redacted]'


def test_a_list_of_value_contact_points_is_redacted():
    """Security review of #1165, F-1: a list-valued valueContactPoint is
    accepted on ingest and returned whole on every profile."""
    out = apply_redaction({'resourceType': 'Patient', 'extension': [
        {'url': 'u', 'valueContactPoint': [
            {'system': 'phone', 'value': PHONE}, 'not-a-point']}]})
    assert out['extension'][0]['valueContactPoint'] == [
        {'system': 'phone', 'value': '[Redacted]'}, 'not-a-point']


def test_a_contact_point_value_companion_is_removed():
    """F-2: `_value` can carry the number in its own extensions."""
    out = apply_redaction({'resourceType': 'Patient', 'extension': [
        {'url': 'u', 'valueContactPoint': {
            'system': 'phone',
            '_value': {'extension': [{'url': 'u', 'valueCode': PHONE}]}}}]})
    assert out['extension'][0]['valueContactPoint'] == {'system': 'phone'}


@pytest.mark.parametrize('surface', ['read', 'intake', 'deidentified'])
def test_a_list_valued_contact_point_leaves_no_phone_on_any_surface(
        app, client, tenant_id, auth_headers, surface):
    _store(app, tenant_id, {
        'resourceType': 'Patient', 'id': 'cp-list', 'gender': 'male',
        'extension': [{'url': 'http://example.org/phone',
                       'valueContactPoint': [
                           {'system': 'phone', 'value': PHONE}]}]})
    if surface == 'read':
        resp = client.get('/r6/fhir/Patient/cp-list', headers=auth_headers)
    else:
        resp = client.post('/r6/fhir/$share-bundle', headers=auth_headers,
                           json={'patient_id': 'cp-list', 'profile': surface,
                                 'resource_types': ['Patient']})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'cp-list' in body and 'valueContactPoint' in body
    assert PHONE not in body


def test_a_list_of_value_addresses_gets_the_address_rule():
    out = apply_redaction({'resourceType': 'Patient', 'extension': [
        {'url': 'u', 'valueAddress': [_address(), _address()]}]})
    assert out['extension'][0]['valueAddress'] == [
        {'state': 'MA', 'country': 'US'}] * 2


def test_location_position_is_removed_and_the_location_kept():
    """G3. A Location can be the person's home (`mode: instance`)."""
    out = apply_redaction(_location())
    assert 'position' not in out
    assert (out['id'], out['mode']) == ('home', 'instance')


def test_a_position_key_on_another_type_is_not_touched():
    """G3 is Location's element; the rule is scoped by type."""
    other = {'resourceType': 'Observation', 'id': 'o',
             'component': [{'position': 3}]}
    assert apply_redaction(other)['component'] == [{'position': 3}]


def test_device_serials_go_and_the_lot_and_type_stay():
    """D1. A lot is not one person's, so `lotNumber` stays."""
    out = apply_redaction(_device())
    for key in ('serialNumber', 'distinctIdentifier'):
        assert key not in out
    assert out['udiCarrier'] == [{'deviceIdentifier': '00844588003288'}]
    assert out['lotNumber'] == 'LOT-42'
    assert out['type']['coding'][0]['code'] == '706767009'


def test_immunization_lot_number_survives():
    imm = {'resourceType': 'Immunization', 'id': 'i', 'status': 'completed',
           'lotNumber': 'LOT-7', 'vaccineCode': {'coding': [
               {'system': 'http://hl7.org/fhir/sid/cvx', 'code': '140'}]}}
    assert apply_redaction(imm)['lotNumber'] == 'LOT-7'


def test_uuid_and_expression_values_are_removed():
    """X1. The extension's url stays, so a reader sees one existed."""
    out = apply_redaction(_top_level())
    by_url = {e['url']: e for e in out['extension']}
    assert by_url['http://example.org/uuid'] == {
        'url': 'http://example.org/uuid'}
    assert by_url['http://example.org/expr'] == {
        'url': 'http://example.org/expr'}


def _f1_positions():
    """Rows from the security review of #1162 (#1164 F1): the extension on
    a primitive companion, on identifier[], on contact[] and on a Coverage."""
    def ext():
        return [{'url': 'http://example.org/birthplace',
                 'valueAddress': {'line': [ADDR_LINE], 'city': ADDR_CITY}},
                {'url': 'http://example.org/phone',
                 'valueContactPoint': {'system': 'phone', 'value': PHONE}}]
    patient = {'resourceType': 'Patient', 'id': 'f1-pt', 'gender': 'female',
               'birthDate': '1980-02-03', '_birthDate': {'extension': ext()},
               'identifier': [{'system': 'https://x.example/mrn',
                               'value': 'MRN-1', 'extension': ext()}],
               'contact': [{'extension': ext()}]}
    coverage = {'resourceType': 'Coverage', 'id': 'f1-cov',
                'status': 'active',
                'beneficiary': {'reference': 'Patient/f1-pt'},
                'payor': [{'reference': 'Organization/payer'}],
                'extension': ext()}
    return patient, coverage


@pytest.mark.parametrize('surface', ['read', 'intake', 'deidentified'])
def test_f1_extension_positions_leave_no_address_or_phone(
        app, client, tenant_id, auth_headers, surface):
    """The deidentified share is stamped ANONYED, so it matters most there."""
    _store(app, tenant_id, *_f1_positions())
    if surface == 'read':
        bodies = [client.get(f'/r6/fhir/{p}', headers=auth_headers)
                  for p in ('Patient/f1-pt', 'Coverage/f1-cov')]
    else:
        bodies = [client.post('/r6/fhir/$share-bundle', headers=auth_headers,
                              json={'patient_id': 'f1-pt', 'profile': surface,
                                    'resource_types': ['Patient',
                                                       'Coverage']})]
    for resp in bodies:
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'f1-pt' in body and 'valueContactPoint' in body
        assert [c for c in (ADDR_LINE, ADDR_CITY, PHONE) if c in body] == []


def test_a_patient_read_drops_the_home_geolocation(
        app, client, tenant_id, auth_headers):
    """The measured leak on a real read path. The canary scan keeps its
    geolocation off `Patient.address`, which the intake share ships whole by
    design (#282); this is where that address is measured."""
    _store(app, tenant_id, {
        'resourceType': 'Patient', 'id': 'geo-p1', 'gender': 'female',
        'address': [_address()], 'extension': _payload_extensions()})
    r = client.get('/r6/fhir/Patient/geo-p1', headers=auth_headers)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'geo-p1' in body and '"MA"' in body
    assert [c for c in STRING_CANARIES + NUMBER_CANARIES if c in body] == []
