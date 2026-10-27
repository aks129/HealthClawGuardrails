"""Synthetic records in the shape two real vendor feeds serve (#1058).

Hand-built from the SHAPE of Oracle Health's open sandbox and SMART Health
IT's open sandbox (keys, code systems, which references dangle), with every
value invented. Neither vendor sends `meta.profile`, which is the point: the
check-in and ktc exports used to keep only claimed rows, so both feeds
exported nothing.

What each shape carries that matters here:

- Oracle: Conditions with a problem-list-item category beside a SNOMED
  qualifier, a SNOMED code beside a proprietary one, a `recorder`
  Practitioner and sometimes an `encounter`; most are entered-in-error with
  no clinicalStatus (valid FHIR). Allergies coded in RxNorm or SNOMED, and
  some said only in words. MedicationRequests with RxNorm beside a
  proprietary synonym, intent `plan`. Immunizations with CVX beside NDC,
  `location` and `performer.actor` references. One Coverage whose payor
  Organization the feed does not include.
- SMART (Synthea): Conditions with no category, an `encounter` on every
  record, MedicationRequests with a `requester`.

The CANARY strings stand where a feed puts its own words; none may reach an
export.
"""

from tests import checkin_fixtures as f

PT = f.PT
SNOMED = 'http://snomed.info/sct'
RXNORM = 'http://www.nlm.nih.gov/research/umls/rxnorm'
CVX = 'http://hl7.org/fhir/sid/cvx'
NDC = 'http://hl7.org/fhir/sid/ndc'
IMO = 'https://fhir.example-ehr.com/codeSystem/problem-imo'
SYNONYM = 'https://fhir.example-ehr.com/codeSystem/synonym'
_HL7 = 'http://terminology.hl7.org/CodeSystem/'
HTEST = {'system': _HL7 + 'v3-ActReason', 'code': 'HTEST'}

CANARY = 'CANARY-Vendor-Words-Okafor'


def _meta():
    """The HTEST tag the sandbox puller adds, and no profile claim."""
    return {'tag': [dict(HTEST, display=CANARY)]}


def _cc(*codings):
    return {'coding': [{'system': s, 'code': c, 'display': CANARY}
                       for s, c in codings], 'text': CANARY}


def _status(kind, code):
    return {'coding': [{'system': _HL7 + kind, 'code': code}]}


def _ref(target):
    return {'reference': target, 'display': CANARY}


def patient(pid=PT):
    return {
        'resourceType': 'Patient', 'id': pid, 'meta': _meta(),
        'text': {'status': 'generated', 'div': f'<div>{CANARY}</div>'},
        'identifier': [{'use': 'usual', 'system': 'https://ehr.example/mrn',
                        'value': 'MRN-1058'}],
        'name': [{'use': 'official', 'text': CANARY, 'family': 'Testvendor',
                  'given': ['Kim']}],
        'gender': 'male', 'birthDate': '1970-04-05',
        'contact': [{'extension': [{
            'url': 'https://ehr.example/contact-person',
            'valueReference': _ref('RelatedPerson/rp-1')}]}],
    }


#: Oracle-shaped problem-list Conditions: (id, SNOMED code, live?).
ORACLE_CONDITIONS = (
    ('or-cond-1', '91302008', True), ('or-cond-2', '66344007', True),
    ('or-cond-3', '93444006', False), ('or-cond-4', '90979004', False),
    ('or-cond-5', '48694002', False), ('or-cond-6', '82272006', False),
)


def oracle_condition(cid, code, live, pid=PT):
    res = {
        'resourceType': 'Condition', 'id': cid, 'meta': _meta(),
        'text': {'status': 'generated', 'div': f'<div>{CANARY}</div>'},
        'category': [_cc((_HL7 + 'condition-category', 'problem-list-item')),
                     _cc((SNOMED, '74188005'))],
        'code': _cc((SNOMED, code), (IMO, '15184')),
        'subject': _ref(f'Patient/{pid}'),
        'recorder': _ref('Practitioner/or-pr-1'),
        'recordedDate': '2024-02-03T10:00:00Z',
    }
    if live:
        res['clinicalStatus'] = _status('condition-clinical', 'resolved')
        res['verificationStatus'] = _status('condition-ver-status',
                                            'confirmed')
        res['encounter'] = _ref('Encounter/or-enc-1')
    else:
        res['verificationStatus'] = _status('condition-ver-status',
                                            'entered-in-error')
    return res


