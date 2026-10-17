"""Synthetic records and helpers shared by the FHI-53 check-in export tests.

Every value is invented. The CANARY strings stand where a real feed puts a
name or a number, so a test can assert that they never reach the bundle or
the audit row.
"""

import base64
import json

from models import db
from r6.models import AuditEventRecord, R6Resource

TENANT = 'checkin-test-tenant'      # private: not in PUBLIC_TENANTS
OTHER_TENANT = 'checkin-other-tenant'
MINT_SECRET = 'checkin-test-mint-secret'
PT = 'ci-pt-1'
SIBLING = 'ci-pt-2'

US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/'
V2_0203 = 'http://terminology.hl7.org/CodeSystem/v2-0203'
COVERAGE_CLASS = 'http://terminology.hl7.org/CodeSystem/coverage-class'
NAIC = 'urn:oid:2.16.840.1.113883.6.300'

MEMBER_ID = 'MEMBER-7731'
GROUP_ID = 'GROUP-0042'
PAYOR_NAME = 'Synthetic Mutual Health Plan'
NAIC_ID = '99999'

# Never allowed out.
CANARY_DISPLAY = 'CANARY-Rivera-Marisol'
CANARY_CLASS_NAME = 'CANARY-Employer-Group-Name'
CANARY_SSN = 'CANARY-999-12-3456'
CANARY_TYPE_TEXT = 'CANARY-Member-of-Marisol'
CANARY_NOTE = 'CANARY-free-text-note'


def claim(name, version='7.0.0'):
    return {'profile': [f'{US_CORE}{name}|{version}']}


def patient(pid=PT, given='Ana', family='Testperson'):
    return {
        'resourceType': 'Patient', 'id': pid,
        'meta': claim('us-core-patient'),
        'name': [{'family': family, 'given': [given]}],
        'birthDate': '1980-02-03',
        'telecom': [{'system': 'phone', 'value': '555-0100'}],
        'address': [{'line': ['1 Synthetic Way'], 'city': 'Testville',
                     'state': 'TX'}],
        'identifier': [
            {'system': 'http://hl7.org/fhir/sid/us-ssn', 'value': '999001111'},
            {'system': 'https://ehr.example/odd-ssn-system',
             'type': {'coding': [{'system': V2_0203, 'code': 'SS'}]},
             'value': CANARY_SSN},
            {'system': 'https://ehr.example/mrn', 'value': 'MRN-1'},
        ],
        'text': {'status': 'generated', 'div': f'<div>{CANARY_NOTE}</div>'},
    }


def condition(cid='ci-cond-1', pid=PT, profiled=True):
    res = {
        'resourceType': 'Condition', 'id': cid,
        'code': {'coding': [{'system': 'http://snomed.info/sct',
                             'code': '44054006',
                             'display': CANARY_DISPLAY}],
                 'text': CANARY_DISPLAY},
        'subject': {'reference': f'Patient/{pid}', 'display': CANARY_DISPLAY},
        'note': [{'text': CANARY_NOTE}],
    }
    if profiled:
        res['meta'] = claim('us-core-condition-problems-health-concerns')
    return res


def nka_allergy(aid='ci-nka-1', pid=PT):
    """A stored, coded "no known allergy" record (SNOMED 716186003)."""
    return {
        'resourceType': 'AllergyIntolerance', 'id': aid,
        'meta': claim('us-core-allergyintolerance'),
        'recordedDate': '2026-01-05',
        'code': {'coding': [{'system': 'http://snomed.info/sct',
                             'code': '716186003',
                             'display': CANARY_DISPLAY}]},
        'patient': {'reference': f'Patient/{pid}'},
    }


def organization(oid='ci-org-1', name=PAYOR_NAME):
    return {
        'resourceType': 'Organization', 'id': oid,
        'name': name,
        'identifier': [{'system': NAIC, 'value': NAIC_ID},
                       {'system': 'http://hl7.org/fhir/sid/us-npi',
                        'value': '1234567890'}],
        'telecom': [{'system': 'phone', 'value': '555-0199'}],
        'address': [{'line': ['9 Payer Plaza'], 'city': 'Plantown'}],
    }


def coverage(covid='ci-cov-1', pid=PT, payor='Organization/ci-org-1',
             subscriber=None):
    return {
        'resourceType': 'Coverage', 'id': covid,
        'meta': claim('us-core-coverage'),
        'status': 'active',
        'subscriberId': MEMBER_ID,
        'identifier': [
            {'system': 'https://payer.example/member',
             'type': {'coding': [{'system': V2_0203, 'code': 'MB',
                                  'display': CANARY_DISPLAY}],
                      'text': CANARY_TYPE_TEXT},
             'value': MEMBER_ID},
            {'system': 'https://payer.example/other',
             'type': {'coding': [{'system': V2_0203, 'code': 'MB'},
                                 {'system': V2_0203, 'code': 'SS'}]},
             'value': CANARY_SSN},
        ],
        'class': [
            {'type': {'coding': [{'system': COVERAGE_CLASS,
                                  'code': 'group'}]},
             'value': GROUP_ID, 'name': CANARY_CLASS_NAME},
            {'type': {'coding': [{'system': COVERAGE_CLASS,
                                  'code': 'rxbin'}]},
             'value': 'RXBIN-1'},
        ],
        'beneficiary': {'reference': f'Patient/{pid}'},
        'subscriber': subscriber or {'reference': f'Patient/{pid}',
                                     'display': CANARY_DISPLAY},
        'policyHolder': {'reference': 'RelatedPerson/ci-rp-1',
                         'display': CANARY_DISPLAY},
        'payor': [{'reference': payor, 'display': CANARY_DISPLAY},
                  {'reference': 'RelatedPerson/ci-rp-1'}],
    }


def seed(app, *resources, tenant=TENANT):
    with app.app_context():
        for res in resources:
            db.session.add(R6Resource(
                resource_type=res['resourceType'],
                resource_json=json.dumps(res), resource_id=res['id'],
                tenant_id=tenant))
        db.session.commit()


def mint(client, types, tenant=TENANT, **extra):
    body = {'tenant_id': tenant, 'purpose': 'checkin',
            'resource_types': types}
    body.update(extra)
    return client.post('/r6/fhir/internal/step-up-token', json=body,
                       headers={'X-Tenant-Id': tenant,
                                'X-Internal-Secret': MINT_SECRET})


def token(client, types, tenant=TENANT):
    resp = mint(client, types, tenant=tenant)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()['token']


def claims_of(tok):
    return json.loads(base64.urlsafe_b64decode(tok.rsplit('.', 1)[0]))


def export(client, tok, types, patient_id=PT, tenant=TENANT, headers=None,
           **extra):
    body = {'profile': 'checkin', 'resource_types': types}
    if patient_id is not None:
        body['patient_id'] = patient_id
    body.update(extra)
    h = {'X-Tenant-Id': tenant}
    if tok:
        h['X-Step-Up-Token'] = tok
    h.update(headers or {})
    return client.post('/r6/fhir/$share-bundle', json=body, headers=h)


def resources(resp, rtype=None):
    out = [e['resource'] for e in resp.get_json()['entry']]
    return [r for r in out if rtype is None or r['resourceType'] == rtype]


def export_audits(app, tenant=TENANT):
    with app.app_context():
        return [(a.agent_id, a.detail) for a in AuditEventRecord.query.filter_by(
            tenant_id=tenant, resource_id='share-bundle').all()]
