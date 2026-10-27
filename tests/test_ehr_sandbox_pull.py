"""scripts/ehr_sandbox_pull.py: one Epic or Oracle sandbox patient into a
sandbox tenant, over SMART Backend Services.

Every vendor payload here is hand-made and synthetic, shaped like what each
sandbox returns (Epic: dotted ids, reference displays, an `_include`d
Medication; Oracle: numeric ids, narrative, an OperationOutcome search
entry). No recorded vendor payload is used or committed. The CANARY strings
stand where a real feed puts a name.

The live sandbox run is QA evidence, not CI.
"""

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from scripts import ehr_sandbox_pull as pull

V3_ACT_REASON = 'http://terminology.hl7.org/CodeSystem/v3-ActReason'
RECORD_SOURCE_SYSTEM = 'https://healthclaw.io/fhir/CodeSystem/record-source'
US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/'
SECRET = 'test-internal-secret-not-real'
BEARER = 'CANARY-vendor-bearer-token-value'
STEP_UP = 'CANARY-step-up-token-value'
PRACTITIONER_CANARY = 'CANARY-Physician-Family-Medicine-MD'
SUBJECT_CANARY = 'CANARY-Lin-Derrick'
CLIENT_ID = 'synthetic-client-id-0001'

EPIC = pull.VENDORS['epic']
ORACLE = pull.VENDORS['oracle']

EPIC_TOKEN = 'https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token'
EPIC_FHIR = 'https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4'
ORACLE_TOKEN = ('https://authorization.cerner.com/tenants/'
                'ec2458f2-1e24-41c8-b71b-0e701af7583d/hosts/'
                'fhir-ehr-code.cerner.com/protocols/oauth2/profiles/'
                'smart-v1/token')
ORACLE_FHIR = ('https://fhir-ehr-code.cerner.com/r4/'
               'ec2458f2-1e24-41c8-b71b-0e701af7583d')


# --------------------------------------------------------------------------
# Synthetic fixtures


def _claim(name):
    return {'profile': [f'{US_CORE}{name}|7.0.0']}


def epic_patient(pid='eSynth.Pt-0001'):
    return {
        'resourceType': 'Patient', 'id': pid,
        'meta': dict(_claim('us-core-patient'),
                     source='https://fhir.epic.com/CANARY-source-endpoint',
                     tag=[{'system': RECORD_SOURCE_SYSTEM, 'code': 'fasten'},
                          {'system': V3_ACT_REASON, 'code': 'HTEST',
                           'display': 'CANARY-upstream-htest-display'}]),
        'name': [{'family': 'Synthetic', 'given': ['Ana']}],
        'birthDate': '1987-09-12',
        'gender': 'female',
    }


def epic_medication_request(pid='eSynth.Pt-0001'):
    return {
        'resourceType': 'MedicationRequest', 'id': 'eSynth.MedReq-01',
        'status': 'active', 'intent': 'order',
        'medicationReference': {'reference': 'Medication/eSynth.Med-01',
                                'display': 'CANARY-med-display'},
        'subject': {'reference': f'Patient/{pid}',
                    'display': SUBJECT_CANARY},
        'requester': {'reference': 'Practitioner/eSynth.Prac-01',
                      'display': PRACTITIONER_CANARY},
    }


def epic_medication():
    return {
        'resourceType': 'Medication', 'id': 'eSynth.Med-01',
        'code': {'coding': [{'system':
                             'http://www.nlm.nih.gov/research/umls/rxnorm',
                             'code': '197361'}],
                 'text': 'CANARY-med-text'},
    }


def epic_practitioner():
    return {'resourceType': 'Practitioner', 'id': 'eSynth.Prac-01',
            'name': [{'text': PRACTITIONER_CANARY}]}


def epic_condition(pid='eSynth.Pt-0001'):
    return {
        'resourceType': 'Condition', 'id': 'eSynth.Cond-01',
        'meta': _claim('us-core-condition-problems-health-concerns'),
        'code': {'coding': [{'system': 'http://snomed.info/sct',
                             'code': '44054006'}]},
        'subject': {'reference': f'Patient/{pid}'},
    }


def oracle_patient(pid='12724066'):
    return {
        'resourceType': 'Patient', 'id': pid,
        'meta': _claim('us-core-patient'),
        'text': {'status': 'generated',
                 'div': f'<div>{SUBJECT_CANARY}</div>'},
        'name': [{'family': 'Synthetic', 'given': ['Ben']}],
        'birthDate': '1990-04-01',
    }


