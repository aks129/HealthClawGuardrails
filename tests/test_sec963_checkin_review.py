"""Security review of #963 (FHI-53, the `checkin` export) at fc911bb.

Tests named `test_F*` are findings: they fail on fc911bb and pass once the
finding is fixed. The rest pin attacks that the PR already refuses.
All data is synthetic (tests/checkin_fixtures.py).
"""

import pytest

from r6 import checkin as checkin_mod
from r6.audit import AuditWriteError
from r6.stepup import clear_nonce_cache
from tests import checkin_fixtures as f

LEGACY_V2_0203 = 'http://hl7.org/fhir/v2/0203'
CANARY_NETWORK = 'CANARY-Network-Marisol-Rivera'
CANARY_NESTED = 'CANARY-Nested-Display-Name'


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', f.MINT_SECRET)
    clear_nonce_cache()


@pytest.fixture
def records(app):
    f.seed(app, f.patient(), f.patient(f.SIBLING, given='Bo'),
           f.condition(), f.condition('ci-cond-sib', pid=f.SIBLING),
           f.coverage(), f.organization())


# --- F1: a check-in token is a general tenant read credential -------------


def test_F1_a_check_in_token_cannot_read_an_unticked_type(
        client, records, monkeypatch):
    """Minted for {Patient} only, it reads the patient's Condition on the
    ordinary FHIR read path."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    tok = f.token(client, ['Patient'])
    resp = client.get('/r6/fhir/Condition/ci-cond-1',
                      headers={'X-Tenant-Id': f.TENANT,
                               'X-Step-Up-Token': tok})
    assert resp.status_code == 401, resp.status_code


def test_F1_a_check_in_token_cannot_read_another_patient(
        client, records, monkeypatch):
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    tok = f.token(client, ['Patient'])
    resp = client.get(f'/r6/fhir/Patient/{f.SIBLING}',
                      headers={'X-Tenant-Id': f.TENANT,
                               'X-Step-Up-Token': tok})
    assert resp.status_code == 401, resp.status_code


def test_F1_a_spent_check_in_token_reads_nothing(client, records,
                                                  monkeypatch):
    """Single use holds on the export only: after it is spent, the same
    token still searches the whole tenant for the rest of its TTL."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    tok = f.token(client, ['Patient'])
    assert f.export(client, tok, ['Patient']).status_code == 200
    resp = client.get('/r6/fhir/Condition',
                      headers={'X-Tenant-Id': f.TENANT,
                               'X-Step-Up-Token': tok})
    assert resp.status_code == 401, resp.status_code


# --- F2: an SS-typed identifier under the legacy v2-0203 URL leaves -------

def test_F2_an_ss_coding_under_the_legacy_v2_url_never_leaves(app, client):
    cov = f.coverage()
    cov['identifier'] = [{
        'system': 'https://payer.example/member',
        'type': {'coding': [
            {'system': 'http://terminology.hl7.org/CodeSystem/v2-0203',
             'code': 'MB'},
            {'system': LEGACY_V2_0203, 'code': 'SS'}]},
        'value': f.CANARY_SSN}]
    f.seed(app, f.patient(), cov, f.organization())
    resp = f.export(client, f.token(client, ['Coverage']), ['Coverage'])
    assert resp.status_code == 200
    assert f.CANARY_SSN not in resp.get_data(as_text=True)


# --- F3: Coverage.network is upstream free text and survives --------------

def test_F3_coverage_network_free_text_never_leaves(app, client):
    cov = f.coverage()
    cov['network'] = CANARY_NETWORK
    f.seed(app, f.patient(), cov, f.organization())
    resp = f.export(client, f.token(client, ['Coverage']), ['Coverage'])
    assert resp.status_code == 200
    assert CANARY_NETWORK not in resp.get_data(as_text=True)


# --- F4: a request the export will refuse with 400 still burns the token --

def test_F4_a_missing_patient_id_does_not_spend_the_token(client, records):
    tok = f.token(client, ['Patient'])
    assert f.export(client, tok, ['Patient'],
                    patient_id=None).status_code == 400
    assert f.export(client, tok, ['Patient']).status_code == 200


# --- F5: display/text nested in the verbatim Patient fields survives ------

