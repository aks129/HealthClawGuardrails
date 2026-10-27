"""Real vendor feeds through the check-in and ktc exports (#1058).

Oracle Health and SMART Health IT send no `meta.profile`, and both exports
kept only claimed rows, so both feeds exported nothing: 0 of 73 and 0 of 25
check-in records in the live run. The ruling: check an unclaimed record
against a light US Core table at export time (r6/us_core_profiles.py
`classify`), claim only what passes, and keep the rest home with a reason
in fixed words. Alongside it, references that point outside the bundle are
dropped, and the sandbox codes carry our labels.

Records are tests/vendor_record_fixtures.py: vendor-shaped, invented.
Each test names the mutation that turns it red.
"""

import json
from collections import Counter

import pytest

from r6 import checkin, terminology, us_core_profiles
from tests import checkin_fixtures as f
from tests import vendor_record_fixtures as v

SIX = sorted(checkin.CHECKIN_TYPES)
US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/'


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', f.MINT_SECRET)
    monkeypatch.setenv('KTC_EXPORT_ENABLED', 'true')


@pytest.fixture(autouse=True)
def _static_table_only(monkeypatch):
    """Labels come from the static table, never the opt-in resolver."""
    from r6 import terminology_resolver
    monkeypatch.setattr(terminology_resolver, 'resolve', lambda *a: None)


def _export(client, types=SIX):
    return f.export(client, f.token(client, types), types)


def _ktc_export(client, types=SIX):
    resp = client.post('/r6/fhir/internal/step-up-token', json={
        'tenant_id': f.TENANT, 'purpose': 'ktc', 'resource_types': types},
        headers={'X-Tenant-Id': f.TENANT,
                 'X-Internal-Secret': f.MINT_SECRET})
    tok = resp.get_json()['token']
    return f.export(client, tok, types, profile='ktc')


def _counts(resp):
    return dict(Counter(r['resourceType'] for r in f.resources(resp)))


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


# --- 0 -> N: the two vendor shapes export ---------------------------------

def test_an_oracle_shaped_patient_exports_what_conforms(app, client):
    """Was 0 of 30. The words-only allergy, the Coverage whose payor is
    not on file and the four entered-in-error Conditions stay home;
    everything else goes.

    MUTATION: restore `if not has_claim(stored): continue` -> red (0)."""
    f.seed(app, *v.oracle_records())
    resp = _export(client)
    assert resp.status_code == 200
    assert _counts(resp) == {'Patient': 1, 'Condition': 2,
                             'AllergyIntolerance': 12,
                             'MedicationRequest': 2, 'Immunization': 7}


def test_a_smart_shaped_patient_exports_what_conforms(app, client):
    """Was 0 of 16. SMART's Conditions carry no category, which US Core
    requires, so they stay home; nothing is inferred to fill it."""
    f.seed(app, *v.smart_records())
    resp = _export(client)
    assert resp.status_code == 200
    assert _counts(resp) == {'Patient': 1, 'AllergyIntolerance': 2,
                             'MedicationRequest': 6, 'Immunization': 4}


def test_ktc_exports_an_oracle_shaped_patient_without_any_claim(app, client):
    """ktc answered 422 (no exportable Patient). It now carries the
    checked records, and still no `meta.profile` leaves.

    MUTATION: stamp in collect for ktc too -> red on the profile check."""
    f.seed(app, *v.oracle_records())
    resp = _ktc_export(client)
    assert resp.status_code == 200
    assert _counts(resp)['Patient'] == 1
    assert _counts(resp)['Condition'] == 2
    for res in f.resources(resp):
        assert 'profile' not in res.get('meta', {}), res['resourceType']


# --- the claim gate -------------------------------------------------------

def test_each_checked_record_claims_the_profile_it_was_checked_against(
        app, client):
    f.seed(app, *v.oracle_records(), v.payor())
    want = {
        'Patient': 'us-core-patient',
        'Condition': 'us-core-condition-problems-health-concerns',
        'AllergyIntolerance': 'us-core-allergyintolerance',
        'MedicationRequest': 'us-core-medicationrequest',
        'Immunization': 'us-core-immunization',
        'Coverage': 'us-core-coverage',
    }
    resp = _export(client)
    seen = set()
    for res in f.resources(resp):
        if res['resourceType'] == 'Organization':
            continue
        seen.add(res['resourceType'])
        assert res['meta']['profile'] == [
            US_CORE + want[res['resourceType']]], res['id']
    assert seen == set(want)