def oracle_allergy(pid='12724066'):
    return {
        'resourceType': 'AllergyIntolerance', 'id': '9876543',
        'code': {'coding': [{'system': 'http://snomed.info/sct',
                             'code': '91936005'}]},
        'patient': {'reference': f'Patient/{pid}'},
    }


def oracle_outcome():
    return {'resourceType': 'OperationOutcome',
            'issue': [{'severity': 'information', 'code': 'informational'}]}


def _bundle(*resources, next_url=None, outcome=None):
    entry = [{'resource': r, 'search': {'mode': 'match'}} for r in resources]
    if outcome is not None:
        entry.append({'resource': outcome, 'search': {'mode': 'outcome'}})
    out = {'resourceType': 'Bundle', 'type': 'searchset', 'entry': entry}
    if next_url:
        out['link'] = [{'relation': 'next', 'url': next_url}]
    return out


# --------------------------------------------------------------------------
# A fake vendor transport: records every call, answers from a table.


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if body is not None else ''

    def json(self):
        return self._body


class FakeSession:
    """Answers the token POST with BEARER and each GET from `pages`, keyed
    by the URL path relative to the FHIR base (query ignored)."""

    def __init__(self, vendor, pages=None, token_response=None):
        self.vendor = vendor
        self.pages = pages or {}
        self.token_response = token_response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(('POST', url, kwargs))
        if self.token_response is not None:
            return self.token_response
        return FakeResponse(200, {'access_token': BEARER,
                                  'token_type': 'Bearer', 'expires_in': 300})

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        if url in self.pages:
            answer = self.pages[url]
        else:
            rel = url[len(self.vendor.fhir_base) + 1:].split('?')[0]
            answer = self.pages.get(rel, _bundle())
        if isinstance(answer, FakeResponse):
            return answer
        return FakeResponse(200, answer)


def epic_pages(pid='eSynth.Pt-0001'):
    return {
        f'Patient/{pid}': epic_patient(pid),
        'Condition': _bundle(epic_condition(pid)),
        'MedicationRequest': _bundle(epic_medication_request(pid),
                                     epic_medication(), epic_practitioner()),
    }


def oracle_pages(pid='12724066'):
    return {
        f'Patient/{pid}': oracle_patient(pid),
        'AllergyIntolerance': _bundle(oracle_allergy(pid),
                                      outcome=oracle_outcome()),
    }


@pytest.fixture(autouse=True)
def client_ids(tmp_path, monkeypatch):
    """Client ids come from env here; the local config file is pointed at
    a path that does not exist, so the owner's real file is never read."""
    monkeypatch.setattr(pull, 'CLIENT_CONFIG', tmp_path / 'no-such.json')
    monkeypatch.setenv('EPIC_SANDBOX_CLIENT_ID', CLIENT_ID)
    monkeypatch.setenv('ORACLE_SANDBOX_CLIENT_ID', CLIENT_ID)


@pytest.fixture
def key_file(tmp_path):
    """A throwaway P-384 key, never the real one."""
    key = ec.generate_private_key(ec.SECP384R1())
    path = tmp_path / 'test-key.pem'
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return path


@pytest.fixture
def private_key(key_file):
    return serialization.load_pem_private_key(key_file.read_bytes(), None)


@pytest.fixture
def exports(tmp_path, monkeypatch):
    out = tmp_path / 'exports'
    monkeypatch.setattr(pull, 'EXPORTS_DIR', out)
    return out


class FakeSeed:
    def __init__(self, status_code=201, created=None):
        self.status_code = status_code
        self.created = created
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        n = self.created
        if n is None:
            n = len(kwargs['json']['bundle']['entry'])
        return FakeResponse(self.status_code, {
            'tenant_id': kwargs['json']['tenant_id'], 'created_count': n,
            'step_up_token': STEP_UP, 'note': 'x'})


def _argv(key_file, vendor='epic', tenant='sandbox-epic-ana',
          patient='eSynth.Pt-0001'):
    return ['--vendor', vendor, '--patient', patient, '--tenant', tenant,
            '--key', str(key_file)]


def _b64json(part):
    return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))


# --------------------------------------------------------------------------
# URLs: exact strings, never from env