def test_F5_a_display_nested_in_a_verbatim_patient_field_never_leaves(
        app, client):
    pt = f.patient()
    pt['address'][0]['extension'] = [{
        'url': 'https://ehr.example/care-of',
        'valueReference': {'reference': 'Practitioner/ci-pr-1',
                           'display': CANARY_NESTED}}]
    f.seed(app, pt)
    resp = f.export(client, f.token(client, ['Patient']), ['Patient'])
    assert resp.status_code == 200
    assert CANARY_NESTED not in resp.get_data(as_text=True)


# --- held: attacks the PR refuses -------------------------------------------

def test_a_cross_patient_id_in_another_tenant_exports_nothing(app, client):
    f.seed(app, f.patient('ci-pt-foreign'), tenant=f.OTHER_TENANT)
    f.seed(app, f.patient())
    resp = f.export(client, f.token(client, ['Patient']), ['Patient'],
                    patient_id='ci-pt-foreign')
    assert resp.status_code == 200
    assert f.resources(resp) == []


def test_a_sibling_subscriber_and_patient_payor_are_dropped(app, client):
    cov = f.coverage(subscriber={'reference': f'Patient/{f.SIBLING}'})
    cov['payor'] = [{'reference': f'Patient/{f.SIBLING}'},
                    {'reference': 'Organization/ci-org-1/_history/1'},
                    {'reference': 'https://evil.example/Organization/x'}]
    f.seed(app, f.patient(), cov, f.organization())
    resp = f.export(client, f.token(client, ['Coverage']), ['Coverage'])
    c, = f.resources(resp, 'Coverage')
    assert 'subscriber' not in c and 'payor' not in c
    assert f.resources(resp, 'Organization') == []


def test_a_contained_related_person_never_leaves(app, client):
    cond = f.condition()
    cond['contained'] = [{'resourceType': 'RelatedPerson', 'id': 'rp',
                          'name': [{'family': 'CANARY-Contained'}]}]
    cond['asserter'] = {'reference': '#rp'}
    cond['extension'] = [{'url': 'https://x.example/e', 'valueReference': {
        'reference': 'https://ehr.example/fhir/RelatedPerson/9'}}]
    f.seed(app, f.patient(), cond)
    resp = f.export(client, f.token(client, ['Condition']), ['Condition'])
    text = resp.get_data(as_text=True)
    assert 'CANARY-Contained' not in text and 'RelatedPerson' not in text


def test_a_tampered_op_is_refused(client, records):
    tok = f.token(client, ['Patient'])
    payload, sig = tok.rsplit('.', 1)
    import base64
    import json
    claims = json.loads(base64.urlsafe_b64decode(payload))
    claims['op'] = 'checkin:Condition,Patient'
    forged = base64.urlsafe_b64encode(
        json.dumps(claims).encode()).decode() + '.' + sig
    assert f.export(client, forged, ['Condition', 'Patient']).status_code \
        == 401


@pytest.mark.parametrize('extra', [{'ttl': 99999}, {'aud': 'x'},
                                   {'op': 'checkin:Coverage'},
                                   {'scope': 'write'}])
def test_the_mint_refuses_caller_chosen_claims(client, extra):
    assert f.mint(client, ['Patient'], **extra).status_code == 400


def test_a_check_in_token_cannot_reach_the_deidentified_profile(client,
                                                                 records):
    tok = f.token(client, ['Patient'])
    resp = client.post('/r6/fhir/$share-bundle',
                       json={'profile': 'deidentified', 'patient_id': f.PT},
                       headers={'X-Tenant-Id': f.TENANT,
                                'X-Step-Up-Token': tok})
    assert resp.status_code == 401


def test_an_audit_write_failure_releases_no_bundle(app, client, records,
                                                   monkeypatch):
    def boom(*a, **k):
        raise AuditWriteError('synthetic audit failure')
    monkeypatch.setattr(checkin_mod, 'add_audit_event', boom)
    app.config['PROPAGATE_EXCEPTIONS'] = False
    resp = f.export(client, f.token(client, ['Patient', 'Coverage']),
                    ['Patient', 'Coverage'])
    assert resp.status_code >= 500
    text = resp.get_data(as_text=True)
    for leaked in ('Testperson', f.MEMBER_ID, f.PAYOR_NAME, 'entry'):
        assert leaked not in text


def test_the_size_cap_body_carries_nothing(app, client, monkeypatch):
    monkeypatch.setattr(checkin_mod, 'MAX_BUNDLE_BYTES', 10)
    f.seed(app, f.patient())
    resp = f.export(client, f.token(client, ['Patient']), ['Patient'])
    assert resp.status_code == 413
    assert resp.get_json() == {'error': 'checkin_too_large'}