def test_a_record_that_fails_the_check_is_not_exported(app, client):
    """THE GATE. An unclaimed record that does not pass stays home.

    MUTATION: `classify` returns a claim for anything of a known type ->
    red."""
    allergy = v.oracle_allergy('or-alg-words', None)
    no_category = v.smart_records()[1]
    no_gender = v.patient()
    no_gender.pop('gender')
    f.seed(app, no_gender, allergy, no_category)
    resp = _export(client)
    text = resp.get_data(as_text=True)
    assert 'or-alg-words' not in text
    assert no_category['id'] not in text
    assert f.resources(resp, 'Patient') == []


def test_held_records_are_counted_by_a_fixed_reason(app, client):
    """The reason is ours, in fixed words keyed by a code: nothing of the
    record is in it.

    MUTATION: put the record id in the reason -> red."""
    f.seed(app, *v.oracle_records(), *v.smart_records('sm-pt-other'))
    resp = _export(client)
    held = json.loads(resp.headers['X-Records-Held'])
    assert held == {'AllergyIntolerance': {'no-standard-code': 1},
                    'Condition': {'entered-in-error': 4},
                    'Coverage': {'no-insurer': 1}}
    for reasons in held.values():
        for code in reasons:
            words = us_core_profiles.HELD_REASONS[code]
            assert words and v.CANARY not in words
    assert f.export_audits(app)[0][1].endswith('; held=6')


def test_nothing_held_sends_no_held_header(app, client):
    f.seed(app, f.patient())
    resp = _export(client, ['Patient'])
    assert 'X-Records-Held' not in resp.headers


def test_the_coverage_goes_when_its_payor_is_on_file(app, client):
    f.seed(app, *v.oracle_records(), v.payor())
    resp = _export(client)
    cov, = f.resources(resp, 'Coverage')
    assert cov['payor'] == [{'reference': 'Organization/or-payor'}]
    org, = f.resources(resp, 'Organization')
    assert org['name'] == 'Synthetic Vendor Health Plan'


def test_a_held_coverage_takes_no_payor_with_it(app, client):
    """A Coverage that stays home for another reason does not bring its
    payor Organization out alone."""
    cov = v.oracle_coverage()
    cov.pop('relationship')
    f.seed(app, v.patient(), cov, v.payor())
    resp = _export(client)
    assert f.resources(resp, 'Coverage') == []
    assert f.resources(resp, 'Organization') == []


def test_a_foreign_type_claim_is_replaced_by_the_checked_one(app, client):
    """A Condition claiming us-core-patient is unclaimed for its type; once
    checked it carries only the claim we checked."""
    cond = v.oracle_condition('or-cond-x', '91302008', True)
    cond['meta']['profile'] = [US_CORE + 'us-core-patient']
    f.seed(app, v.patient(), cond)
    resp = _export(client, ['Condition'])
    out, = f.resources(resp, 'Condition')
    assert out['meta']['profile'] == [
        US_CORE + 'us-core-condition-problems-health-concerns']


def test_a_claimed_record_is_not_reclassified(app, client):
    """The upstream claim still counts on its own (an allow-listed claim
    for the row's own type), as before #1058."""
    f.seed(app, f.patient(), f.condition())
    resp = _export(client, ['Condition'])
    assert len(f.resources(resp, 'Condition')) == 1


# --- entered in error, and claims a drop breaks (PR #1062 review) ----------

def _eie(res):
    """`res` marked entered-in-error where its type says so."""
    if res['resourceType'] in ('Condition', 'AllergyIntolerance'):
        res['verificationStatus'] = {'coding': [{'code': 'entered-in-error',
                                                 'system': (
            'http://terminology.hl7.org/CodeSystem/condition-ver-status'
            if res['resourceType'] == 'Condition' else
            'http://terminology.hl7.org/CodeSystem/'
            'allergyintolerance-verification')}]}
        res.pop('clinicalStatus', None)
    else:
        res['status'] = 'entered-in-error'
    return res


