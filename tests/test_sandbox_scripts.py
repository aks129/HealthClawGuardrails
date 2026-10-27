"""The sandbox test-data scripts: scripts/sandbox_test_patient.py and
scripts/ktc_shl_link.py.

No network here: the vendor sandbox and the KTC harness are faked. What is
pinned is the part that keeps real records off a third-party host (the HTEST
gate), the file a receiver decrypts (the JWE), and the shape of the bundle
the puller builds.
"""

import importlib.util
import json
import time
from pathlib import Path

import pytest

from r6 import shl

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / 'scripts' / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


link = _load('ktc_shl_link')
pull = _load('sandbox_test_patient')

V3 = 'http://terminology.hl7.org/CodeSystem/v3-ActReason'
HTEST = {'system': V3, 'code': 'HTEST', 'display': 'test health data'}
VENDOR_TEXT = 'Vendor display that must survive the pull verbatim'


def _res(rtype, rid, slot='tag'):
    res = {'resourceType': rtype, 'id': rid}
    if slot:
        res['meta'] = {slot: [dict(HTEST)]}
    return res


def _bundle(*resources):
    return {'resourceType': 'Bundle', 'type': 'collection',
            'entry': [{'resource': r} for r in resources]}


class FakeHarness:
    def __init__(self, link_id='abcDEF123_-xyz'):
        self.link_id = link_id
        self.posted = []

    def __call__(self, jwe, url=link.HARNESS_URL):
        self.posted.append((url, jwe))
        return self.link_id


# --- ktc_shl_link: the HTEST gate ---------------------------------------------

@pytest.mark.parametrize('bundle', [
    _bundle(_res('Patient', 'p1'), _res('Condition', 'c1', slot=None)),
    _bundle(_res('Patient', 'p1'),
            {'resourceType': 'Condition', 'id': 'c1',
             'meta': {'tag': [{'system': 'http://example.org', 'code': 'HTEST'}]}}),
    _bundle(_res('Patient', 'p1'),
            {'resourceType': 'Condition', 'id': 'c1',
             'meta': {'tag': [{'system': V3, 'code': 'HTEST2'}]}}),
    _bundle(),
    {'resourceType': 'Bundle', 'type': 'collection'},
    {'resourceType': 'Patient', 'id': 'p1', 'meta': {'tag': [dict(HTEST)]}},
    {'resourceType': 'Bundle', 'entry': [{'fullUrl': 'urn:uuid:x'}]},
    [],
])
def test_a_bundle_not_all_htest_is_refused_and_nothing_is_posted(bundle):
    """MUTATION: drop the per-entry check, or let an empty bundle pass as
    vacuously all-tagged -> red."""
    harness = FakeHarness()
    with pytest.raises(link.NotTestData):
        link.make_link(bundle, post=harness)
    assert harness.posted == []


def test_the_marker_counts_on_meta_security_too():
    harness = FakeHarness()
    shlink = link.make_link(_bundle(_res('Patient', 'p1', slot='security')),
                            post=harness)
    assert shlink.startswith('shlink:/')