def test_sandbox_urls_are_the_exact_strings():
    assert EPIC.token_url == EPIC_TOKEN
    assert EPIC.fhir_base == EPIC_FHIR
    assert ORACLE.token_url == ORACLE_TOKEN
    assert ORACLE.fhir_base == ORACLE_FHIR
    # Production differs from the code sandbox by one token.
    assert 'fhir-ehr.cerner.com' not in ORACLE.token_url + ORACLE.fhir_base


def test_env_cannot_move_any_request(monkeypatch, key_file, exports):
    """Every env spelling a reader might reach for, pointed at production,
    and every request still lands on the exact sandbox strings."""
    prod = 'https://fhir-ehr.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d'
    for name in ('EPIC_FHIR_BASE', 'EPIC_TOKEN_URL', 'ORACLE_FHIR_BASE',
                 'ORACLE_TOKEN_URL', 'EHR_FHIR_BASE', 'EHR_TOKEN_URL',
                 'FHIR_BASE_URL', 'TOKEN_URL', 'EHR_SANDBOX_FHIR_BASE'):
        monkeypatch.setenv(name, prod)
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    session = FakeSession(ORACLE, oracle_pages())

    rc = pull.main(_argv(key_file, 'oracle', 'sandbox-oracle-ben', '12724066'),
                   session=session, seed_session=FakeSeed())

    assert rc == 0
    posts = [u for m, u, _ in session.calls if m == 'POST']
    assert posts == [ORACLE_TOKEN]
    for method, url, _ in session.calls:
        if method == 'GET':
            assert url.startswith(ORACLE_FHIR + '/'), url


def test_a_production_url_is_refused_by_the_fetch_guard():
    with pytest.raises(pull.PullError):
        pull.get_json(FakeSession(ORACLE), ORACLE, BEARER,
                      'https://fhir-ehr.cerner.com/r4/'
                      'ec2458f2-1e24-41c8-b71b-0e701af7583d/Patient/1')


def test_the_built_session_ignores_env_proxies_and_bundles():
    assert pull.new_session().trust_env is False


def test_every_request_disables_redirects(monkeypatch, key_file, exports):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    session = FakeSession(EPIC, epic_pages())
    seed = FakeSeed()

    assert pull.main(_argv(key_file), session=session, seed_session=seed) == 0

    assert session.calls
    for _, _, kwargs in session.calls:
        assert kwargs['allow_redirects'] is False
    assert seed.calls[0][1]['allow_redirects'] is False


def test_a_redirect_is_not_followed(private_key):
    prod = 'https://fhir-ehr.cerner.com/r4/x/Patient/1'
    session = FakeSession(ORACLE, {
        'Patient/12724066': FakeResponse(302, None, {'Location': prod})})

    with pytest.raises(pull.PullError):
        pull.fetch_patient(session, ORACLE, BEARER, '12724066')
    assert len(session.calls) == 1


def test_a_token_redirect_is_not_followed(private_key):
    session = FakeSession(EPIC, token_response=FakeResponse(
        307, None, {'Location': 'https://evil.example/token'}))

    with pytest.raises(pull.PullError):
        pull.get_token(session, EPIC, CLIENT_ID, private_key)
    assert len(session.calls) == 1


# --------------------------------------------------------------------------
# Paging


def test_a_same_base_next_link_is_followed():
    nxt = f'{EPIC_FHIR}/Condition?patient=p&page=2'
    session = FakeSession(EPIC, {
        'Condition': _bundle(epic_condition(), next_url=nxt),
        nxt: _bundle(dict(epic_condition(), id='eSynth.Cond-02'))})

    got = pull.search(session, EPIC, BEARER, 'Condition', {'patient': 'p'})

    assert [r['id'] for r in got] == ['eSynth.Cond-01', 'eSynth.Cond-02']


@pytest.mark.parametrize('nxt', [
    'https://evil.example/r4/Condition?page=2',
    'https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4@evil.example/x',
    'https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/STU3/Condition',
    'http://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/Condition',
    'https://fhir.epic.com.evil.example/interconnect-fhir-oauth/api/FHIR/R4/x',
])
def test_an_off_base_next_link_is_refused(nxt):
    session = FakeSession(EPIC, {
        'Condition': _bundle(epic_condition(), next_url=nxt)})

    with pytest.raises(pull.PullError):
        pull.search(session, EPIC, BEARER, 'Condition', {'patient': 'p'})
    assert all(url != nxt for _, url, _ in session.calls)