#: Oracle-shaped allergies: (id, (system, code) or None for words only).
ORACLE_ALLERGIES = (
    ('or-alg-1', (RXNORM, '161')), ('or-alg-2', (RXNORM, '743')),
    ('or-alg-3', (RXNORM, '2670')), ('or-alg-4', (RXNORM, '219228')),
    ('or-alg-5', (RXNORM, '190521')), ('or-alg-6', (SNOMED, '102263004')),
    ('or-alg-7', (SNOMED, '418920007')), ('or-alg-8', (SNOMED, '762952008')),
    ('or-alg-9', (SNOMED, '373553006')), ('or-alg-10', (SNOMED, '295004007')),
    ('or-alg-11', (SNOMED, '293660003')), ('or-alg-12', (SNOMED, '256259004')),
    ('or-alg-words', None),
)


def oracle_allergy(aid, coding, pid=PT):
    res = {
        'resourceType': 'AllergyIntolerance', 'id': aid, 'meta': _meta(),
        'clinicalStatus': _status('allergyintolerance-clinical', 'active'),
        'verificationStatus': _status('allergyintolerance-verification',
                                      'confirmed'),
        'code': _cc(coding) if coding else {'text': CANARY},
        'patient': _ref(f'Patient/{pid}'),
        'recorder': _ref('Practitioner/or-pr-1'),
        'encounter': _ref('Encounter/or-enc-1'),
        'recordedDate': '2023-05-06T00:00:00Z',
        'note': [{'authorReference': _ref('Practitioner/or-pr-1'),
                  'text': CANARY}],
    }
    return res


def oracle_medication(mid, pid=PT):
    return {
        'resourceType': 'MedicationRequest', 'id': mid, 'meta': _meta(),
        'status': 'active', 'intent': 'plan',
        'category': [_cc((_HL7 + 'medicationrequest-category',
                          'community'))],
        'reportedBoolean': True,
        'medicationCodeableConcept': _cc((RXNORM, '519'),
                                         (SYNONYM, '2748759')),
        'subject': _ref(f'Patient/{pid}'),
        'encounter': _ref('Encounter/or-enc-2'),
        'requester': _ref('Practitioner/or-pr-2'),
        'authoredOn': '2024-01-02T00:00:00Z',
    }


#: Oracle-shaped Immunizations: (id, CVX code).
ORACLE_IMMUNIZATIONS = (
    ('or-imm-1', '93'), ('or-imm-2', '24'), ('or-imm-3', '207'),
    ('or-imm-4', '112'), ('or-imm-5', '230'), ('or-imm-6', '229'),
    ('or-imm-7', '302'),
)


def oracle_immunization(iid, cvx, pid=PT):
    return {
        'resourceType': 'Immunization', 'id': iid, 'meta': _meta(),
        'status': 'completed',
        'vaccineCode': _cc((NDC, '49281-0215-10'), (CVX, cvx)),
        'patient': _ref(f'Patient/{pid}'),
        'encounter': _ref('Encounter/or-enc-3'),
        'occurrenceDateTime': '2022-10-11T00:00:00Z',
        'primarySource': True,
        'location': _ref('Location/or-loc-1'),
        'performer': [{'actor': _ref('Organization/or-org-clinic')},
                      {'function': _cc((_HL7 + 'v2-0443', 'AP')),
                       'actor': _ref('Practitioner/or-pr-1')}],
    }


def oracle_coverage(covid='or-cov-1', pid=PT, payor='Organization/or-payor'):
    return {
        'resourceType': 'Coverage', 'id': covid, 'meta': _meta(),
        'status': 'active',
        'type': _cc(('https://nahdo.org/sopt', '5')),
        'subscriberId': 'SUB-1058',
        'subscriber': _ref(f'Patient/{pid}'),
        'beneficiary': _ref(f'Patient/{pid}'),
        'relationship': _cc((_HL7 + 'subscriber-relationship', 'self')),
        'period': {'start': '2024-01-01'},
        'payor': [_ref(payor)],
    }


