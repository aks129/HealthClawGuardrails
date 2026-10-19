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
    """MUTATION: drop the `has_claim` check in r6/checkin.py export -> red."""
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
        'type': {'coding': [{'system': f.V2_0203, 'code': 'MB'}],
                 'text': 'Member Number'}}]
    assert cov['class'] == [{
        'type': {'coding': [{'system': f.COVERAGE_CLASS, 'code': 'group'}],
                 'text': 'Group'},
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


# --- QA #963: what the wallet needs ---------------------------------------

def _refs(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == 'reference' and isinstance(value, str):
                yield value
            else:
                yield from _refs(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _refs(value)


def test_every_entry_has_a_full_url_on_our_host(app, client):
    """The wallet (careagents/static/checkin_answer.js prepareRecords, #960)
    keeps only entries with a fullUrl; without one all six items read
    "unavailable" (QA #963).

    MUTATION: append entries without fullUrl again -> red."""
    f.seed(app, f.patient(), f.condition(), f.coverage(), f.organization())
    types = ['Patient', 'Condition', 'Coverage']
    resp = f.export(client, f.token(client, types), types)
    entries = resp.get_json()['entry']
    assert len(entries) == 4
    for entry in entries:
        res = entry['resource']
        assert entry['fullUrl'] == (
            f"http://localhost/r6/fhir/{res['resourceType']}/{res['id']}")


def test_references_between_entries_resolve_as_the_wallet_resolves_them(
        app, client):
    """prepareRecords maps a literal `Type/id` reference to the entry whose
    fullUrl names it. Coverage.payor -> Organization, beneficiary and
    subject -> Patient must all land on an entry."""
    f.seed(app, f.patient(), f.condition(), f.coverage(), f.organization())
    types = ['Patient', 'Condition', 'Coverage']
    resp = f.export(client, f.token(client, types), types)
    entries = resp.get_json()['entry']
    url_of = {f"{e['resource']['resourceType']}/{e['resource']['id']}":
              e['fullUrl'] for e in entries}
    refs = [r for e in entries for r in _refs(e['resource'])]
    assert 'Organization/ci-org-1' in refs and f'Patient/{f.PT}' in refs
    for ref in refs:
        assert ref in url_of, f'{ref} resolves to no entry'


@pytest.mark.parametrize('bad_id', ['..', '.', 'ci cond', 'ci/cond'])
def test_an_id_that_could_walk_the_url_is_not_sent(app, client, bad_id):
    """MUTATION: drop the `rid in ('.', '..')` check in full_url -> red for
    the dot ids."""
    f.seed(app, f.patient(), f.condition(bad_id))
    resp = f.export(client, f.token(client, ['Condition']), ['Condition'])
    assert resp.status_code == 200
    assert f.resources(resp, 'Condition') == []


# --- QA #963: refusals ----------------------------------------------------

def _outcomes(app):
    from r6.models import AuditEventRecord
    with app.app_context():
        return [(a.outcome, a.detail) for a in AuditEventRecord.query.filter_by(
            tenant_id=f.TENANT, resource_id='share-bundle').all()]


def test_a_400_is_audited_as_a_failure(app, client):
    """MUTATION: outcome='failure' -> 'success' on the 400 path -> red."""
    f.seed(app, f.patient())
    f.export(client, f.token(client, ['Patient']), ['Patient'],
             patient_id=None)
    (outcome, _), = _outcomes(app)
    assert outcome == 'failure'


def test_a_413_is_audited_as_a_failure_and_does_not_spend_the_token(
        app, client, monkeypatch):
    """MUTATION: drop outcome='failure' on the 413 path -> red.
    MUTATION: consume the nonce before the size check -> red (the retry
    answers 401)."""
    f.seed(app, f.patient(), f.condition())
    tok = f.token(client, ['Patient'])
    monkeypatch.setattr(checkin, 'MAX_BUNDLE_BYTES', 50)
    assert f.export(client, tok, ['Patient']).status_code == 413
    (outcome, detail), = _outcomes(app)
    assert outcome == 'failure' and 'over the size limit' in detail
    monkeypatch.setattr(checkin, 'MAX_BUNDLE_BYTES', 400_000)
    assert f.export(client, tok, ['Patient']).status_code == 200
    assert f.export(client, tok, ['Patient']).status_code == 401


# --- owner ruling 2026-10-14: a US Core Patient ---------------------------

RACE = 'http://hl7.org/fhir/us/core/StructureDefinition/us-core-race'
ETHNICITY = 'http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity'
CDC = 'urn:oid:2.16.840.1.113883.6.238'


def _patient_with(app, client, **fields):
    pt = f.patient()
    pt.update(fields)
    f.seed(app, pt)
    resp = f.export(client, f.token(client, ['Patient']), ['Patient'])
    out, = f.resources(resp, 'Patient')
    return out, resp.get_data(as_text=True)


def test_patient_identifier_values_go_out_but_never_an_ssn(app, client):
    """MUTATION: stop restoring identifier values -> red.
    MUTATION: drop the SSN check in _patient_identifiers -> red."""
    out, text = _patient_with(app, client, identifier=[
        {'system': 'https://ehr.example/mrn', 'value': 'MRN-1',
         'type': {'coding': [{'system': f.V2_0203, 'code': 'MR',
                              'display': f.CANARY_DISPLAY}],
                  'text': f.CANARY_DISPLAY},
         'assigner': {'display': f.CANARY_DISPLAY}},
        {'system': 'http://hl7.org/fhir/sid/us-ssn', 'value': '999001111'},
        {'system': 'https://ehr.example/x', 'value': f.CANARY_SSN,
         'type': {'coding': [{'system': 'http://hl7.org/fhir/v2/0203',
                              'code': 'SS'}]}},
        {'system': 'https://ehr.example/stub'},
    ])
    assert out['identifier'] == [{
        'system': 'https://ehr.example/mrn',
        'type': {'coding': [{'system': f.V2_0203, 'code': 'MR'}]},
        'value': 'MRN-1'}]
    for leaked in ('999001111', f.CANARY_SSN, f.CANARY_DISPLAY):
        assert leaked not in text


def test_race_and_ethnicity_carry_our_labels_not_upstream_text(app, client):
    """MUTATION: copy the upstream `text` sub-extension -> red."""
    out, text = _patient_with(app, client, extension=[
        {'url': RACE, 'extension': [
            {'url': 'ombCategory', 'valueCoding': {
                'system': CDC, 'code': '2106-3', 'display': f.CANARY_DISPLAY}},
            {'url': 'detailed', 'valueCoding': {
                'system': CDC, 'code': '2108-9'}},
            {'url': 'text', 'valueString': 'CANARY-race-text'}]},
        {'url': ETHNICITY, 'extension': [
            {'url': 'ombCategory', 'valueCoding': {
                'system': CDC, 'code': '2186-5'}},
            {'url': 'text', 'valueString': 'CANARY-ethnicity-text'}]},
        {'url': 'https://ehr.example/birthplace',
         'valueString': 'CANARY-birthplace'},
    ])
    assert out['extension'] == [
        {'url': RACE, 'extension': [
            {'url': 'ombCategory', 'valueCoding': {
                'system': CDC, 'code': '2106-3'}},
            {'url': 'text', 'valueString': 'White'}]},
        {'url': ETHNICITY, 'extension': [
            {'url': 'ombCategory', 'valueCoding': {
                'system': CDC, 'code': '2186-5'}},
            {'url': 'text', 'valueString': 'Not Hispanic or Latino'}]},
    ]
    assert 'CANARY' not in text


def test_an_unmapped_category_takes_the_whole_extension_out(app, client):
    """`text` is 1..1, and we never borrow it from upstream: with no code we
    can label, the extension goes rather than leave invalid.

    MUTATION: emit the extension with no `text` when nothing maps -> red."""
    out, text = _patient_with(app, client, extension=[
        {'url': RACE, 'extension': [
            {'url': 'ombCategory', 'valueCoding': {
                'system': CDC, 'code': '9999-9'}},
            {'url': 'text', 'valueString': 'CANARY-race-text'}]}])
    assert 'extension' not in out
    assert 'CANARY' not in text


def test_a_null_flavor_category_carries_the_code_systems_own_display(
        app, client):
    """The `text` for v3-NullFlavor#ASKU is the code system's own display
    (an earlier wording drew 4 validator errors as a coding display; check-in
    output now carries no coding.display at all, ruling B).

    MUTATION: label ASKU 'Asked but no answer' again -> red."""
    null_flavor = 'http://terminology.hl7.org/CodeSystem/v3-NullFlavor'
    out, _ = _patient_with(app, client, extension=[
        {'url': RACE, 'extension': [
            {'url': 'ombCategory', 'valueCoding': {
                'system': null_flavor, 'code': 'ASKU'}},
            {'url': 'text', 'valueString': 'CANARY-race-text'}]}])
    race, = out['extension']
    assert race['extension'] == [
        {'url': 'ombCategory', 'valueCoding': {
            'system': null_flavor, 'code': 'ASKU'}},
        {'url': 'text', 'valueString': 'asked but unknown'}]


# --- QA recheck: A (Organization.active) and B (our label in text) --------

@pytest.mark.parametrize('active,expected', [(True, True), (False, False),
                                             ('yes', None)])
def test_the_payor_keeps_a_boolean_active(app, client, active, expected):
    """us-core-organization requires `active`.

    MUTATION: drop the `active` copy in _checkin_organization -> red."""
    org = f.organization()
    org['active'] = active
    f.seed(app, f.patient(), f.coverage(), org)
    resp = f.export(client, f.token(client, ['Coverage']), ['Coverage'])
    out, = f.resources(resp, 'Organization')
    assert out.get('active') == expected


def _all_displays(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == 'display':
                yield value
            yield from _all_displays(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _all_displays(value)


def test_check_in_output_has_no_coding_display(app, client):
    """Ruling B: a validator checks coding.display against the code
    system's display, and ours differs (CVX 150, RxNorm 314076).

    MUTATION: drop the _codes_with_our_text call -> red."""
    f.seed(app, f.patient(), f.condition(), f.coverage(), f.organization())
    types = ['Patient', 'Condition', 'Coverage']
    resp = f.export(client, f.token(client, types), types)
    assert list(_all_displays(resp.get_json())) == []


def test_our_label_moves_to_codeable_concept_text(app, client):
    """The label is r6/terminology.py's, keyed by code; upstream text never.

    MUTATION: keep the upstream `text` instead of setting ours -> red."""
    from r6.terminology import SNOMED, lookup
    label = lookup(SNOMED, '44054006')
    assert label
    f.seed(app, f.patient(), f.condition())
    resp = f.export(client, f.token(client, ['Condition']), ['Condition'])
    cond, = f.resources(resp, 'Condition')
    assert cond['code']['text'] == label
    assert f.CANARY_DISPLAY not in resp.get_data(as_text=True)


def test_an_unrecognised_code_gets_no_text(app, client):
    """MUTATION: leave `text` alone when nothing is recognised -> red, if
    redaction or relabelling ever leaves one there."""
    cond = f.condition('ci-cond-unknown')
    cond['code'] = {'coding': [{'system': 'https://ehr.example/cs',
                                'code': 'LOCAL-1',
                                'display': f.CANARY_DISPLAY}],
                    'text': f.CANARY_DISPLAY}
    f.seed(app, f.patient(), cond)
    resp = f.export(client, f.token(client, ['Condition']), ['Condition'])
    out, = f.resources(resp, 'Condition')
    assert out['code'] == {'coding': [{'system': 'https://ehr.example/cs',
                                       'code': 'LOCAL-1'}]}


def test_other_profiles_keep_their_display(app):
    """Ruling B is check-in only: intake still relabels coding.display."""
    from r6.access import _intake_profile
    out = _intake_profile(f.condition())
    assert out['code']['coding'][0].get('display')


def test_a_concept_built_after_the_relabel_still_gets_our_text(app, client):
    """Coverage.type is rebuilt code-only AFTER apply_redaction relabels, so
    only _codes_with_our_text can give it a label. A table code is used so
    the label exists (synthetic: Coverage.type is normally v3-ActCode).

    MUTATION: stop setting `text` from the lookup -> red."""
    from r6.terminology import SNOMED, lookup
    cov = f.coverage()
    cov['type'] = {'coding': [{'system': SNOMED, 'code': '44054006',
                               'display': f.CANARY_DISPLAY}]}
    f.seed(app, f.patient(), cov, f.organization())
    resp = f.export(client, f.token(client, ['Coverage']), ['Coverage'])
    out, = f.resources(resp, 'Coverage')
    assert out['type'] == {'coding': [{'system': SNOMED, 'code': '44054006'}],
                           'text': lookup(SNOMED, '44054006')}
