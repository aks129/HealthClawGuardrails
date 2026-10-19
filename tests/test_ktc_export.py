"""The `ktc` export profile and its grant (SHL design PR 1).

Kill the Clipboard: a SMART Health Link carries a patient's chosen records
to a clinic's EHR. This is the engine half of PR 1: its own scoped,
audience-bound, single-use token, and a `$share-bundle` profile that reuses
the check-in's row walk, redaction, labels and PHI-free audit, omits
`meta.profile`, keeps the HTEST marker and carries the one Patient.

Records and helpers are the check-in's (tests/checkin_fixtures.py). Each
test names the mutation that turns it red.
"""

import json
import time

import pytest

from r6 import ktc
from r6.models import AuditEventRecord
from r6.stepup import generate_step_up_token
from tests import checkin_fixtures as f

ALL = ['AllergyIntolerance', 'Condition', 'Coverage', 'Immunization',
       'MedicationRequest', 'Patient']
V3_ACT_REASON = 'http://terminology.hl7.org/CodeSystem/v3-ActReason'
HTEST = {'system': V3_ACT_REASON, 'code': 'HTEST',
         'display': 'test health data'}


@pytest.fixture(autouse=True)
def _mint_secret(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', f.MINT_SECRET)
    # The export is off unless the engine says otherwise (the flag tests
    # at the end of this file); everything else here runs with it on.
    monkeypatch.setenv('KTC_EXPORT_ENABLED', 'true')


def mint(client, types, tenant=f.TENANT, **extra):
    body = {'tenant_id': tenant, 'purpose': 'ktc', 'resource_types': types}
    body.update(extra)
    return client.post('/r6/fhir/internal/step-up-token', json=body,
                       headers={'X-Tenant-Id': tenant,
                                'X-Internal-Secret': f.MINT_SECRET})


def token(client, types, tenant=f.TENANT):
    resp = mint(client, types, tenant=tenant)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()['token']


def export(client, tok, types, **kw):
    return f.export(client, tok, types, profile='ktc', **kw)


def _tagged(res):
    res.setdefault('meta', {})['tag'] = [
        {'system': V3_ACT_REASON, 'code': 'HTEST', 'display': f.CANARY_DISPLAY}]
    return res


def _medication_request(pid=f.PT):
    return {
        'resourceType': 'MedicationRequest', 'id': 'ci-mr-1',
        'meta': f.claim('us-core-medicationrequest'),
        'status': 'active', 'intent': 'order',
        'medicationCodeableConcept': {'coding': [{
            'system': 'http://www.nlm.nih.gov/research/umls/rxnorm',
            'code': '314076', 'display': f.CANARY_DISPLAY}],
            'text': f.CANARY_DISPLAY},
        'subject': {'reference': f'Patient/{pid}', 'display': f.CANARY_DISPLAY},
        'note': [{'text': f.CANARY_NOTE}],
    }


def _immunization(pid=f.PT):
    return {
        'resourceType': 'Immunization', 'id': 'ci-imm-1',
        'meta': f.claim('us-core-immunization'),
        'status': 'completed', 'occurrenceDateTime': '2025-10-01',
        'vaccineCode': {'coding': [{'system': 'http://hl7.org/fhir/sid/cvx',
                                    'code': '150',
                                    'display': f.CANARY_DISPLAY}]},
        'patient': {'reference': f'Patient/{pid}'},
    }


def _all_records():
    return [f.patient(), f.condition(), f.nka_allergy(), _medication_request(),
            _immunization(), f.coverage(), f.organization()]


@pytest.fixture
def records(app):
    f.seed(app, *_all_records())


# --- closed tables ------------------------------------------------------------

def test_the_type_set_is_the_check_in_six():
    """Owner ruling 2026-10-16: the six, plus the payor by reference."""
    assert ktc.KTC_TYPES == {
        'Patient', 'Condition', 'AllergyIntolerance', 'MedicationRequest',
        'Immunization', 'Coverage'}
    assert 'Organization' not in ktc.KTC_TYPES


def test_the_operation_is_its_own_and_sorted():
    assert ktc.operation(['Patient', 'Coverage', 'Patient']) == \
        'ktc:Coverage,Patient'


# --- mint -----------------------------------------------------------------

def test_the_mint_issues_a_read_scoped_ktc_bound_token(client):
    """MUTATION: mint with audience='checkin-export' -> red."""
    resp = mint(client, ['Patient', 'Condition'])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['scope'] == 'read' and body['expires_in'] == 120
    claims = f.claims_of(body['token'])
    assert claims['scope'] == 'read'
    assert claims['aud'] == 'ktc-export'
    assert claims['op'] == 'ktc:Condition,Patient'
    assert 115 <= claims['exp'] - time.time() <= 121


@pytest.mark.parametrize('types', [
    ['Organization'], ['Patient', 'Observation'], [], 'Patient', None,
    # Patient is always in the set: the bundle must carry it.
    ['Condition'], ['Coverage', 'Immunization'],
])
def test_the_mint_refuses_a_set_outside_the_six_or_without_patient(
        client, types):
    assert mint(client, types).status_code == 400


@pytest.mark.parametrize('extra', [
    {'ttl': 3600}, {'ttl_seconds': 3600}, {'aud': 'x'}, {'audience': 'x'},
    {'op': 'ktc:Patient'}, {'operation': 'x'}, {'scope': 'write'},
])
def test_the_mint_refuses_a_caller_chosen_claim(client, extra):
    assert mint(client, ['Patient'], **extra).status_code == 400


def test_the_mint_needs_the_internal_secret(client):
    resp = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': f.TENANT, 'purpose': 'ktc',
                             'resource_types': ['Patient']},
                       headers={'X-Tenant-Id': f.TENANT,
                                'X-Internal-Secret': 'wrong'})
    assert resp.status_code == 403


