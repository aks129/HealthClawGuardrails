"""A name in an extension's valueHumanName (#1164, F1).

Rows from the security review of #1162. An extension whose value is a
HumanName carries a third party's full name: on the Patient itself, on a
primitive companion (`_birthDate`), on `contact[]` and on a Coverage. The
walker matched `name` by key, so `valueHumanName` left whole on every read.

The standard read now cuts it to initials, the rule `name` gets today, until
names go entirely (#1161 PR 1, an owner ruling). The deidentified share is
stamped ANONYED, so it drops valueHumanName whole. The address and phone
rows of F1 are in tests/test_redaction_nested_contact_place.py (#1165).
Synthetic data only.
"""

import json

import pytest

from r6.redaction import apply_patient_controlled_redaction, apply_redaction

PATIENT_ID = 'sec1162-pt'

HN_FAMILY = 'ZQSECHNFAMILY'
HN_GIVEN = 'ZQSECHNGIVEN'
PRIM_FAMILY = 'ZQSECPRIMFAMILY'
CONTACT_EXT_FAMILY = 'ZQSECCONTACTEXTFAMILY'
COV_EXT_FAMILY = 'ZQSECCOVEXTFAMILY'
NESTED_FAMILY = 'ZQSECNESTEDFAMILY'

LEAK_CANARIES = (HN_FAMILY, HN_GIVEN, PRIM_FAMILY, CONTACT_EXT_FAMILY,
                 COV_EXT_FAMILY, NESTED_FAMILY)


def _patient():
    return {
        'resourceType': 'Patient', 'id': PATIENT_ID,
        'name': [{'family': 'Testperson', 'given': ['Ana']}],
        'birthDate': '1980-02-03',
        'extension': [
            {'url': 'https://x.example/emergency-guardian',
             'valueHumanName': {'family': HN_FAMILY, 'given': [HN_GIVEN],
                                'text': HN_FAMILY + ' ' + HN_GIVEN}},
            {'url': 'https://x.example/outer', 'extension': [
                {'url': 'inner',
                 'valueHumanName': {'family': NESTED_FAMILY}}]},
        ],
        '_birthDate': {'extension': [{
            'url': 'https://x.example/recorded-by',
            'valueHumanName': {'family': PRIM_FAMILY}}]},
        'contact': [{
            'name': {'family': 'Contactperson'},
            'extension': [{'url': 'https://x.example/contact-alias',
                           'valueHumanName': {
                               'family': CONTACT_EXT_FAMILY}}]}],
    }


def _coverage():
    return {
        'resourceType': 'Coverage', 'id': 'sec1162-cov', 'status': 'active',
        'beneficiary': {'reference': f'Patient/{PATIENT_ID}'},
        'payor': [{'reference': 'Organization/payer'}],
        'extension': [{'url': 'https://x.example/subscriber-name',
                       'valueHumanName': {'family': COV_EXT_FAMILY}}],
    }


@pytest.fixture
def seeded(client, auth_headers):
    resp = client.post('/r6/fhir/Bundle/$ingest-context',
                       headers=auth_headers,
                       json={'resourceType': 'Bundle', 'type': 'collection',
                             'entry': [{'resource': _patient()},
                                       {'resource': _coverage()}]})
    body = resp.get_json()
    assert resp.status_code == 201, resp.get_data(as_text=True)[:300]
    assert body['resource_count'] == 2 and not body.get('skipped_count')


def _share(client, headers, profile):
    resp = client.post('/r6/fhir/$share-bundle', headers=headers,
                       json={'patient_id': PATIENT_ID, 'profile': profile,
                             'resource_types': ['Patient', 'Coverage']})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    raw = resp.get_data(as_text=True)
    ids = {e['resource']['id'] for e in json.loads(raw)['entry']}
    # Reached data, or every absence below is vacuous.
    assert ids == {PATIENT_ID, 'sec1162-cov'}, ids
    return raw


@pytest.mark.parametrize('profile', ['intake', 'deidentified'])
def test_share_bundle_drops_names_in_extensions(
        client, auth_headers, seeded, profile):
    raw = _share(client, auth_headers, profile)
    leaked = [c for c in LEAK_CANARIES if c in raw]
    assert leaked == [], 'leaked: ' + ', '.join(leaked)


def test_the_deidentified_share_carries_no_human_name_at_all(
        client, auth_headers, seeded):
    """Initials would pass the canary check above; ANONYED means none.
    The extension itself stays, so a reader sees one existed."""
    raw = _share(client, auth_headers, 'deidentified')
    assert 'valueHumanName' not in raw
    assert 'https://x.example/emergency-guardian' in raw


def test_standard_read_drops_names_in_extensions(
        client, auth_headers, seeded):
    leaked = []
    for path in (f'Patient/{PATIENT_ID}', 'Coverage/sec1162-cov'):
        resp = client.get(f'/r6/fhir/{path}', headers=auth_headers)
        assert resp.status_code == 200, path
        raw = resp.get_data(as_text=True)
        assert path.split('/')[1] in raw
        leaked += [c for c in LEAK_CANARIES if c in raw]
    assert leaked == [], 'leaked: ' + ', '.join(leaked)


def test_standard_read_cuts_a_value_human_name_to_initials():
    """The rule `name` gets today, so the shape stays readable."""
    out = apply_redaction(_patient())
    assert out['extension'][0]['valueHumanName'] == {
        'family': 'Z.', 'given': ['Z.']}
    assert out['extension'][1]['extension'][0]['valueHumanName'] == {
        'family': 'Z.'}


def test_a_list_of_value_human_names_is_cut_too():
    out = apply_redaction({'resourceType': 'Patient', 'extension': [
        {'url': 'u', 'valueHumanName': [{'family': HN_FAMILY}]}]})
    assert out['extension'][0]['valueHumanName'] == [{'family': 'Z.'}]


def test_patient_controlled_redaction_drops_it_at_any_depth():
    out = json.dumps(apply_patient_controlled_redaction(
        {**_patient(), 'contained': [_coverage()]}, PATIENT_ID))
    assert 'valueHumanName' not in out