def _claimed(res, name):
    res['meta'] = f.claim(name)
    return res


def test_an_entered_in_error_record_never_exports_claimed_or_not(
        app, client):
    """A record its source withdrew is not the person's, whatever it
    claims. Main exported these: nothing on the export path read
    verificationStatus or status, and the wallet holds back only withdrawn
    allergies, after the export.

    MUTATION: skip the entered-in-error hold in collect -> red."""
    unclaimed = [
        _eie(v.oracle_condition('eie-cond', '91302008', True)),
        _eie(v.oracle_allergy('eie-alg', (v.RXNORM, '161'))),
        _eie(v.oracle_medication('eie-med')),
        _eie(v.oracle_immunization('eie-imm', '207')),
        _eie(v.oracle_coverage('eie-cov')),
    ]
    claimed = [
        _eie(_claimed(v.oracle_condition('eie-cond-c', '91302008', True),
                      'us-core-condition-problems-health-concerns')),
        _eie(_claimed(v.oracle_allergy('eie-alg-c', (v.RXNORM, '161')),
                      'us-core-allergyintolerance')),
        _eie(_claimed(v.oracle_medication('eie-med-c'),
                      'us-core-medicationrequest')),
        _eie(_claimed(v.oracle_immunization('eie-imm-c', '207'),
                      'us-core-immunization')),
        _eie(f.coverage('eie-cov-c')),
    ]
    f.seed(app, v.patient(), v.payor(), f.organization(), *unclaimed,
           *claimed)
    resp = _export(client)
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert 'eie-' not in text
    held = json.loads(resp.headers['X-Records-Held'])
    for rtype in ('Condition', 'AllergyIntolerance', 'MedicationRequest',
                  'Immunization', 'Coverage'):
        assert held[rtype] == {'entered-in-error': 2}, rtype
    assert us_core_profiles.HELD_REASONS['entered-in-error']
    assert f.export_audits(app)[0][1].endswith('; held=10')


@pytest.mark.parametrize('where', ['top', 'nested'])
def test_a_record_with_a_modifier_extension_is_held_not_changed(
        app, client, where):
    """FHIR: an unknown modifierExtension may change the record's meaning.
    The export drops extensions, so such a record is held, claimed or not,
    rather than sent with its meaning silently changed (security review of
    #1062, F3).

    MUTATION: drop the modifierExtension check in status_hold -> red."""
    mod = [{'url': 'urn:example:negated', 'valueBoolean': True}]
    cond = _claimed(v.oracle_condition('mod-cond', '91302008', True),
                    'us-core-condition-problems-health-concerns')
    if where == 'top':
        cond['modifierExtension'] = mod
    else:
        cond['category'][0]['modifierExtension'] = mod
    f.seed(app, v.patient(), cond)
    resp = _export(client)
    assert 'mod-cond' not in resp.get_data(as_text=True)
    assert json.loads(resp.headers['X-Records-Held']) == {
        'Condition': {'unknown-modifier': 1}}
    assert us_core_profiles.HELD_REASONS['unknown-modifier']


def test_a_patient_keeps_only_its_rebuilt_race_extension(app, client):
    pt = v.patient()
    pt['extension'] = [
        {'url': US_CORE + 'us-core-race', 'extension': [
            {'url': 'ombCategory', 'valueCoding': {
                'system': 'urn:oid:2.16.840.1.113883.6.238',
                'code': '2106-3'}},
            {'url': 'text', 'valueString': v.CANARY}]},
        {'url': 'urn:example:next-of-kin',
         'valueHumanName': {'family': v.CANARY}}]
    f.seed(app, pt)
    resp = _export(client, ['Patient'])
    out, = f.resources(resp, 'Patient')
    assert [e['url'] for e in out['extension']] == [US_CORE + 'us-core-race']
    assert v.CANARY not in resp.get_data(as_text=True)