def test_the_check_in_mint_is_unchanged(client):
    claims = f.claims_of(f.token(client, ['Patient']))
    assert claims['aud'] == 'checkin-export'
    assert claims['op'] == 'checkin:Patient'


# --- grant ------------------------------------------------------------------

def test_a_ktc_token_exports_once(client, records):
    """MUTATION: drop consume_nonce=True -> red."""
    tok = token(client, ['Patient'])
    assert export(client, tok, ['Patient']).status_code == 200
    second = export(client, tok, ['Patient'])
    assert second.status_code == 401
    assert 'entry' not in second.get_json()


def test_a_check_in_token_cannot_export_ktc(client, records):
    """MUTATION: grant on checkin.AUDIENCE -> red."""
    tok = f.token(client, ['Patient'])
    assert export(client, tok, ['Patient']).status_code == 401


def test_a_ktc_token_cannot_export_a_check_in(client, records):
    tok = token(client, ['Patient'])
    assert f.export(client, tok, ['Patient']).status_code == 401


def test_a_ktc_token_cannot_export_intake(client, records):
    tok = token(client, ALL)
    resp = client.post('/r6/fhir/$share-bundle',
                       json={'profile': 'intake', 'patient_id': f.PT},
                       headers={'X-Tenant-Id': f.TENANT,
                                'X-Step-Up-Token': tok})
    assert resp.status_code == 401


def test_a_ktc_token_cannot_write(client, records):
    tok = token(client, ALL)
    resp = client.post('/r6/fhir/Patient',
                       json={'resourceType': 'Patient', 'id': 'ci-new'},
                       headers={'X-Tenant-Id': f.TENANT,
                                'X-Step-Up-Token': tok})
    assert resp.status_code == 401


def test_the_general_token_cannot_export_ktc(client, records):
    """The cached CareAgents token: no scope claim, no `aud`."""
    tok = generate_step_up_token(f.TENANT)
    assert export(client, tok, ['Patient']).status_code == 401


def test_a_token_with_the_op_but_no_audience_is_refused(client, records):
    tok = generate_step_up_token(f.TENANT, scope='read',
                                 operation='ktc:Patient')
    assert export(client, tok, ['Patient']).status_code == 401


