"""Security tester exploit tests for PR #976 (ktc export, SHL PR 1).

V1/V2/V3/V6 attacks on the new `$share-bundle` profile=ktc exit. Synthetic
data only; every CANARY string stands where a real feed puts a name.
"""

import json

import pytest

from r6.actions.confirmations import ACTION_APPROVAL_AUDIENCE
from r6.models import AuditEventRecord
from r6.stepup import generate_step_up_token
from tests import checkin_fixtures as f

ALL = ['AllergyIntolerance', 'Condition', 'Coverage', 'Immunization',
       'MedicationRequest', 'Patient']
C = 'CANARY-Nested-Okafor-Delphine'
SNOMED = 'http://snomed.info/sct'


@pytest.fixture(autouse=True)
def _mint_secret(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', f.MINT_SECRET)
    monkeypatch.setenv('KTC_EXPORT_ENABLED', 'true')


def _mint(client, types, tenant=f.TENANT):
    resp = client.post(
        '/r6/fhir/internal/step-up-token',
        json={'tenant_id': tenant, 'purpose': 'ktc', 'resource_types': types},
        headers={'X-Tenant-Id': tenant, 'X-Internal-Secret': f.MINT_SECRET})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()['token']


def _export(client, tok, types, **kw):
    return f.export(client, tok, types, profile='ktc', **kw)


def _ext():
    return [{'url': 'https://ehr.example/x', 'valueString': C,
             'extension': [{'url': 'y', 'valueString': C}]}]


def _hostile_records():
    pt = f.patient()
    pt['contact'] = [{'name': {'text': C, 'family': C},
                      'telecom': [{'system': 'phone', 'value': C}],
                      'relationship': [{'text': C}]}]
    pt['generalPractitioner'] = [{'reference': 'Practitioner/p1',
                                  'display': C}]
    pt['maritalStatus'] = {'text': C}
    pt['communication'] = [{'language': {'text': C}}]
    pt['modifierExtension'] = _ext()
    pt['link'] = [{'other': {'reference': f'Patient/{f.SIBLING}',
                             'display': C}, 'type': 'seealso'}]
    pt['patientName'] = C  # non-FHIR (FHI-66)

    cond = f.condition()
    cond['extension'] = _ext()
    cond['modifierExtension'] = _ext()
    cond['category'] = [{'text': C}]
    cond['severity'] = {'coding': [{'system': SNOMED, 'code': '0000000',
                                    'display': C}], 'text': C}
    cond['evidence'] = [{'detail': [{'reference': 'Observation/o1',
                                     'display': C}],
                         'code': [{'text': C}]}]
    cond['stage'] = [{'summary': {'text': C}}]
    cond['recorder'] = {'reference': 'Practitioner/p1', 'display': C}

    allergy = f.nka_allergy()
    allergy['reaction'] = [{
        'substance': {'coding': [{'system': SNOMED, 'code': '256349002',
                                  'display': C}], 'text': C},
        'manifestation': [{'coding': [{'system': SNOMED,
                                       'code': '271807003',
                                       'display': C}], 'text': C},
                          {'text': C}],
        'description': C, 'note': [{'text': C}],
        'extension': _ext()}]
    allergy['extension'] = _ext()

    mr = {
        'resourceType': 'MedicationRequest', 'id': 'sec-mr-1',
        'meta': f.claim('us-core-medicationrequest'),
        'status': 'active', 'intent': 'order',
        'medicationCodeableConcept': {'text': C},
        'subject': {'reference': f'Patient/{f.PT}'},
        'requester': {'reference': 'Practitioner/p1', 'display': C},
        'reasonCode': [{'text': C}],
        'dosageInstruction': [{'text': C, 'patientInstruction': C,
                               'additionalInstruction': [{'text': C}],
                               'route': {'text': C}}],
        'extension': _ext(),
    }
    imm = {
        'resourceType': 'Immunization', 'id': 'sec-imm-1',
        'meta': f.claim('us-core-immunization'),
        'status': 'completed', 'occurrenceDateTime': '2025-10-01',
        'vaccineCode': {'coding': [{'system': 'http://hl7.org/fhir/sid/cvx',
                                    'code': '150', 'display': C}],
                        'text': C},
        'patient': {'reference': f'Patient/{f.PT}', 'display': C},
        'performer': [{'actor': {'reference': 'Practitioner/p1',
                                 'display': C},
                       'function': {'text': C}}],
        'reasonCode': [{'text': C}],
        'site': {'text': C},
        'extension': _ext(),
    }
    cov = f.coverage()
    cov['extension'] = _ext()
    cov['class'].append({'type': {'coding': [{
        'system': f.COVERAGE_CLASS, 'code': 'plan', 'display': C}],
        'text': C}, 'value': 'PLAN-1', 'name': C})
    cov['type'] = {'coding': [{'system': 'http://terminology.hl7.org/'
                               'CodeSystem/v3-ActCode', 'code': 'HIP',
                               'display': C}], 'text': C}
    cov['relationship'] = {'coding': [{
        'system': 'http://terminology.hl7.org/CodeSystem/subscriber-'
                  'relationship', 'code': 'self', 'display': C}], 'text': C}
    org = f.organization()
    org['alias'] = [C]
    org['extension'] = _ext()
    org['contact'] = [{'name': {'text': C}}]
    return [pt, cond, allergy, mr, imm, cov, org]


def _full(client):
    resp = _export(client, _mint(client, ALL), ALL)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp, resp.get_data(as_text=True)


# --- V1: no upstream text, at any depth (FHIR fields) ----------------------

def test_no_nested_upstream_display_or_text_leaves(app, client):
    recs = _hostile_records()
    recs[0].pop('patientName')  # FHI-66, pinned separately below
    f.seed(app, *recs)
    _, text = _full(client)
    hits = []

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f'{path}.{k}')
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f'{path}[{i}]')
        elif isinstance(o, str) and C in o:
            hits.append(path)
    walk(json.loads(text), '$')
    assert hits == [], hits