_DAR = {'extension': [{
    'url': 'http://hl7.org/fhir/StructureDefinition/data-absent-reason',
    'valueCode': 'unknown'}]}


def test_a_claimed_record_whose_required_value_was_only_an_extension_is_held(
        app, client):
    """A primitive given only as data-absent-reason (`_occurrenceDateTime`,
    `_gender`) has no value once the export strips `_` keys, so the
    claimed record no longer meets its claim (#1062 re-check).

    MUTATION: skip the re-check of claimed records the profile changed ->
    red."""
    imm = _claimed(v.oracle_immunization('dar-imm', '207'),
                   'us-core-immunization')
    imm.pop('occurrenceDateTime')
    imm['_occurrenceDateTime'] = _DAR
    pt = _claimed(v.patient(), 'us-core-patient')
    pt.pop('gender')
    pt['_gender'] = _DAR
    f.seed(app, pt, imm)
    resp = _export(client)
    text = resp.get_data(as_text=True)
    assert 'dar-imm' not in text
    assert f.resources(resp, 'Patient') == []
    assert json.loads(resp.headers['X-Records-Held']) == {
        'Immunization': {'missing-details': 1},
        'Patient': {'no-gender': 1}}


def _empties(obj, path='$'):
    if isinstance(obj, dict):
        if not obj:
            yield path
        for key, value in obj.items():
            yield from _empties(value, f'{path}.{key}')
    elif isinstance(obj, list):
        if not obj:
            yield path
        for i, value in enumerate(obj):
            yield from _empties(value, f'{path}[{i}]')


@pytest.mark.parametrize('profile', ['checkin', 'ktc'])
def test_no_empty_object_or_list_leaves(app, client, profile):
    """FHIR ele-1: an element has a value or children. A requester that
    carried only an extension, and an empty manufacturer, do not go out as
    `{}`.

    MUTATION: skip the empty-element prune in _checkin_profile -> red."""
    med = v.oracle_medication('em-med')
    med['requester'] = {'extension': [{'url': 'urn:example:x',
                                       'valueString': v.CANARY}]}
    imm = v.oracle_immunization('em-imm', '207')
    imm['manufacturer'] = {}
    imm['reasonCode'] = []
    f.seed(app, *v.oracle_records(), med, imm)
    resp = (_export(client) if profile == 'checkin'
            else _ktc_export(client))
    assert resp.status_code == 200
    out = {r['id']: r for r in f.resources(resp)}
    assert 'requester' not in out['em-med']
    assert 'manufacturer' not in out['em-imm']
    assert list(_empties(resp.get_json()['entry'])) == []


def test_the_profile_itself_leaves_no_empty_element():
    """The profile prunes on its own, not only through the export's
    reference drop (which prunes too), so any other caller of
    _checkin_profile gets no `{}` either.

    MUTATION: skip the empty-element prune in _checkin_profile -> red."""
    from r6.access import _checkin_profile
    med = v.oracle_medication('em-med')
    med['requester'] = {'extension': [{'url': 'urn:example:x',
                                       'valueString': v.CANARY}]}
    med['dosageInstruction'] = [{}]
    out = _checkin_profile(med, patient_id=v.PT)
    assert 'requester' not in out
    assert 'dosageInstruction' not in out
    assert list(_empties(out)) == []


def test_ktc_holds_entered_in_error_records_too(app, client):
    f.seed(app, v.patient(),
           _eie(v.oracle_condition('eie-cond', '91302008', True)))
    resp = _ktc_export(client)
    assert resp.status_code == 200
    assert 'eie-cond' not in resp.get_data(as_text=True)
    assert json.loads(resp.headers['X-Records-Held']) == {
        'Condition': {'entered-in-error': 1}}


def test_a_claimed_refuted_condition_still_exports(app, client):
    """Ruling: refuted is not held. The export never filtered it for a
    claimed record, and refuted is a clinical finding (ruled out), not a
    withdrawn record. The wallet still holds back refuted allergies."""
    cond = f.condition()
    cond['verificationStatus'] = {'coding': [{
        'system': 'http://terminology.hl7.org/CodeSystem/condition-ver-status',
        'code': 'refuted'}]}
    f.seed(app, f.patient(), cond)
    resp = _export(client, ['Condition'])
    assert [c['id'] for c in f.resources(resp, 'Condition')] == ['ci-cond-1']