def test_a_check_in_spelt_op_under_the_ktc_audience_is_refused(
        client, records):
    tok = generate_step_up_token(f.TENANT, scope='read',
                                 audience='ktc-export',
                                 operation='checkin:Patient')
    assert export(client, tok, ['Patient']).status_code == 401


def test_no_token_is_refused(client, records):
    assert export(client, None, ['Patient']).status_code == 401


def test_a_token_for_another_tenant_is_refused(client, records):
    tok = token(client, ['Patient'], tenant=f.OTHER_TENANT)
    assert export(client, tok, ['Patient']).status_code == 401


def test_an_expired_ktc_token_is_refused(client, records):
    tok = generate_step_up_token(
        f.TENANT, ttl_seconds=-1, scope='read', audience='ktc-export',
        operation='ktc:Patient')
    assert export(client, tok, ['Patient']).status_code == 401


@pytest.mark.parametrize('minted,asked', [
    (['Patient'], ['Patient', 'Coverage']),
    (['Patient', 'Coverage'], ['Patient']),
])
def test_the_type_set_must_match_exactly(client, records, minted, asked):
    assert export(client, token(client, minted), asked).status_code == 401


@pytest.mark.parametrize('types,patient_id', [
    (['Organization'], f.PT), ([], f.PT), ('Patient', f.PT), (None, f.PT),
    (['Condition'], f.PT), (['Patient'], None), (['Patient'], '../x'),
    (['Patient'], ['ci-pt-1']),
])
def test_a_bad_request_is_refused_before_the_token_is_spent(
        client, records, types, patient_id):
    """#963 F4 ordering. MUTATION: move the request check below the first
    require_grant and spend there -> red."""
    tok = token(client, ['Patient'])
    assert export(client, tok, types, patient_id=patient_id).status_code \
        == 400
    assert export(client, tok, ['Patient']).status_code == 200


def test_a_missing_patient_is_refused_and_does_not_spend_the_token(
        app, client):
    """The spec requires the Patient. MUTATION: drop the Patient check ->
    red (a bundle without one goes out, and the token is spent)."""
    f.seed(app, f.patient(f.SIBLING), f.condition())
    tok = token(client, ['Condition', 'Patient'])
    resp = export(client, tok, ['Condition', 'Patient'])
    assert resp.status_code == 422
    assert 'entry' not in resp.get_json()
    assert 'ci-cond-1' not in resp.get_data(as_text=True)
    f.seed(app, f.patient())
    assert export(client, tok, ['Condition', 'Patient']).status_code == 200


def test_an_unclaimed_patient_counts_as_missing(app, client):
    pt = f.patient()
    pt.pop('meta')
    f.seed(app, pt, f.condition())
    tok = token(client, ['Condition', 'Patient'])
    assert export(client, tok, ['Condition', 'Patient']).status_code == 422


def test_an_oversized_bundle_is_refused_whole_and_keeps_the_token(
        app, client, records, monkeypatch):
    """MUTATION: consume the nonce before the size check -> red."""
    tok = token(client, ['Patient'])
    monkeypatch.setattr(ktc, 'MAX_BUNDLE_BYTES', 50)
    resp = export(client, tok, ['Patient'])
    assert resp.status_code == 413
    assert resp.get_json() == {'error': 'ktc_too_large'}
    monkeypatch.setattr(ktc, 'MAX_BUNDLE_BYTES', 2_000_000)
    assert export(client, tok, ['Patient']).status_code == 200


def test_the_ceiling_is_its_own():
    """Larger than check-in's 400 KB: the PDF rides inside as base64."""
    assert ktc.MAX_BUNDLE_BYTES == 2_000_000


# --- what leaves ------------------------------------------------------------

def _full(client):
    resp = export(client, token(client, ALL), ALL)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.mimetype == 'application/fhir+json'
    return resp, resp.get_data(as_text=True)


def test_the_bundle_is_a_collection_of_the_six_and_the_payor(client, records):
    resp, _ = _full(client)
    bundle = resp.get_json()
    assert bundle['type'] == 'collection' and bundle['timestamp']
    assert sorted(r['resourceType'] for r in f.resources(resp)) == sorted(
        ALL + ['Organization'])
    for entry in bundle['entry']:
        rtype, rid = entry['resource']['resourceType'], entry['resource']['id']
        assert entry['fullUrl'].endswith(f'/r6/fhir/{rtype}/{rid}')


