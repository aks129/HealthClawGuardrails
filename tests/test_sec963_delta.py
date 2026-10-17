"""Delta security review of #963, 95c425c..4213ac5 (Patient identifiers,
OMB race/ethnicity rebuild). Synthetic data only.

test_D1_* fail at 4213ac5: an SSN labelled only by a local system name or
by type.text leaves with its value, now that Patient identifier values go
out under `checkin`.
"""

import pytest

from r6.stepup import clear_nonce_cache
from tests import checkin_fixtures as f

CDC = 'urn:oid:2.16.840.1.113883.6.238'
RACE = 'http://hl7.org/fhir/us/core/StructureDefinition/us-core-race'
ETH = 'http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity'


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', f.MINT_SECRET)
    clear_nonce_cache()


def _export_patient(app, client, pt):
    f.seed(app, pt)
    resp = f.export(client, f.token(client, ['Patient']), ['Patient'])
    assert resp.status_code == 200
    return resp, resp.get_data(as_text=True)


@pytest.mark.parametrize('ident', [
    {'system': 'https://ehr.example/ssn', 'value': '999-12-3456'},
    {'system': 'https://ehr.example/ids/7',
     'type': {'text': 'Social Security Number'}, 'value': '999-12-3456'},
    {'system': 'https://ehr.example/ids/8', 'value': '999123456',
     'type': {'coding': [{'system': 'https://ehr.example/cs',
                          'code': 'SSN'}]}},
], ids=['local-ssn-system', 'type-text-only', 'local-code-SSN'])
def test_D1_an_ssn_labelled_only_locally_never_leaves(app, client, ident):
    pt = f.patient()
    pt['identifier'] = [ident]
    _, text = _export_patient(app, client, pt)
    assert '999-12-3456' not in text and '999123456' not in text


def test_identifier_carries_no_upstream_text(app, client):
    pt = f.patient()
    pt['identifier'] = [{
        'use': 'usual', 'system': 'https://ehr.example/mrn', 'value': 'MRN-1',
        'type': {'coding': [{'system': 'http://terminology.hl7.org/'
                             'CodeSystem/v2-0203', 'code': 'MR',
                             'display': 'CANARY-IdDisplay'}],
                 'text': 'CANARY-IdText'},
        'assigner': {'reference': 'Organization/x',
                     'display': 'CANARY-Assigner'},
        'period': {'start': '2020', 'extension': [
            {'url': 'u', 'valueString': 'CANARY-IdExt'}]},
        'extension': [{'url': 'u', 'valueString': 'CANARY-IdExt2'}]}]
    resp, text = _export_patient(app, client, pt)
    assert 'CANARY' not in text
    out, = f.resources(resp, 'Patient')
    assert out['identifier'] == [{
        'use': 'usual', 'system': 'https://ehr.example/mrn',
        'type': {'coding': [{'system': 'http://terminology.hl7.org/'
                             'CodeSystem/v2-0203', 'code': 'MR'}]},
        'value': 'MRN-1'}]


def test_omb_rebuild_keeps_no_upstream_text(app, client):
    pt = f.patient()
    pt['identifier'] = []
    pt['extension'] = [
        {'url': RACE, 'extension': [
            {'url': 'ombCategory', 'valueCoding': {
                'system': CDC, 'code': '2106-3', 'display': 'CANARY-RaceDisp'}},
            {'url': 'ombCategory', 'valueCoding': {
                'system': CDC, 'code': 'CANARY-unmapped'}},
            {'url': 'detailed', 'valueCoding': {
                'system': CDC, 'code': '2108-9', 'display': 'CANARY-Detail'}},
            {'url': 'text', 'valueString': 'CANARY-RaceText'},
            {'url': 'ombCategory', 'extension': [
                {'url': 'u', 'valueString': 'CANARY-Nested'}],
             'valueCoding': {'system': CDC, 'code': '2028-9',
                             'extension': [{'url': 'u',
                                            'valueString': 'CANARY-CodExt'}]}},
        ]},
        {'url': ETH, 'extension': [
            {'url': 'text', 'valueString': 'CANARY-EthText'}]},
        {'url': 'https://ehr.example/birthplace',
         'valueString': 'CANARY-OtherExt'},
    ]
    resp, text = _export_patient(app, client, pt)
    assert 'CANARY' not in text
    out, = f.resources(resp, 'Patient')
    assert out['extension'] == [{'url': RACE, 'extension': [
        {'url': 'ombCategory', 'valueCoding': {
            'system': CDC, 'code': '2106-3'}},
        {'url': 'ombCategory', 'valueCoding': {
            'system': CDC, 'code': '2028-9'}},
        {'url': 'text', 'valueString': 'White, Asian'}]}]


def test_full_url_ignores_a_hostile_host_for_path_and_id(app, client):
    """The Host header picks the origin only; type and id stay ours."""
    f.seed(app, f.patient())
    resp = f.export(client, f.token(client, ['Patient']), ['Patient'],
                    headers={'Host': 'evil.example'})
    assert resp.status_code == 200
    url = resp.get_json()['entry'][0]['fullUrl']
    assert url == 'http://evil.example/r6/fhir/Patient/ci-pt-1'