def test_a_claimed_coverage_that_loses_its_payor_is_held(app, client):
    """The claim arrived with the record, but the record we would send has
    no payor once the unresolvable one is dropped, so it no longer meets
    us-core-coverage.

    MUTATION: skip the re-check of records a drop changed -> red."""
    f.seed(app, f.patient(), f.coverage())
    resp = _export(client, ['Coverage'])
    assert f.resources(resp, 'Coverage') == []
    assert json.loads(resp.headers['X-Records-Held']) == {
        'Coverage': {'no-insurer': 1}}


def test_a_claimed_coverage_whose_payor_is_on_file_still_goes(app, client):
    f.seed(app, f.patient(), f.coverage(), f.organization())
    resp = _export(client, ['Coverage'])
    assert len(f.resources(resp, 'Coverage')) == 1
    assert 'X-Records-Held' not in resp.headers


# --- classify, per type ---------------------------------------------------

def _profiled(resource):
    from r6.access import _checkin_profile
    return _checkin_profile(resource, patient_id=v.PT)


@pytest.mark.parametrize('category,claim', [
    ('problem-list-item', 'us-core-condition-problems-health-concerns'),
    ('encounter-diagnosis', 'us-core-condition-encounter-diagnosis'),
])
def test_the_condition_claim_follows_its_category(category, claim):
    cond = v.oracle_condition('c', '91302008', True)
    cond['category'] = [{'coding': [{
        'system': 'http://terminology.hl7.org/CodeSystem/condition-category',
        'code': category}]}]
    assert us_core_profiles.classify(_profiled(cond)) == (
        US_CORE + claim, None)


@pytest.mark.parametrize('mutate,reason', [
    (lambda r: r.pop('identifier'), 'no-identifier'),
    (lambda r: r.pop('name'), 'no-name'),
    (lambda r: r.update(gender='mle'), 'no-gender'),
])
def test_a_patient_needs_identifier_name_and_gender(mutate, reason):
    pt = v.patient()
    mutate(pt)
    assert us_core_profiles.classify(_profiled(pt)) == (None, reason)


def test_an_ssn_only_patient_fails_on_what_leaves_not_what_is_stored():
    """The check reads the profiled record: an SSN is dropped by the
    check-in profile, so a Patient identified only by one has no
    identifier when it leaves."""
    pt = v.patient()
    pt['identifier'] = [{'system': 'http://hl7.org/fhir/sid/us-ssn',
                         'value': '999001111'}]
    assert us_core_profiles.classify(_profiled(pt)) == (None, 'no-identifier')


def test_an_allergy_without_status_unless_entered_in_error_is_held():
    """FHIR ait-1 and ait-2: clinicalStatus present unless the record is
    entered-in-error, and absent when it is."""
    alg = v.oracle_allergy('a', (v.RXNORM, '161'))
    alg.pop('clinicalStatus')
    assert us_core_profiles.classify(_profiled(alg)) == (
        None, 'status-unclear')
    alg['verificationStatus']['coding'][0]['code'] = 'entered-in-error'
    assert us_core_profiles.classify(_profiled(alg))[0] == (
        US_CORE + 'us-core-allergyintolerance')


def test_a_medication_coded_only_outside_rxnorm_is_held():
    med = v.oracle_medication('m')
    med['medicationCodeableConcept']['coding'].pop(0)
    assert us_core_profiles.classify(_profiled(med)) == (
        None, 'no-standard-code')


def test_an_immunization_with_no_date_is_held():
    imm = v.oracle_immunization('i', '207')
    imm.pop('occurrenceDateTime')
    assert us_core_profiles.classify(_profiled(imm)) == (
        None, 'missing-details')


def test_a_type_outside_the_six_is_never_claimed():
    assert us_core_profiles.classify(v.payor()) == (None, 'missing-details')


# --- references -----------------------------------------------------------