def test_exactly_one_patient_even_with_a_linked_sibling(app, client):
    recs = _hostile_records()
    f.seed(app, *recs, f.patient(f.SIBLING, given='Sib', family='Ling'))
    resp, text = _full(client)
    pts = f.resources(resp, 'Patient')
    assert [p['id'] for p in pts] == [f.PT]
    assert 'Ling' not in text
    for r in f.resources(resp):
        assert 'profile' not in (r.get('meta') or {})


@pytest.mark.xfail(strict=True, reason='FHI-66: non-FHIR fields survive '
                   'every redaction profile, ktc included')
def test_fhi66_a_non_fhir_patientname_does_not_leave(app, client):
    f.seed(app, *_hostile_records())
    _, text = _full(client)
    assert C not in text


# --- V6 / grant -----------------------------------------------------------

@pytest.mark.parametrize('tok_kw', [
    dict(),                                       # cached general token
    dict(scope='write'),
    dict(scope='read'),
    dict(audience='curatr', operation='ktc:Patient'),
    dict(audience=ACTION_APPROVAL_AUDIENCE, operation='ktc:Patient'),
    dict(scope='read', audience='ktc-export', operation='ktc:*'),
    dict(scope='read', audience='ktc-export', operation='ktc:Patient,'),
    dict(scope='read', audience='checkin-export', operation='ktc:Patient'),
])
def test_foreign_tokens_cannot_export(app, client, tok_kw):
    f.seed(app, f.patient())
    tok = generate_step_up_token(f.TENANT, **tok_kw)
    resp = _export(client, tok, ['Patient'])
    assert resp.status_code == 401
    assert 'entry' not in (resp.get_json() or {})


def test_a_ktc_token_cannot_read_through_the_general_fhir_paths(
        app, client, monkeypatch):
    """Production enforces read auth (runtime_config)."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    f.seed(app, *_hostile_records())
    tok = _mint(client, ALL)
    h = {'X-Tenant-Id': f.TENANT, 'X-Step-Up-Token': tok,
         'Authorization': f'Bearer {tok}'}
    for path in (f'/r6/fhir/Patient/{f.PT}', '/r6/fhir/Patient',
                 '/r6/fhir/Condition', f'/r6/fhir/Patient/{f.PT}/$everything',
                 '/r6/fhir/AuditEvent'):
        resp = client.get(path, headers=h)
        assert C not in resp.get_data(as_text=True), path
        assert f.CANARY_DISPLAY not in resp.get_data(as_text=True), path
        assert resp.status_code in (401, 403, 404, 405), (path,
                                                          resp.status_code)


def test_a_ktc_token_for_tenant_a_cannot_export_tenant_b(app, client):
    f.seed(app, f.patient(), tenant=f.OTHER_TENANT)
    tok = _mint(client, ['Patient'], tenant=f.TENANT)
    resp = _export(client, tok, ['Patient'], tenant=f.OTHER_TENANT)
    assert resp.status_code == 401


def test_replay_after_a_successful_export_is_refused(app, client):
    f.seed(app, f.patient())
    tok = _mint(client, ['Patient'])
    assert _export(client, tok, ['Patient']).status_code == 200
    again = _export(client, tok, ['Patient'])
    assert again.status_code == 401
    assert 'Testperson' not in again.get_data(as_text=True)


def test_a_coverage_payor_cannot_pull_a_patient_record(app, client):
    """A payor reference naming a Patient id pulls only Organizations."""
    cov = f.coverage(payor=f'Organization/{f.SIBLING}')
    f.seed(app, f.patient(), cov,
           f.patient(f.SIBLING, given='Sib', family='Ling'))
    resp = _export(client, _mint(client, ['Coverage', 'Patient']),
                   ['Coverage', 'Patient'])
    assert resp.status_code == 200
    assert 'Ling' not in resp.get_data(as_text=True)


# --- V2: audit ------------------------------------------------------------

def _rows(app):
    with app.app_context():
        return [(a.outcome, a.detail) for a in AuditEventRecord.query.filter_by(
            tenant_id=f.TENANT).all()]


@pytest.mark.parametrize('tok', [None, 'garbage.token', 'checkin', 'replay'])
def test_every_grant_refusal_writes_a_phi_free_failure_row(app, client, tok):
    f.seed(app, f.patient())
    if tok == 'checkin':
        tok = f.token(client, ['Patient'])
    elif tok == 'replay':
        tok = _mint(client, ['Patient'])
        assert _export(client, tok, ['Patient']).status_code == 200
    before = len(_rows(app))
    resp = _export(client, tok, ['Patient'])
    assert resp.status_code == 401
    new = _rows(app)[before:]
    assert new and all(o != 'success' for o, _ in new), new
    joined = ''.join(d or '' for _, d in new)
    assert f.PT not in joined and 'Testperson' not in joined


def test_an_audit_write_failure_releases_nothing(app, client, monkeypatch):
    f.seed(app, *_hostile_records())
    tok = _mint(client, ALL)

    def boom(*a, **k):
        raise RuntimeError('simulated audit insert failure')
    monkeypatch.setattr('r6.audit._new_audit_event', boom)
    app.config['PROPAGATE_EXCEPTIONS'] = False
    resp = _export(client, tok, ALL)
    assert resp.status_code >= 500
    body = resp.get_data(as_text=True)
    assert 'Testperson' not in body and 'entry' not in body
