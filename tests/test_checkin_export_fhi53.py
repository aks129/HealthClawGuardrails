"""The `checkin` export profile for SMART Health Check-in (FHI-53).

A patient ticks items in the wallet; CareAgents mints a token for exactly
those resource types and asks $share-bundle for one patient's records. What
leaves is the intake profile, narrowed: only the ticked types, only rows that
carry an allow-listed US Core/C4DIC claim, Coverage member and group numbers
restored by allow-list, and the payor named from its own Organization record.

The exploit cases (replay, subset/superset, foreign tokens, canaries) are in
tests/test_sec_checkin_exploits.py. Each test names the mutation that turns
it red.
"""

import time

import pytest

from r6 import checkin
from r6.us_core_profiles import CANONICALS
from tests import checkin_fixtures as f


@pytest.fixture(autouse=True)
def _mint_secret(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', f.MINT_SECRET)


# --- closed tables ------------------------------------------------------------

def test_the_six_types_and_their_claims_line_up():
    """Organization is never a check-in type: that would export every one."""
    assert checkin.CHECKIN_TYPES == {
        'Patient', 'Condition', 'AllergyIntolerance', 'MedicationRequest',
        'Immunization', 'Coverage'}
    assert set(checkin.CLAIMS_BY_TYPE) == checkin.CHECKIN_TYPES
    for claims in checkin.CLAIMS_BY_TYPE.values():
        assert claims <= set(CANONICALS)


def test_operation_is_the_sorted_distinct_set():
    assert checkin.operation(['Patient', 'Coverage', 'Patient']) == \
        'checkin:Coverage,Patient'


# --- mint -----------------------------------------------------------------

def test_the_mint_issues_a_read_scoped_audience_bound_token(client):
    """MUTATION: mint scope=None -> red here, and the write-refusal tests
    in the exploit file go red with it."""
    resp = f.mint(client, ['Patient', 'Condition'])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['scope'] == 'read'
    assert body['expires_in'] == 120
    claims = f.claims_of(body['token'])
    assert claims['scope'] == 'read'
    assert claims['aud'] == 'checkin-export'
    assert claims['op'] == 'checkin:Condition,Patient'
    assert 115 <= claims['exp'] - time.time() <= 121


@pytest.mark.parametrize('types', [
    ['Organization'], ['Patient', 'RelatedPerson'], [], 'Patient', None,
    ['patient'], [['Patient']],
])
def test_the_mint_refuses_a_type_outside_the_six(client, types):
    assert f.mint(client, types).status_code == 400


@pytest.mark.parametrize('extra', [
    {'ttl': 3600}, {'ttl_seconds': 3600}, {'aud': 'x'}, {'audience': 'x'},
    {'op': 'checkin:Coverage'}, {'operation': 'x'}, {'scope': 'write'},
])
def test_the_mint_refuses_a_caller_chosen_claim(client, extra):
    assert f.mint(client, ['Patient'], **extra).status_code == 400


def test_the_mint_refuses_another_purpose(client):
    resp = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': f.TENANT, 'purpose': 'export',
                             'resource_types': ['Patient']},
                       headers={'X-Tenant-Id': f.TENANT,
                                'X-Internal-Secret': f.MINT_SECRET})
    assert resp.status_code == 400


def test_the_mint_is_still_behind_the_internal_secret(client):
    resp = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': f.TENANT, 'purpose': 'checkin',
                             'resource_types': ['Patient']},
                       headers={'X-Tenant-Id': f.TENANT})
    assert resp.status_code == 403


# --- export ---------------------------------------------------------------

def test_a_patient_and_a_profiled_condition_go_out(app, client):
    f.seed(app, f.patient(), f.condition())
    resp = f.export(client, f.token(client, ['Patient', 'Condition']),
                    ['Patient', 'Condition'])
    assert resp.status_code == 200
    assert resp.mimetype == 'application/fhir+json'
    pt, = f.resources(resp, 'Patient')
    assert pt['name'] == [{'family': 'Testperson', 'given': ['Ana']}]
    assert pt['birthDate'] == '1980-02-03'
    cond, = f.resources(resp, 'Condition')
    assert cond['code']['coding'][0]['code'] == '44054006'
    assert cond['subject'] == {'reference': f'Patient/{f.PT}'}
    assert 'note' not in cond


def test_a_row_without_a_claim_is_not_exported(app, client):
    """MUTATION: drop the `has_claim` check in _share_bundle_checkin -> red."""
    f.seed(app, f.patient(), f.condition('ci-cond-bare', profiled=False))
    resp = f.export(client, f.token(client, ['Patient', 'Condition']),
                    ['Patient', 'Condition'])
    assert f.resources(resp, 'Condition') == []


def test_a_claim_for_another_type_does_not_count(app, client):
    cond = f.condition('ci-cond-wrong-claim')
    cond['meta'] = f.claim('us-core-patient')
    f.seed(app, f.patient(), cond)
    resp = f.export(client, f.token(client, ['Patient', 'Condition']),
                    ['Patient', 'Condition'])
    assert f.resources(resp, 'Condition') == []


