"""Final security check of #963 at 2757e0b. Synthetic data only.

test_D2_* fail at 2757e0b: SSN shapes the value check does not read (a
space or dot separator, an `SSN:` label inside the value) leave on Patient
identifiers, Coverage.subscriberId and Coverage class.value.
"""

import pytest

from r6.access import _checkin_profile

V2 = 'http://terminology.hl7.org/CodeSystem/v2-0203'
CLS = 'http://terminology.hl7.org/CodeSystem/coverage-class'


@pytest.mark.parametrize('value', [
    '999 12 3456', '999.12.3456', 'SSN:999-12-3456', 'ssn 999121234'])
def test_D2_a_patient_identifier_with_an_ssn_never_leaves(value):
    pt = {'resourceType': 'Patient', 'id': 'p', 'identifier': [
        {'system': 'https://ehr.example/ids', 'value': value}]}
    assert 'identifier' not in _checkin_profile(pt, patient_id='p')


@pytest.mark.parametrize('value', ['999 12 3456', '999.12.3456'])
def test_D2_a_subscriber_id_with_an_ssn_shape_never_leaves(value):
    cov = {'resourceType': 'Coverage', 'id': 'v', 'subscriberId': value,
           'beneficiary': {'reference': 'Patient/p'}}
    assert 'subscriberId' not in _checkin_profile(cov, patient_id='p')


def test_D2_a_class_value_with_an_ssn_shape_never_leaves():
    cov = {'resourceType': 'Coverage', 'id': 'v',
           'beneficiary': {'reference': 'Patient/p'},
           'class': [{'type': {'coding': [{'system': CLS, 'code': 'group'}]},
                      'value': '999-12-3456'}]}
    assert '999-12-3456' not in str(_checkin_profile(cov, patient_id='p'))


def test_no_upstream_text_survives_the_relabel():
    cond = {'resourceType': 'Condition', 'id': 'c',
            'subject': {'reference': 'Patient/p'},
            'code': {'text': 'CANARY-a'},
            'category': [{'coding': [{'system': 'http://x', 'code': 'zz',
                                      'display': 'CANARY-b'}],
                          'text': 'CANARY-c'}],
            'evidence': [{'code': [{'text': 'CANARY-d'}],
                          'detail': [{'reference': 'Observation/o',
                                      'display': 'CANARY-e'}]}],
            'extension': [{'url': 'u', 'valueCodeableConcept': {
                'text': 'CANARY-f'}}]}
    assert 'CANARY' not in str(_checkin_profile(cond, patient_id='p'))