def payor(oid='or-payor'):
    return {'resourceType': 'Organization', 'id': oid, 'active': True,
            'name': 'Synthetic Vendor Health Plan'}


def oracle_records(pid=PT):
    """One Oracle-shaped patient: 1 + 6 + 13 + 2 + 7 + 1 = 30 records."""
    return ([patient(pid)]
            + [oracle_condition(c, code, live, pid)
               for c, code, live in ORACLE_CONDITIONS]
            + [oracle_allergy(a, coding, pid)
               for a, coding in ORACLE_ALLERGIES]
            + [oracle_medication('or-med-1', pid),
               oracle_medication('or-med-2', pid)]
            + [oracle_immunization(i, cvx, pid)
               for i, cvx in ORACLE_IMMUNIZATIONS]
            + [oracle_coverage(pid=pid)])


#: SMART-shaped Conditions (no category on any of them).
SMART_CONDITIONS = ('19169002', '156073000', '72892002')
SMART_ALLERGIES = ('232347008', '420174000')
SMART_MEDICATIONS = ('751905', '1367439', '1870230', '748856', '389221',
                     '665078')
SMART_VACCINES = ('140', '114', '43', '113')


def smart_records(pid=PT):
    """One SMART-shaped patient: 1 + 3 + 2 + 6 + 4 = 16 records."""
    out = [{
        'resourceType': 'Patient', 'id': pid, 'meta': _meta(),
        'text': {'status': 'generated', 'div': f'<div>{CANARY}</div>'},
        'identifier': [{'system': 'https://github.com/synthetichealth/synthea',
                        'value': 'synthea-1058'}],
        'name': [{'use': 'official', 'family': 'Testsmart',
                  'given': ['Lee'], 'prefix': ['Ms.']}],
        'gender': 'female', 'birthDate': '1985-07-08',
    }]
    for n, code in enumerate(SMART_CONDITIONS):
        out.append({
            'resourceType': 'Condition', 'id': f'sm-cond-{n}', 'meta': _meta(),
            'clinicalStatus': _status('condition-clinical', 'resolved'),
            'verificationStatus': _status('condition-ver-status', 'confirmed'),
            'code': _cc((SNOMED, code)),
            'subject': _ref(f'Patient/{pid}'),
            'encounter': _ref(f'Encounter/sm-enc-{n}'),
            'onsetDateTime': '2010-01-01T00:00:00Z',
        })
    for n, code in enumerate(SMART_ALLERGIES):
        out.append({
            'resourceType': 'AllergyIntolerance', 'id': f'sm-alg-{n}',
            'meta': _meta(),
            'clinicalStatus': _status('allergyintolerance-clinical', 'active'),
            'verificationStatus': _status('allergyintolerance-verification',
                                          'confirmed'),
            'type': 'allergy', 'category': ['food'],
            'criticality': 'low',
            'code': _cc((SNOMED, code)),
            'patient': _ref(f'Patient/{pid}'),
            'recordedDate': '2001-02-03T00:00:00Z',
        })
    for n, code in enumerate(SMART_MEDICATIONS):
        out.append({
            'resourceType': 'MedicationRequest', 'id': f'sm-med-{n}',
            'meta': _meta(), 'status': 'stopped', 'intent': 'order',
            'medicationCodeableConcept': _cc((RXNORM, code)),
            'subject': _ref(f'Patient/{pid}'),
            'encounter': _ref(f'Encounter/sm-enc-{n}'),
            'requester': _ref('Practitioner/sm-pr-1'),
            'authoredOn': '2015-03-04T00:00:00Z',
        })
    for n, code in enumerate(SMART_VACCINES):
        out.append({
            'resourceType': 'Immunization', 'id': f'sm-imm-{n}',
            'meta': _meta(), 'status': 'completed',
            'vaccineCode': _cc((CVX, code)),
            'patient': _ref(f'Patient/{pid}'),
            'encounter': _ref(f'Encounter/sm-enc-{n}'),
            'occurrenceDateTime': '2016-05-06T00:00:00Z',
            'primarySource': True,
        })
    return out