def test_no_reference_in_the_bundle_points_outside_it(app, client):
    """Encounter, Practitioner, Location and the clinic Organization are
    not in the bundle, so nothing may point at them.

    MUTATION: skip drop_dangling_references in collect -> red."""
    f.seed(app, *v.oracle_records(), v.payor())
    resp = _export(client)
    entries = resp.get_json()['entry']
    present = {f"{e['resource']['resourceType']}/{e['resource']['id']}"
               for e in entries}
    refs = [r for e in entries for r in _refs(e['resource'])]
    assert refs
    for ref in refs:
        assert ref in present, f'{ref} resolves to no entry'
    imm = f.resources(resp, 'Immunization')[0]
    assert 'performer' not in imm and 'location' not in imm
    med = f.resources(resp, 'MedicationRequest')[0]
    assert 'requester' not in med and 'encounter' not in med


def test_a_reference_to_a_held_record_goes_with_it(app, client):
    """A medicine given for a problem that stayed home does not point at it.

    MUTATION: skip the drop after holding in collect -> red."""
    records = v.smart_records()
    held = records[1]
    med = records[6]
    med['reasonReference'] = [{'reference': f"Condition/{held['id']}"}]
    f.seed(app, *records)
    resp = _export(client)
    out = next(r for r in f.resources(resp) if r['id'] == med['id'])
    assert 'reasonReference' not in out
    assert held['id'] not in resp.get_data(as_text=True)


def test_the_named_patient_reference_stays_when_patient_is_not_asked_for(
        app, client):
    """subject is required, and the receiver knows whose check-in it is."""
    f.seed(app, *v.oracle_records())
    resp = _export(client, ['Condition'])
    for cond in f.resources(resp, 'Condition'):
        assert cond['subject'] == {'reference': f'Patient/{v.PT}'}


def test_a_ktc_bundle_has_no_dangling_reference(app, client):
    f.seed(app, *v.smart_records())
    resp = _ktc_export(client)
    entries = resp.get_json()['entry']
    present = {f"{e['resource']['resourceType']}/{e['resource']['id']}"
               for e in entries}
    for e in entries:
        for ref in _refs(e['resource']):
            assert ref in present, ref


# --- labels and words ----------------------------------------------------

def test_no_vendor_words_leave(app, client):
    f.seed(app, *v.oracle_records(), v.payor(), *v.smart_records('sm-pt'))
    for resp in (_export(client), _ktc_export(client)):
        assert v.CANARY not in resp.get_data(as_text=True)
        assert v.CANARY not in resp.headers.get('X-Records-Held', '')


_SANDBOX_CODES = (
    [(v.SNOMED, c) for _, c, _ in v.ORACLE_CONDITIONS]
    + [coding for _, coding in v.ORACLE_ALLERGIES if coding]
    + [(v.RXNORM, '519')]
    + [(v.CVX, c) for _, c in v.ORACLE_IMMUNIZATIONS]
    + [(v.SNOMED, c) for c in v.SMART_CONDITIONS + v.SMART_ALLERGIES]
    + [(v.RXNORM, c) for c in v.SMART_MEDICATIONS]
    + [(v.CVX, c) for c in v.SMART_VACCINES])


@pytest.mark.parametrize('system,code', _SANDBOX_CODES)
def test_every_sandbox_code_has_our_label(system, code):
    """MUTATION: delete any one of the #1058 rows -> red for that code."""
    assert terminology.lookup(system, code)


def test_exported_concepts_carry_our_label_as_their_text(app, client):
    f.seed(app, *v.oracle_records(), *v.smart_records('sm-pt'))
    resp = _export(client)
    fields = {'Condition': 'code', 'AllergyIntolerance': 'code',
              'MedicationRequest': 'medicationCodeableConcept',
              'Immunization': 'vaccineCode'}
    for res in f.resources(resp):
        field = fields.get(res['resourceType'])
        if not field:
            continue
        concept = res[field]
        labels = [terminology.lookup(c['system'], c['code'])
                  for c in concept['coding']]
        assert concept.get('text') == next(lb for lb in labels if lb)