def test_exactly_one_patient_and_it_is_the_named_one(app, client, records):
    f.seed(app, f.patient(f.SIBLING, given='Sib'),
           f.condition('ci-cond-sib', pid=f.SIBLING))
    resp, text = _full(client)
    pts = f.resources(resp, 'Patient')
    assert [p['id'] for p in pts] == [f.PT]
    assert f.SIBLING not in text


def test_no_meta_profile_on_any_entry(client, records):
    """KTC: senders SHOULD NOT claim profiles.

    MUTATION: drop meta.pop('profile') in _ktc_profile -> red."""
    resp, text = _full(client)
    assert 'StructureDefinition' not in text
    for res in f.resources(resp):
        assert 'profile' not in res.get('meta', {}), res['resourceType']


def test_rows_without_a_claim_still_stay_home(app, client, records):
    """The claim gate runs on the stored row, before the strip.

    MUTATION: gate on the profiled resource instead -> red (nothing but
    the payor would go out)."""
    f.seed(app, f.condition('ci-cond-unclaimed', profiled=False))
    resp, text = _full(client)
    assert 'ci-cond-unclaimed' not in text
    assert [c['id'] for c in f.resources(resp, 'Condition')] == ['ci-cond-1']


def test_the_htest_marker_survives_on_every_type(app, client):
    """The design: verify on every type, not only Organization."""
    f.seed(app, *[_tagged(r) for r in _all_records()])
    resp, text = _full(client)
    assert f.CANARY_DISPLAY not in text
    out = f.resources(resp)
    assert len(out) == len(ALL) + 1
    for res in out:
        assert res['meta']['tag'] == [HTEST], res['resourceType']


@pytest.mark.parametrize('canary', [
    f.CANARY_DISPLAY, f.CANARY_CLASS_NAME, f.CANARY_TYPE_TEXT, f.CANARY_NOTE,
    f.CANARY_SSN])
def test_upstream_text_never_leaves(client, records, canary):
    """The check-in redaction, reused: no upstream display/text, note,
    narrative or SSN-class identifier."""
    _, text = _full(client)
    assert canary not in text


def test_labels_come_from_our_table(client, records):
    resp, _ = _full(client)
    mr, = f.resources(resp, 'MedicationRequest')
    concept = mr['medicationCodeableConcept']
    assert 'display' not in concept['coding'][0]
    from r6 import terminology
    assert concept.get('text') == terminology.lookup(
        'http://www.nlm.nih.gov/research/umls/rxnorm', '314076')


def test_an_empty_allergy_set_is_shared_as_empty(app, client):
    """Never infer "no known allergies" from an absence.

    MUTATION: synthesise an NKA AllergyIntolerance when none is found ->
    red."""
    f.seed(app, f.patient(), f.condition())
    resp, text = _full(client)
    assert f.resources(resp, 'AllergyIntolerance') == []
    assert 'known allerg' not in text.lower()
    assert '716186003' not in text


def test_a_stored_nka_record_goes_out_by_code(client, records):
    """An explicit record is exported by code, dated, as the check-in
    does; no wording of ours turns it into a bare "No known allergy"."""
    resp, _ = _full(client)
    allergy, = f.resources(resp, 'AllergyIntolerance')
    assert allergy['code']['coding'][0]['code'] == '716186003'
    assert allergy['recordedDate'] == '2026-01-05'
    out = json.dumps(allergy)
    assert f.CANARY_DISPLAY not in out
    assert 'no known allerg' not in out.lower()


# --- audit ------------------------------------------------------------------

def test_one_audit_event_from_fixed_words(app, client, records):
    """MUTATION: interpolate patient_id into the detail -> red."""
    _full(client)
    assert f.export_audits(app) == [(
        'careagents-ktc',
        'share-bundle export (profile=ktc): 7 resources across 7 type(s); '
        'types=AllergyIntolerance,Condition,Coverage,Immunization,'
        'MedicationRequest,Patient')]