def test_main_refuses_an_untagged_file_with_exit_2(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'b.json'
    path.write_text(json.dumps(_bundle(_res('Patient', 'p1', slot=None))))
    harness = FakeHarness()
    monkeypatch.setattr(link, 'post_jwe', harness)
    assert link.main([str(path)]) == 2
    assert harness.posted == []
    assert 'shlink:/' not in capsys.readouterr().out


# --- ktc_shl_link: the file a receiver reads -----------------------------------

def test_the_jwe_round_trips_to_the_bundle_under_the_shlink_key():
    """The file the harness stores decrypts, under the key in the shlink, to
    exactly the bundle, the way r6/shl.py (a receiver) reads it."""
    bundle = _bundle(_res('Patient', 'p1'), _res('Condition', 'c1'))
    harness = FakeHarness()
    shlink = link.make_link(bundle, post=harness)
    (_, jwe), = harness.posted
    payload = shl.parse_shlink(shlink)
    plain = shl.decrypt(shl.b64url_decode(payload['key']), jwe)
    assert json.loads(plain) == bundle


def test_the_jwe_is_compact_dir_a256gcm_fhir_json():
    harness = FakeHarness()
    link.make_link(_bundle(_res('Patient', 'p1')), post=harness)
    (_, jwe), = harness.posted
    parts = jwe.split('.')
    assert len(parts) == 5 and parts[1] == ''
    assert json.loads(shl.b64url_decode(parts[0])) == {
        'alg': 'dir', 'enc': 'A256GCM', 'cty': 'application/fhir+json'}
    assert len(shl.b64url_decode(parts[2])) == 12
    assert len(shl.b64url_decode(parts[4])) == 16


def test_the_aad_is_the_header_segment():
    """MUTATION: encrypt with no AAD -> the receiver's decrypt fails -> red."""
    harness = FakeHarness()
    shlink = link.make_link(_bundle(_res('Patient', 'p1')), post=harness)
    (_, jwe), = harness.posted
    key = shl.b64url_decode(shl.parse_shlink(shlink)['key'])
    head, _, iv, ct, tag = jwe.split('.')
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    sealed = shl.b64url_decode(ct) + shl.b64url_decode(tag)
    AESGCM(key).decrypt(shl.b64url_decode(iv), sealed, head.encode('ascii'))
    with pytest.raises(Exception):
        AESGCM(key).decrypt(shl.b64url_decode(iv), sealed, None)


def test_each_link_has_its_own_key():
    harness = FakeHarness()
    a = link.make_link(_bundle(_res('Patient', 'p1')), post=harness)
    b = link.make_link(_bundle(_res('Patient', 'p1')), post=harness)
    assert shl.parse_shlink(a)['key'] != shl.parse_shlink(b)['key']


def test_the_shlink_is_u_flag_on_the_harness_with_a_label_and_at_most_an_hour():
    harness = FakeHarness('the-link-id')
    now = int(time.time())
    p = shl.parse_shlink(link.make_link(
        _bundle(_res('Patient', 'p1')), post=harness, now=now))
    assert p['url'] == link.HARNESS_URL + '/the-link-id'
    assert p['flag'] == 'U'
    assert p['exp'] == now + 3600
    assert p['label'] == link.DEFAULT_LABEL
    assert harness.posted[0][0] == link.HARNESS_URL


@pytest.mark.parametrize('ttl', [3601, 86400, 0, -5])
def test_a_ttl_outside_one_hour_is_refused(ttl):
    harness = FakeHarness()
    with pytest.raises(ValueError):
        link.make_link(_bundle(_res('Patient', 'p1')), post=harness, ttl=ttl)
    assert harness.posted == []


def test_a_shorter_ttl_and_own_label_are_honoured():
    now = 1_800_000_000
    p = shl.parse_shlink(link.make_link(
        _bundle(_res('Patient', 'p1')), post=FakeHarness(), now=now,
        ttl=600, label='Connectathon run 3'))
    assert p['exp'] == now + 600 and p['label'] == 'Connectathon run 3'


@pytest.mark.parametrize('link_id', ['', 'a/b', '../x', 'a' * 200, None, 7])
def test_a_harness_answer_that_is_not_a_plain_id_is_refused(link_id):
    with pytest.raises(ValueError):
        link.make_link(_bundle(_res('Patient', 'p1')),
                       post=FakeHarness(link_id))


# --- sandbox_test_patient ------------------------------------------------------

def test_tagging_adds_htest_once_and_keeps_the_vendor_text_verbatim():
    res = {'resourceType': 'Condition', 'id': 'c1',
           'meta': {'versionId': '3', 'tag': [{'system': 'x', 'code': 'y'}]},
           'code': {'coding': [{'code': '1', 'display': VENDOR_TEXT}],
                    'text': VENDOR_TEXT},
           'subject': {'reference': 'Patient/p1', 'display': VENDOR_TEXT}}
    before = json.loads(json.dumps(res))
    pull.tag_test_data(res)
    pull.tag_test_data(res)
    assert res['meta']['tag'] == before['meta']['tag'] + [HTEST]
    del res['meta'], before['meta']
    assert res == before


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


class FakeSession:
    """Answers each GET from a queue keyed by resource type."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None,
            allow_redirects=True):
        assert allow_redirects is False
        self.calls.append((url, dict(params or {})))
        rtype = url.rstrip('/').rsplit('/', 2)[-2 if '/Patient/' in url else -1]
        return self.answers[rtype].pop(0)


def _search(rtype, n, extra=()):
    entries = [{'resource': {'resourceType': rtype, 'id': f'{rtype}-{i}',
                             'code': {'text': VENDOR_TEXT}}}
               for i in range(n)]
    entries += [{'resource': r} for r in extra]
    return FakeResponse(200, {'resourceType': 'Bundle', 'type': 'searchset',
                              'entry': entries})


def _answers(**over):
    base = {
        'Patient': [FakeResponse(200, {'resourceType': 'Patient', 'id': 'p1'})],
        'Condition': [_search('Condition', 2)],
        'AllergyIntolerance': [_search('AllergyIntolerance', 1)],
        'MedicationRequest': [_search('MedicationRequest', 1)],
        'Immunization': [_search('Immunization', 1)],
        'Coverage': [_search('Coverage', 0)],
        'Observation': [_search('Observation', 3)],
    }
    base.update(over)
    return base


def test_the_bundle_is_a_tagged_collection_of_the_check_in_types():
    session = FakeSession(_answers())
    bundle, counts = pull.build_bundle('oracle', 'p1', session=session)
    assert bundle['resourceType'] == 'Bundle' and bundle['type'] == 'collection'
    types = [e['resource']['resourceType'] for e in bundle['entry']]
    assert types.count('Patient') == 1 and 'Observation' not in types
    assert counts == {'Patient': 1, 'Condition': 2, 'AllergyIntolerance': 1,
                      'MedicationRequest': 1, 'Immunization': 1, 'Coverage': 0}
    # Every entry clears the link script's gate, and keeps the vendor text.
    link.check_test_only(bundle)
    assert all(e['resource']['code']['text'] == VENDOR_TEXT
               for e in bundle['entry'] if 'code' in e['resource'])
    assert all(e['fullUrl'].startswith(pull.SOURCES['oracle']['base'] + '/')
               for e in bundle['entry'])


def test_oracle_condition_search_is_bounded_and_problem_list_only():
    session = FakeSession(_answers())
    pull.build_bundle('oracle', 'p1', session=session)
    params = {u.rsplit('/', 1)[-1]: p for u, p in session.calls}
    assert params['Condition'] == {'patient': 'p1',
                                   'category': 'problem-list-item',
                                   '_count': '20'}
    assert all(p.get('_count') == '20' for u, p in session.calls
               if not u.rsplit('/', 2)[-2] == 'Patient')


def test_labs_are_opt_in():
    session = FakeSession(_answers())
    bundle, counts = pull.build_bundle('oracle', 'p1', session=session,
                                       labs=True)
    assert counts['Observation'] == 3
    obs = [p for u, p in session.calls if u.endswith('/Observation')]
    assert obs == [{'patient': 'p1', 'category': 'laboratory', '_count': '20'}]


def test_a_504_is_retried_once_then_the_type_is_left_out():
    session = FakeSession(_answers(
        Immunization=[FakeResponse(504), FakeResponse(504)],
        Condition=[FakeResponse(504), _search('Condition', 2)]))
    bundle, counts = pull.build_bundle('oracle', 'p1', session=session)
    assert counts['Condition'] == 2
    assert counts['Immunization'] is None
    assert sum(u.endswith('/Immunization') for u, _ in session.calls) == 2


def test_a_vendor_that_ignores_count_is_cut_to_twenty_and_strays_dropped():
    """Oracle answered 1503 Conditions to `_count=20`; ingest takes 500."""
    stray = {'resourceType': 'OperationOutcome', 'id': 'oo'}
    session = FakeSession(_answers(
        Condition=[_search('Condition', 45, extra=[stray])]))
    bundle, counts = pull.build_bundle('oracle', 'p1', session=session)
    assert counts['Condition'] == 20
    assert 'OperationOutcome' not in {
        e['resource']['resourceType'] for e in bundle['entry']}


class NotJson(FakeResponse):
    def json(self):
        raise ValueError('not JSON')


def test_a_200_that_is_not_json_leaves_the_type_out():
    session = FakeSession(_answers(Coverage=[NotJson(200)]))
    _, counts = pull.build_bundle('oracle', 'p1', session=session)
    assert counts['Coverage'] is None


def test_no_patient_no_bundle():
    session = FakeSession(_answers(Patient=[FakeResponse(404), ]))
    with pytest.raises(pull.SandboxError):
        pull.build_bundle('oracle', 'p1', session=session)


# --- Security review fix round (2026-10-26) -------------------------------------

def test_the_file_sent_is_rebuilt_from_the_checked_resources():
    """The harness gets exactly what the gate checked: a puller bundle's
    fullUrl, meta and timestamp stay behind. MUTATION: encrypt `bundle`
    instead of the rebuilt one -> red."""
    built, _ = pull.build_bundle('oracle', 'p1',
                                 session=FakeSession(_answers()))
    harness = FakeHarness()
    shlink = link.make_link(built, post=harness)
    (_, jwe), = harness.posted
    key = shl.b64url_decode(shl.parse_shlink(shlink)['key'])
    assert json.loads(shl.decrypt(key, jwe)) == {
        'resourceType': 'Bundle', 'type': 'collection',
        'entry': [{'resource': e['resource']} for e in built['entry']]}


@pytest.mark.parametrize('bundle', [
    {'resourceType': 'Bundle', 'type': 'collection', 'link': [],
     'entry': [{'resource': _res('Patient', 'p1')}]},
    _bundle(dict(_res('Patient', 'p1'), extension=[
        {'url': 'x', 'valueReference': {'resourceType': 'Patient'}}])),
])
def test_an_unchecked_key_or_a_nested_resource_is_refused(bundle):
    harness = FakeHarness()
    with pytest.raises(link.NotTestData):
        link.make_link(bundle, post=harness)
    assert harness.posted == []


class _RecordingSession:
    instances = []

    def __init__(self):
        self.trust_env = True
        self.kwargs = None
        _RecordingSession.instances.append(self)

    def post(self, url, **kwargs):
        self.kwargs = kwargs
        return FakeResponse(302, {'id': 'abc'})


def test_the_harness_post_follows_no_redirect_and_ignores_env(monkeypatch):
    import requests
    _RecordingSession.instances = []
    monkeypatch.setattr(requests, 'Session', _RecordingSession)

    with pytest.raises(requests.HTTPError):
        link.post_jwe('a.b.c.d.e')

    (s,) = _RecordingSession.instances
    assert s.trust_env is False
    assert s.kwargs['allow_redirects'] is False


def test_the_ingest_post_follows_no_redirect_and_ignores_env(monkeypatch):
    import requests
    _RecordingSession.instances = []
    monkeypatch.setattr(requests, 'Session', _RecordingSession)

    status, _ = pull.ingest({'resourceType': 'Bundle'}, 'https://e.example',
                            'sandbox-smart-x', 'synthetic-secret')

    (s,) = _RecordingSession.instances
    assert status == 302
    assert s.trust_env is False
    assert s.kwargs['allow_redirects'] is False


def test_ingest_itself_refuses_a_plain_http_engine(monkeypatch):
    import requests
    _RecordingSession.instances = []
    monkeypatch.setattr(requests, 'Session', _RecordingSession)

    with pytest.raises(ValueError):
        pull.ingest({'resourceType': 'Bundle'}, 'http://e.example',
                    'sandbox-smart-x', 'synthetic-secret')
    assert _RecordingSession.instances == []


def test_the_sandbox_session_ignores_env():
    assert pull.new_session().trust_env is False


def _main_ingest(monkeypatch, tmp_path, tenant, engine):
    built, sent = [], []
    monkeypatch.setattr(pull, 'build_bundle', lambda *a, **k: built.append(1)
                        or (_bundle(_res('Patient', 'p1')), {'Patient': 1}))
    monkeypatch.setattr(pull, 'ingest',
                        lambda *a, **k: sent.append(a) or (200, {}))
    try:
        rc = pull.main(['--source', 'smart', '--out', str(tmp_path / 'b.json'),
                        '--ingest', engine, '--tenant', tenant])
    except SystemExit as exc:
        rc = exc.code
    return rc, built, sent


@pytest.mark.parametrize('engine', [
    'http://app.healthclaw.io', 'http://localhost.evil.example',
    'ftp://localhost', 'localhost:5099'])
def test_ingest_refuses_a_plain_http_engine_before_any_request(
        monkeypatch, tmp_path, engine):
    rc, built, sent = _main_ingest(monkeypatch, tmp_path, 'sandbox-smart-x',
                                   engine)
    assert rc != 0 and built == [] and sent == []


@pytest.mark.parametrize('engine,tenant', [
    ('http://localhost:5099', 'sandbox-smart-test'),
    ('http://127.0.0.1:5099', 'sandbox-oracle-ben'),
    ('https://app.healthclaw.io', 'sandbox-smart-x'),
])
def test_ingest_takes_a_sandbox_tenant_and_a_safe_engine(
        monkeypatch, tmp_path, engine, tenant):
    rc, built, sent = _main_ingest(monkeypatch, tmp_path, tenant, engine)
    assert rc == 0 and len(sent) == 1
    assert sent[0][2] == tenant


@pytest.mark.parametrize('tenant', ['sandbox-smart', 'sandbox-epic-x',
                                    'sandbox-smart-x\n', 'Sandbox-smart-x'])
def test_ingest_refuses_a_tenant_off_the_sandbox_pattern(
        monkeypatch, tmp_path, tenant):
    rc, built, sent = _main_ingest(monkeypatch, tmp_path, tenant,
                                   'http://localhost:5099')
    assert rc != 0 and built == [] and sent == []


def _oracle_allergy_with_contained_role():
    """Oracle's open sandbox serves AllergyIntolerance with an untagged
    contained PractitionerRole, referenced as `#id`."""
    return {
        'resourceType': 'AllergyIntolerance', 'id': 'a1',
        'contained': [{'resourceType': 'PractitionerRole', 'id': 'pr1',
                       'practitioner': {'display': 'CANARY-Contained-Name'}}],
        'patient': {'reference': 'Patient/p1'},
        'code': {'text': VENDOR_TEXT},
        'recorder': {'reference': '#pr1'},
        'asserter': {'reference': '#pr1', 'display': VENDOR_TEXT},
        'note': [{'authorReference': {'reference': '#pr1'}}],
    }


def _oracle_answers():
    return _answers(AllergyIntolerance=[FakeResponse(200, {
        'resourceType': 'Bundle', 'type': 'searchset',
        'entry': [{'resource': _oracle_allergy_with_contained_role()}]})])


def test_contained_resources_and_their_references_are_dropped():
    dropped = pull.Counter()
    bundle, _ = pull.build_bundle('oracle', 'p1',
                                  session=FakeSession(_oracle_answers()),
                                  dropped_contained=dropped)

    (allergy,) = [e['resource'] for e in bundle['entry']
                  if e['resource']['resourceType'] == 'AllergyIntolerance']
    assert 'contained' not in allergy
    assert 'recorder' not in allergy      # left empty, so removed
    assert 'note' not in allergy          # its only item emptied
    assert allergy['asserter'] == {'display': VENDOR_TEXT}  # vendor text kept
    assert allergy['patient'] == {'reference': 'Patient/p1'}
    assert dropped == {'PractitionerRole': 1}
    assert 'CANARY-Contained-Name' not in json.dumps(bundle)


def test_an_oracle_shaped_pull_passes_the_link_gate_end_to_end():
    """MUTATION: stop dropping `contained` -> the gate refuses -> red."""
    bundle, _ = pull.build_bundle('oracle', 'p1',
                                  session=FakeSession(_oracle_answers()))
    link.check_test_only(bundle)
    harness = FakeHarness()
    assert link.make_link(bundle, post=harness).startswith('shlink:/')
    assert len(harness.posted) == 1


def test_main_prints_contained_drops_as_counts_only(monkeypatch, tmp_path,
                                                   capsys):
    monkeypatch.setattr(pull, 'new_session',
                        lambda: FakeSession(_oracle_answers()))

    rc = pull.main(['--source', 'oracle', '--out', str(tmp_path / 'b.json')])

    out = capsys.readouterr().out
    assert rc == 0
    assert 'contained PractitionerRole dropped: 1' in out
    assert 'CANARY' not in out and VENDOR_TEXT not in out


def test_a_reference_to_a_kept_resource_is_untouched():
    res = {'resourceType': 'AllergyIntolerance', 'id': 'a2',
           'contained': [{'resourceType': 'Practitioner', 'id': 'x'}],
           'recorder': {'reference': 'Practitioner/x'},
           'asserter': {'reference': '#other'}}
    pull.drop_contained(res, pull.Counter())
    assert res == {'resourceType': 'AllergyIntolerance', 'id': 'a2',
                   'recorder': {'reference': 'Practitioner/x'},
                   'asserter': {'reference': '#other'}}