def test_paging_is_capped(monkeypatch):
    monkeypatch.setattr(pull, 'MAX_PAGES', 3)
    nxt = f'{EPIC_FHIR}/Condition?page=again'
    looping = _bundle(epic_condition(), next_url=nxt)
    session = FakeSession(EPIC, {'Condition': looping, nxt: looping})

    pull.search(session, EPIC, BEARER, 'Condition', {'patient': 'p'})

    assert len(session.calls) == 3


# --------------------------------------------------------------------------
# The client assertion


def test_assertion_header_and_claims(private_key):
    before = pull.now()
    jwt = pull.client_assertion(EPIC, CLIENT_ID, private_key)
    header_b64, claims_b64, _ = jwt.split('.')
    header, claims = _b64json(header_b64), _b64json(claims_b64)

    assert header == {'alg': 'ES384', 'typ': 'JWT',
                      'kid': 'healthclaw-ehr-sandbox-es384-1'}
    assert 'jku' not in header
    assert claims['iss'] == claims['sub'] == CLIENT_ID
    assert claims['aud'] == EPIC_TOKEN
    assert 0 < claims['exp'] - before <= 300
    assert claims['jti']
    assert 'scope' not in claims


def test_assertion_aud_is_the_exact_oracle_token_url(private_key):
    jwt = pull.client_assertion(ORACLE, CLIENT_ID, private_key)
    assert _b64json(jwt.split('.')[1])['aud'] == ORACLE_TOKEN


def test_each_assertion_has_a_unique_jti(private_key):
    jtis = {_b64json(pull.client_assertion(EPIC, CLIENT_ID,
                                           private_key).split('.')[1])['jti']
            for _ in range(5)}
    assert len(jtis) == 5


def test_assertion_signature_is_raw_es384_and_verifies(private_key):
    """JWS wants r||s, 48 bytes each; a DER signature verifies nowhere."""
    jwt = pull.client_assertion(EPIC, CLIENT_ID, private_key)
    signing_input, sig_b64 = jwt.rsplit('.', 1)
    sig = base64.urlsafe_b64decode(sig_b64 + '=' * (-len(sig_b64) % 4))

    assert len(sig) == 96
    der = encode_dss_signature(int.from_bytes(sig[:48], 'big'),
                               int.from_bytes(sig[48:], 'big'))
    private_key.public_key().verify(der, signing_input.encode(),
                                    ec.ECDSA(hashes.SHA384()))


def test_epic_token_request_carries_no_scope(private_key):
    session = FakeSession(EPIC)

    assert pull.get_token(session, EPIC, CLIENT_ID, private_key) == BEARER

    _, url, kwargs = session.calls[0]
    assert url == EPIC_TOKEN
    form = kwargs['data']
    assert form['grant_type'] == 'client_credentials'
    assert form['client_assertion_type'] == (
        'urn:ietf:params:oauth:client-assertion-type:jwt-bearer')
    assert 'scope' not in form


def test_oracle_token_request_lists_system_read_scopes(private_key):
    """SMART v1 `.read`, for the eight types the Oracle app registered."""
    session = FakeSession(ORACLE)

    pull.get_token(session, ORACLE, CLIENT_ID, private_key)

    scopes = session.calls[0][2]['data']['scope'].split()
    assert scopes == [
        'system/AllergyIntolerance.read', 'system/Condition.read',
        'system/Coverage.read', 'system/Immunization.read',
        'system/MedicationRequest.read', 'system/Observation.read',
        'system/Organization.read', 'system/Patient.read']
    assert set(t.split('/')[1].split('.')[0] for t in scopes) <= \
        pull.ALLOWED_TYPES


# --------------------------------------------------------------------------
# Client ids: never in the repo; local file or env


def test_client_id_from_the_local_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv('EPIC_SANDBOX_CLIENT_ID')
    cfg = tmp_path / 'ehr-sandbox.json'
    cfg.write_text(json.dumps({'epic_client_id': 'from-file-epic',
                               'oracle_client_id': 'from-file-oracle'}))
    monkeypatch.setattr(pull, 'CLIENT_CONFIG', cfg)

    assert pull.client_id('epic') == 'from-file-epic'
    assert pull.client_id('oracle') == CLIENT_ID  # env wins over the file