def test_refusals_are_audited_as_failures_from_fixed_words(
        app, client, monkeypatch):
    f.seed(app, f.patient(), f.condition())
    tok = token(client, ['Patient'])
    export(client, tok, ['Patient'], patient_id=None, purpose='CANARY-x')
    monkeypatch.setattr(ktc, 'MAX_BUNDLE_BYTES', 50)
    export(client, tok, ['Patient'])
    monkeypatch.setattr(ktc, 'MAX_BUNDLE_BYTES', 2_000_000)
    export(client, token(client, ['Patient']), ['Patient'],
           patient_id='ci-pt-absent')
    with app.app_context():
        rows = [(a.outcome, a.agent_id, a.detail)
                for a in AuditEventRecord.query.filter_by(
                    tenant_id=f.TENANT, resource_id='share-bundle').all()]
    assert [r[0] for r in rows] == ['failure'] * 3
    assert {r[1] for r in rows} == {'careagents-ktc'}
    details = [r[2] for r in rows]
    assert details == [
        'share-bundle export (profile=ktc) refused: '
        'resource_types or patient_id missing or malformed',
        'share-bundle export (profile=ktc) refused: over the size limit; '
        'types=Patient',
        'share-bundle export (profile=ktc) refused: no exportable Patient']
    assert 'CANARY' not in ''.join(details)
    assert 'ci-pt' not in ''.join(details)


# --- the flag: plaintext never leaves until the link store lands -------------

def test_the_export_flag_is_off_by_default(monkeypatch):
    """MUTATION: default KTC_EXPORT_ENABLED to on -> red."""
    monkeypatch.delenv('KTC_EXPORT_ENABLED')
    assert ktc.export_enabled() is False
    for value in ('', '0', 'false', 'no', 'off', 'enabled'):
        monkeypatch.setenv('KTC_EXPORT_ENABLED', value)
        assert ktc.export_enabled() is False, value
    for value in ('1', 'true', 'yes', ' TRUE '):
        monkeypatch.setenv('KTC_EXPORT_ENABLED', value)
        assert ktc.export_enabled() is True, value


@pytest.mark.parametrize('value', [None, '', '0', 'false'])
def test_off_the_plaintext_bundle_is_refused_and_the_token_kept(
        app, client, records, monkeypatch, value):
    """Design section 1: plaintext never leaves the engine. Until PR 3's
    link store, the export answers only where the engine turned it on.

    MUTATION: drop the export_enabled check -> red."""
    tok = token(client, ALL)
    if value is None:
        monkeypatch.delenv('KTC_EXPORT_ENABLED')
    else:
        monkeypatch.setenv('KTC_EXPORT_ENABLED', value)
    resp = export(client, tok, ALL)
    assert resp.status_code == 404
    assert resp.get_json() == {'error': 'ktc_export_disabled'}
    text = resp.get_data(as_text=True)
    assert 'Testperson' not in text and 'entry' not in text
    assert f.export_audits(app) == [(
        'careagents-ktc',
        'share-bundle export (profile=ktc) refused: export disabled')]
    monkeypatch.setenv('KTC_EXPORT_ENABLED', 'true')
    assert export(client, tok, ALL).status_code == 200


def test_off_it_answers_the_same_to_any_caller(app, client, monkeypatch):
    """Checked first: no token and a malformed request read the same as a
    good one, so the flag is no oracle for anything behind it."""
    monkeypatch.delenv('KTC_EXPORT_ENABLED')
    for resp in (export(client, None, ALL),
                 export(client, None, ['Organization'], patient_id=None)):
        assert resp.status_code == 404
        assert resp.get_json() == {'error': 'ktc_export_disabled'}


def test_off_the_check_in_export_is_unaffected(app, client, records,
                                               monkeypatch):
    monkeypatch.delenv('KTC_EXPORT_ENABLED')
    resp = f.export(client, f.token(client, ['Patient']), ['Patient'])
    assert resp.status_code == 200
