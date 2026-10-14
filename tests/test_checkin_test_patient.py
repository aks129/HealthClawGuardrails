"""The synthetic US Core test patient for SMART Health Check-in (FHI-51).

`careagents/checkin/us_core_test_patient.json` is the record set a check-in
wallet answers from while it is being tested: one made-up person with every
item the connectathon's share-records request asks for. It lives under
`careagents/` because the CareAgents image copies only that directory and the
wallet (FHI-54) serves it from there; the engine loads the same file:

    uv run python scripts/seed_demo_tenant.py --db-mode \\
        --tenant-id checkin-test \\
        --bundle-file careagents/checkin/us_core_test_patient.json

A Check-in item matches a record by the exact `meta.profile` it claims, so
these tests pin the claims, the one record that deliberately claims nothing,
that every reference resolves inside the bundle, and that nothing in it could
be mistaken for a real person. Conformance to the profiles themselves was
checked with the HL7 validator (see the pull request); `r6/validator.py`, the
repo's own, is structural only and runs here as a floor.
"""
import json
import re
from pathlib import Path

import pytest

FIXTURE = (Path(__file__).resolve().parent.parent / 'careagents' / 'checkin'
           / 'us_core_test_patient.json')
BUNDLE = json.loads(FIXTURE.read_text())
RESOURCES = [e['resource'] for e in BUNDLE['entry']]

US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/'

#: The connectathon's share-records items, and the profile each one asks for
#: (https://smart-health-checkin.org/connectathon/requests/records.json).
REQUESTED = {
    'Patient': US_CORE + 'us-core-patient',
    'Condition': US_CORE + 'us-core-condition-problems-health-concerns',
    'AllergyIntolerance': US_CORE + 'us-core-allergyintolerance',
    'MedicationRequest': US_CORE + 'us-core-medicationrequest',
    'Immunization': US_CORE + 'us-core-immunization',
    'Coverage': US_CORE + 'us-core-coverage',
}
#: What the requested records point at, carried so their references resolve.
SUPPORTING = {
    'Organization': US_CORE + 'us-core-organization',
    'Practitioner': US_CORE + 'us-core-practitioner',
}

UNPROFILED_ID = 'checkin-test-condition-unprofiled'


def _profiles(resource):
    return (resource.get('meta') or {}).get('profile') or []


def _references(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == 'reference' and isinstance(v, str):
                yield v
            else:
                yield from _references(v)
    elif isinstance(node, list):
        for v in node:
            yield from _references(v)


def test_is_a_collection_whose_full_urls_name_each_resource():
    assert BUNDLE['resourceType'] == 'Bundle'
    assert BUNDLE['type'] == 'collection'
    for entry in BUNDLE['entry']:
        r = entry['resource']
        assert entry['fullUrl'].endswith(f"/{r['resourceType']}/{r['id']}")


def test_every_requested_item_has_a_record_claiming_its_us_core_profile():
    for rtype, profile in REQUESTED.items():
        claiming = [r for r in RESOURCES
                    if r['resourceType'] == rtype and profile in _profiles(r)]
        assert claiming, f'no {rtype} claims {profile}'


def test_every_profile_claim_is_the_us_core_profile_for_its_type():
    expected = {**REQUESTED, **SUPPORTING}
    for r in RESOURCES:
        for p in _profiles(r):
            assert p == expected[r['resourceType']], (r['id'], p)


def test_exactly_one_record_claims_no_profile_and_it_is_a_requested_type():
    # A problem the wallet must NOT return for "Problems": it claims nothing,
    # so it matches nothing, and nobody stamps a claim on it to make it fit.
    unprofiled = [r for r in RESOURCES if not _profiles(r)]
    assert [r['id'] for r in unprofiled] == [UNPROFILED_ID]
    assert unprofiled[0]['resourceType'] in REQUESTED
    assert 'profile' not in unprofiled[0]['meta']


def test_every_reference_resolves_inside_the_bundle():
    local = {f"{r['resourceType']}/{r['id']}" for r in RESOURCES}
    refs = list(_references(RESOURCES))
    assert refs
    for ref in refs:
        assert ref in local, ref


def test_coverage_carries_synthetic_member_and_group_ids():
    (cov,) = [r for r in RESOURCES if r['resourceType'] == 'Coverage']
    member = [i for i in cov['identifier']
              if i['type']['coding'][0]['code'] == 'MB']
    assert member and member[0]['value'].startswith('FAKE-MEMBER-')
    assert cov['subscriberId'].startswith('FAKE-MEMBER-')
    classes = {c['type']['coding'][0]['code']: c['value'] for c in cov['class']}
    assert classes['group'].startswith('FAKE-GROUP-')
    assert classes['plan'].startswith('FAKE-PLAN-')


def test_nothing_in_it_could_be_a_real_person():
    assert {'code': 'HTEST'}.items() <= BUNDLE['meta']['tag'][0].items()
    for r in RESOURCES:
        for ident in r.get('identifier', []):
            assert ident['value'].startswith('FAKE-'), (r['id'], ident)
        for name in r.get('name', []) if isinstance(r.get('name'), list) else []:
            assert name['family'].startswith('Test'), (r['id'], name)
        for t in r.get('telecom', []):
            if t['system'] == 'phone':
                # 555-0100 to 555-0199: reserved for fiction.
                assert re.fullmatch(r'555-01\d\d', t['value']), t
            if t['system'] == 'email':
                assert t['value'].endswith('.test'), t   # RFC 2606


def test_allergies_are_real_entries_not_an_inferred_none():
    # The fixture has an allergy; "no known allergies" is never stood in for
    # one (AGENTS.md), so no refuted or no-known-allergy code appears here.
    allergies = [r for r in RESOURCES if r['resourceType'] == 'AllergyIntolerance']
    assert allergies
    for a in allergies:
        assert a['code']['coding'][0]['code'] != '716186003'   # SNOMED NKA
        assert a['verificationStatus']['coding'][0]['code'] == 'confirmed'


def test_every_resource_passes_the_repo_structural_validator():
    from r6.validator import R6Validator
    v = R6Validator()
    v.validator_url = ''          # structural only, whatever the env says
    for r in RESOURCES:
        result = v.validate_resource(r)
        assert result['valid'], (r['id'], result['operation_outcome'])


@pytest.mark.parametrize('rtype', sorted({r['resourceType'] for r in RESOURCES}))
def test_every_type_is_one_the_engine_stores(rtype):
    from r6.models import R6Resource
    assert R6Resource.is_supported_type(rtype)


def test_seeds_into_a_local_engine_and_reads_back_by_patient(app, client,
                                                             tenant_id,
                                                             tenant_headers):
    from r6.seed import seed_demo_data
    with app.app_context():
        assert seed_demo_data(tenant_id=tenant_id,
                              resources=RESOURCES) == len(RESOURCES)
        # Seeding twice adds nothing: the ids are stable.
        assert seed_demo_data(tenant_id=tenant_id, resources=RESOURCES) == 0

    r = client.get('/r6/fhir/Condition?patient=Patient/checkin-test-patient',
                   headers=tenant_headers)
    assert r.status_code == 200
    ids = sorted(e['resource']['id'] for e in r.get_json().get('entry', []))
    assert ids == ['checkin-test-condition-htn', UNPROFILED_ID]