def test_no_client_id_is_refused_before_any_request(key_file, exports,
                                                    monkeypatch):
    monkeypatch.delenv('EPIC_SANDBOX_CLIENT_ID')
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    session, seed = FakeSession(EPIC, epic_pages()), FakeSeed()

    assert pull.main(_argv(key_file), session=session, seed_session=seed) != 0
    assert session.calls == [] and seed.calls == []


def test_a_non_es384_key_is_refused(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / 'p256.pem'
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    with pytest.raises(pull.PullError):
        pull.load_key(path)


# --------------------------------------------------------------------------
# Tenant


@pytest.mark.parametrize('tenant', [
    'sandbox-epic-ana', 'sandbox-oracle-ben-2', 'sandbox-epic-a'])
def test_a_sandbox_tenant_is_accepted(tenant):
    pull.check_tenant(tenant)


@pytest.mark.parametrize('tenant', [
    'desktop-demo', 'sandbox-epic-', 'sandbox-cerner-x', 'sandbox-epic-Ana',
    'sandbox-epic-ana\n', 'demo-sandbox-epic-x', 'sandbox-epic-a_b',
    'xsandbox-epic-a', ''])
def test_a_bad_tenant_is_refused(tenant):
    with pytest.raises(pull.PullError):
        pull.check_tenant(tenant)


def test_a_bad_tenant_is_refused_before_any_request(key_file, exports,
                                                    monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    session, seed = FakeSession(EPIC, epic_pages()), FakeSeed()

    rc = pull.main(_argv(key_file, tenant='desktop-demo'),
                   session=session, seed_session=seed)

    assert rc != 0
    assert session.calls == [] and seed.calls == []


# --------------------------------------------------------------------------
# What is kept, and how it is marked


def test_an_off_list_type_is_dropped():
    out = pull.prepare([epic_patient(), epic_practitioner(), oracle_outcome(),
                        {'resourceType': 'AuditEvent', 'id': 'a1'},
                        {'resourceType': 'Provenance', 'id': 'p1'}])
    assert [r['resourceType'] for r in out] == ['Patient']


def test_a_resource_without_an_id_is_dropped():
    out = pull.prepare([dict(epic_condition(), id=None), epic_condition()])
    assert len(out) == 1


def test_duplicates_from_includes_are_kept_once():
    out = pull.prepare([epic_medication(), epic_medication()])
    assert len(out) == 1


def _htest(res):
    return [c for slot in ('tag', 'security')
            for c in res.get('meta', {}).get(slot, [])
            if c.get('system') == V3_ACT_REASON and c.get('code') == 'HTEST']


def test_htest_on_every_entry_with_no_display(key_file, exports, monkeypatch):
    """The `_include`d Medication too, and the upstream HTEST display is not
    kept."""
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    seed = FakeSeed()

    assert pull.main(_argv(key_file), session=FakeSession(EPIC, epic_pages()),
                     seed_session=seed) == 0

    entries = seed.calls[0][1]['json']['bundle']['entry']
    types = {e['resource']['resourceType'] for e in entries}
    assert {'Patient', 'Condition', 'MedicationRequest',
            'Medication'} <= types
    for e in entries:
        assert _htest(e['resource']) == [
            {'system': V3_ACT_REASON, 'code': 'HTEST'}], e['resource']['id']
    assert 'CANARY-upstream-htest-display' not in json.dumps(entries)


def test_meta_source_and_record_source_tags_are_stripped():
    (pt,) = pull.prepare([epic_patient()])
    assert 'source' not in pt['meta']
    assert all(t.get('system') != RECORD_SOURCE_SYSTEM
               for t in pt['meta'].get('tag', []))
    assert pt['meta']['profile'] == _claim('us-core-patient')['profile']


def test_prepare_does_not_mutate_its_input():
    original = epic_patient()
    pull.prepare([original])
    assert original == epic_patient()


def test_the_written_bundle_is_a_transaction(key_file, exports, monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    pull.main(_argv(key_file), session=FakeSession(EPIC, epic_pages()),
              seed_session=FakeSeed())

    (written,) = list(exports.iterdir())
    bundle = json.loads(written.read_text())
    assert bundle['type'] == 'transaction'
    for e in bundle['entry']:
        r = e['resource']
        assert e['request'] == {'method': 'PUT',
                                'url': f"{r['resourceType']}/{r['id']}"}


# --------------------------------------------------------------------------
# Output: counts and types only


def _run_and_capture(capsys, key_file, seed, monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    rc = pull.main(_argv(key_file), session=FakeSession(EPIC, epic_pages()),
                   seed_session=seed)
    out = capsys.readouterr()
    return rc, out.out + out.err


@pytest.mark.parametrize('status', [201, 400, 403, 500])
def test_output_never_carries_the_bearer_or_step_up_token(
        capsys, key_file, exports, monkeypatch, status):
    rc, text = _run_and_capture(capsys, key_file, FakeSeed(status), monkeypatch)

    assert (rc == 0) == (status == 201)
    for secret in (BEARER, STEP_UP, 'step_up_token', SECRET):
        assert secret not in text
    assert 'CANARY' not in text
    assert 'Synthetic' not in text  # no resource content


def test_output_names_counts_and_types(capsys, key_file, exports, monkeypatch):
    rc, text = _run_and_capture(capsys, key_file, FakeSeed(), monkeypatch)

    assert rc == 0
    assert 'Patient 1' in text and 'Medication 1' in text
    assert 'Practitioner 1' in text  # reported as dropped, by type only


def test_a_token_failure_prints_no_body(capsys, key_file, exports,
                                        monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    session = FakeSession(EPIC, token_response=FakeResponse(
        400, {'error': 'invalid_client', 'error_description': 'CANARY-body'}))

    rc = pull.main(_argv(key_file), session=session, seed_session=FakeSeed())

    assert rc != 0
    assert 'CANARY' not in capsys.readouterr().out


def test_no_token_on_disk(key_file, exports, monkeypatch, tmp_path):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    pull.main(_argv(key_file), session=FakeSession(EPIC, epic_pages()),
              seed_session=FakeSeed())

    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert BEARER.encode() not in path.read_bytes()


def test_no_secret_means_no_seed(capsys, key_file, exports, monkeypatch):
    monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET', raising=False)
    session, seed = FakeSession(EPIC, epic_pages()), FakeSeed()

    rc = pull.main(_argv(key_file), session=session, seed_session=seed)

    assert rc != 0
    assert session.calls == [] and seed.calls == []


# --------------------------------------------------------------------------
# Through the real engine: seed, read, check-in export


TENANT = 'sandbox-epic-ana'


class FlaskSeedSession:
    """The script's seed POST, delivered to the Flask test client."""

    def __init__(self, client):
        self.client = client

    def post(self, url, *, json, headers, allow_redirects, timeout):
        assert url == pull.DEFAULT_ENGINE_URL + pull.SEED_PATH
        resp = self.client.post(pull.SEED_PATH, json=json, headers=headers)
        # Flask's `.json` is a property; requests' is a method.
        return FakeResponse(resp.status_code, resp.get_json(silent=True))


@pytest.fixture
def seeded(app, client, key_file, exports, monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    rc = pull.main(_argv(key_file), session=FakeSession(EPIC, epic_pages()),
                   seed_session=FlaskSeedSession(client))
    assert rc == 0
    return app


def test_seed_writes_one_audit_row_per_resource(seeded):
    from r6.models import AuditEventRecord, R6Resource

    rows = R6Resource.query.filter_by(tenant_id=TENANT).all()
    audits = AuditEventRecord.query.filter_by(
        tenant_id=TENANT, agent_id='seed', event_type='create').all()

    # Patient, Condition, MedicationRequest, Medication; no Practitioner.
    assert len(rows) == 4
    assert sorted((a.resource_type, a.resource_id) for a in audits) == \
        sorted((r.resource_type, r.id) for r in rows)


def test_the_seed_needs_the_internal_secret(app, client, key_file, exports,
                                            monkeypatch, capsys):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)

    class WrongSecret(FlaskSeedSession):
        def post(self, url, **kw):
            kw['headers'] = dict(kw['headers'], **{'X-Internal-Secret': 'no'})
            return super().post(url, **kw)

    rc = pull.main(_argv(key_file), session=FakeSession(EPIC, epic_pages()),
                   seed_session=WrongSecret(client))

    assert rc != 0
    assert '403' in capsys.readouterr().out
    from r6.models import R6Resource
    assert R6Resource.query.filter_by(tenant_id=TENANT).count() == 0


def test_a_reference_display_naming_a_practitioner_is_absent_on_read(
        seeded, client):
    from r6.stepup import generate_step_up_token

    resp = client.get('/r6/fhir/MedicationRequest/eSynth.MedReq-01',
                      headers={'X-Tenant-Id': TENANT,
                               'X-Step-Up-Token':
                                   generate_step_up_token(TENANT)})

    assert resp.status_code == 200, resp.status_code
    body = resp.get_data(as_text=True)
    assert PRACTITIONER_CANARY not in body
    assert SUBJECT_CANARY not in body
    assert 'CANARY' not in body
    # Stored as received: redaction is at the exit, not on ingest.
    from r6.models import R6Resource
    stored = R6Resource.query.filter_by(
        tenant_id=TENANT, resource_type='MedicationRequest').one()
    assert PRACTITIONER_CANARY in stored.resource_json


def test_the_checkin_export_keeps_the_patient_name(seeded, client):
    types = ['Patient', 'Condition']
    mint = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': TENANT, 'purpose': 'checkin',
                             'resource_types': types},
                       headers={'X-Tenant-Id': TENANT,
                                'X-Internal-Secret': SECRET})
    assert mint.status_code == 200, mint.get_data(as_text=True)

    resp = client.post('/r6/fhir/$share-bundle',
                       json={'profile': 'checkin', 'resource_types': types,
                             'patient_id': 'eSynth.Pt-0001'},
                       headers={'X-Tenant-Id': TENANT,
                                'X-Step-Up-Token': mint.get_json()['token']})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    (pt,) = [e['resource'] for e in resp.get_json()['entry']
             if e['resource']['resourceType'] == 'Patient']
    assert pt['name'] == [{'family': 'Synthetic', 'given': ['Ana']}]
    assert pt['birthDate'] == '1987-09-12'
    assert _htest(pt)  # the test-data marker crosses to the clinic


def test_the_signing_kid_is_published():
    """The vendors verify against deploy/ehr-sandbox-jwks.json by kid."""
    jwks = json.loads((pull.ROOT / 'deploy'
                       / 'ehr-sandbox-jwks.json').read_text())
    assert pull.KID in {k['kid'] for k in jwks['keys']}


# --------------------------------------------------------------------------
# Security review fix round (2026-10-26)


def test_fhir_reads_get_the_long_search_timeout(monkeypatch, key_file,
                                                exports):
    """Oracle's problem-list search has taken more than 30 s live."""
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    session = FakeSession(EPIC, epic_pages())

    assert pull.main(_argv(key_file), session=session,
                     seed_session=FakeSeed()) == 0

    gets = [kw for m, _, kw in session.calls if m == 'GET']
    assert gets and pull.SEARCH_TIMEOUT >= 120
    assert all(kw['timeout'] == pull.SEARCH_TIMEOUT for kw in gets)


def _run_pages(capsys, key_file, monkeypatch, pages, *extra):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    seed = FakeSeed()
    rc = pull.main(_argv(key_file) + list(extra),
                   session=FakeSession(EPIC, pages), seed_session=seed)
    out = capsys.readouterr()
    return rc, out.out + out.err, seed


def test_the_page_cap_prints_a_truncation_notice(capsys, key_file, exports,
                                                 monkeypatch):
    monkeypatch.setattr(pull, 'MAX_PAGES', 3)
    nxt = f'{EPIC_FHIR}/Condition?page=again'
    looping = _bundle(epic_condition(), next_url=nxt)
    pages = dict(epic_pages(), Condition=looping, **{nxt: looping})

    rc, text, seed = _run_pages(capsys, key_file, monkeypatch, pages)

    # A truncated pull still seeds and exits 0; the notice says so.
    assert rc == 0 and seed.calls
    assert 'epic: Condition truncated at 3' in text
    assert 'CANARY' not in text


def test_the_entry_cap_prints_a_truncation_notice(capsys, key_file, exports,
                                                  monkeypatch):
    monkeypatch.setattr(pull, 'MAX_ENTRIES', 2)
    pages = dict(epic_pages(), Condition=_bundle(
        *(dict(epic_condition(), id=f'eSynth.Cond-0{i}') for i in range(3))))

    rc, text, _ = _run_pages(capsys, key_file, monkeypatch, pages)

    assert rc == 0
    assert 'epic: Condition truncated at 2' in text
    assert 'Condition 2' in text


def test_a_complete_search_prints_no_truncation_notice(
        capsys, key_file, exports, monkeypatch):
    monkeypatch.setattr(pull, 'MAX_ENTRIES', 2)
    pages = {'Patient/eSynth.Pt-0001': epic_patient(),
             'Condition': _bundle(*(dict(epic_condition(),
                                         id=f'eSynth.Cond-0{i}')
                                    for i in range(2)))}

    rc, text, _ = _run_pages(capsys, key_file, monkeypatch, pages)

    assert rc == 0
    assert 'Condition 2' in text
    assert 'truncated' not in text


def test_dry_run_writes_the_export_and_does_not_seed(
        capsys, key_file, exports, monkeypatch):
    monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET', raising=False)
    seed = FakeSeed()

    rc = pull.main(_argv(key_file) + ['--dry-run'],
                   session=FakeSession(EPIC, epic_pages()), seed_session=seed)

    assert rc == 0
    assert seed.calls == []
    assert len(list(exports.iterdir())) == 1
    assert 'not seeded' in capsys.readouterr().out


@pytest.mark.parametrize('url', [
    'http://app.healthclaw.io',
    'http://localhost.evil.example',
    'http://127.0.0.1.evil.example:5099',
    'ftp://localhost',
    'app.healthclaw.io',
    'https://',
])
def test_a_non_https_engine_url_is_refused_before_any_request(
        capsys, key_file, exports, monkeypatch, url):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    session, seed = FakeSession(EPIC, epic_pages()), FakeSeed()

    rc = pull.main(_argv(key_file) + ['--engine-url', url],
                   session=session, seed_session=seed)

    assert rc == 1
    assert session.calls == [] and seed.calls == []
    assert SECRET not in capsys.readouterr().out


@pytest.mark.parametrize('url', [
    'https://app.healthclaw.io', 'http://localhost:5099',
    'http://127.0.0.1:5099', 'HTTP://LOCALHOST:5099',
])
def test_https_or_local_http_engine_urls_are_accepted(url):
    pull.check_engine_url(url)


@pytest.mark.parametrize('patient', ['..', '../..', '.'])
def test_a_dot_patient_id_never_reaches_the_vendor(
        key_file, exports, monkeypatch, patient):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    session, seed = FakeSession(EPIC, epic_pages()), FakeSeed()

    rc = pull.main(_argv(key_file, patient=patient),
                   session=session, seed_session=seed)

    assert rc == 1
    assert [m for m, _, _ in session.calls if m == 'GET'] == []
    assert seed.calls == []


def test_the_patient_id_is_url_encoded():
    session = FakeSession(EPIC)
    pull.fetch_patient(session, EPIC, BEARER, 'a b?c#d')
    assert session.calls[0][1] == f'{EPIC_FHIR}/Patient/a%20b%3Fc%23d'


def test_a_malformed_meta_tag_is_treated_as_untagged():
    for tag in ('x', 7, None):
        (res,) = pull.prepare([{'resourceType': 'Condition', 'id': 'c1',
                                'meta': {'tag': tag}}])
        assert res['meta']['tag'] == [
            {'system': V3_ACT_REASON, 'code': 'HTEST'}]


def test_dropped_types_are_labelled_only_when_type_shaped():
    _, dropped = pull.prepare_counted([
        {'resourceType': 'Practitioner', 'id': 'a'},
        {'resourceType': 'CANARY Name\x1b[2J', 'id': 'b'},
        {'resourceType': 'X' * 65, 'id': 'c'},
        {'resourceType': 7, 'id': 'd'},
    ])
    assert dropped == {'Practitioner': 1, 'other': 3}


def test_a_seed_answer_without_a_count_prints_unknown(
        capsys, key_file, exports, monkeypatch):
    class NoCount(FakeSeed):
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse(201, {'step_up_token': STEP_UP})

    rc, text = _run_and_capture(capsys, key_file, NoCount(), monkeypatch)

    assert rc == 0
    assert 'created unknown' in text
    assert 'created 0' not in text


def test_a_code_text_is_absent_on_read(seeded, client):
    """The runbook says the engine redacts `display` and `text` on the way
    out; this pins the `text` half on the `_include`d Medication."""
    from r6.stepup import generate_step_up_token

    resp = client.get('/r6/fhir/Medication/eSynth.Med-01',
                      headers={'X-Tenant-Id': TENANT,
                               'X-Step-Up-Token':
                                   generate_step_up_token(TENANT)})

    assert resp.status_code == 200, resp.status_code
    assert 'CANARY-med-text' not in resp.get_data(as_text=True)