def test_only_the_ticked_types_go_out(app, client):
    f.seed(app, f.patient(), f.condition(), f.coverage(), f.organization())
    resp = f.export(client, f.token(client, ['Patient']), ['Patient'])
    assert {r['resourceType'] for r in f.resources(resp)} == {'Patient'}
    assert f.MEMBER_ID not in resp.get_data(as_text=True)
    assert f.GROUP_ID not in resp.get_data(as_text=True)


def test_patient_id_is_required(app, client):
    f.seed(app, f.patient())
    resp = f.export(client, f.token(client, ['Patient']), ['Patient'],
                    patient_id=None)
    assert resp.status_code == 400
    assert 'entry' not in resp.get_json()


def test_a_family_tenant_exports_only_the_named_patient(app, client):
    """MUTATION: skip the patient filter for Patient rows -> red."""
    f.seed(app, f.patient(), f.patient(f.SIBLING, given='Bea'),
           f.condition(), f.condition('ci-cond-sib', pid=f.SIBLING))
    resp = f.export(client, f.token(client, ['Patient', 'Condition']),
                    ['Patient', 'Condition'])
    assert [r['id'] for r in f.resources(resp, 'Patient')] == [f.PT]
    assert [r['id'] for r in f.resources(resp, 'Condition')] == ['ci-cond-1']


# --- Coverage and the payor -----------------------------------------------

def test_coverage_restores_member_group_and_payor_name(app, client):
    f.seed(app, f.patient(), f.coverage(), f.organization())
    resp = f.export(client, f.token(client, ['Patient', 'Coverage']),
                    ['Patient', 'Coverage'])
    cov, = f.resources(resp, 'Coverage')
    assert cov['subscriberId'] == f.MEMBER_ID
    assert cov['identifier'] == [{
        'system': 'https://payer.example/member', 'value': f.MEMBER_ID,
        'type': {'coding': [{'system': f.V2_0203, 'code': 'MB'}]}}]
    assert cov['class'] == [{
        'type': {'coding': [{'system': f.COVERAGE_CLASS, 'code': 'group'}]},
        'value': f.GROUP_ID}]
    assert cov['payor'] == [{'reference': 'Organization/ci-org-1'}]
    assert cov['beneficiary'] == {'reference': f'Patient/{f.PT}'}
    assert cov['subscriber'] == {'reference': f'Patient/{f.PT}'}
    org, = f.resources(resp, 'Organization')
    assert org['name'] == f.PAYOR_NAME
    assert org['identifier'] == [{'system': f.NAIC, 'value': f.NAIC_ID}]
    assert 'telecom' not in org and 'address' not in org


def test_a_c4dic_coverage_claim_counts(app, client):
    cov = f.coverage()
    cov['meta'] = {'profile': [
        'http://hl7.org/fhir/us/insurance-card/StructureDefinition/'
        'C4DIC-Coverage|1.1.0']}
    f.seed(app, f.patient(), cov, f.organization())
    resp = f.export(client, f.token(client, ['Coverage']), ['Coverage'])
    assert len(f.resources(resp, 'Coverage')) == 1


# --- absence --------------------------------------------------------------

def test_no_allergy_records_export_no_allergy_rows(app, client):
    """Absence is never fabricated: no "no known allergies" is synthesised."""
    f.seed(app, f.patient())
    resp = f.export(client,
                    f.token(client, ['Patient', 'AllergyIntolerance']),
                    ['Patient', 'AllergyIntolerance'])
    assert resp.status_code == 200
    assert f.resources(resp, 'AllergyIntolerance') == []


def test_a_stored_nka_record_goes_out_dated_without_display(app, client):
    f.seed(app, f.patient(), f.nka_allergy())
    resp = f.export(client, f.token(client, ['AllergyIntolerance']),
                    ['AllergyIntolerance'])
    nka, = f.resources(resp, 'AllergyIntolerance')
    assert nka['recordedDate'] == '2026-01-05'
    assert nka['code']['coding'][0]['code'] == '716186003'
    assert f.CANARY_DISPLAY not in resp.get_data(as_text=True)


# --- size and audit ---------------------------------------------------------

def test_an_oversized_bundle_is_refused_whole(app, client, monkeypatch):
    """MUTATION: drop the size check -> red. Truncation would be worse."""
    monkeypatch.setattr(checkin, 'MAX_BUNDLE_BYTES', 200)
    f.seed(app, f.patient(), f.condition())
    resp = f.export(client, f.token(client, ['Patient', 'Condition']),
                    ['Patient', 'Condition'])
    assert resp.status_code == 413
    assert resp.get_json() == {'error': 'checkin_too_large'}


def test_the_real_cap_is_400000_bytes():
    assert checkin.MAX_BUNDLE_BYTES == 400_000


def test_one_export_writes_one_fixed_word_audit_row(app, client):
    f.seed(app, f.patient(), f.condition(), f.coverage(), f.organization())
    resp = f.export(client,
                    f.token(client, ['Patient', 'Condition', 'Coverage']),
                    ['Patient', 'Condition', 'Coverage'])
    assert resp.status_code == 200
    assert f.export_audits(app) == [(
        'careagents-checkin',
        'share-bundle export (profile=checkin): 4 resources across 4 '
        'type(s); types=Condition,Coverage,Patient')]
