"""SMART Health Link issuing: r6/shl.py, r6/shl_links.py and the ktc
export's `deliver: link` (SHL design PR 3 shape).

The format checks read a file the way RX Genie's prototype/src/shl.rs does
(the receiver CareAgents hands links to): five segments with an empty
second, a header of alg dir / enc A256GCM and no zip, ciphertext and tag
joined, the header segment as AAD, base64url without padding throughout,
and a payload of url, a 32-byte key, flag U, exp and label. The two
RX_GENIE_* vectors were produced by shl.rs `create()` itself, so the round
trip is checked against the other implementation, not only against ours.
"""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from models import db
from r6 import ktc, shl, shl_links
from r6.models import AuditEventRecord
from tests import checkin_fixtures as f

ALL = ['AllergyIntolerance', 'Condition', 'Coverage', 'Immunization',
       'MedicationRequest', 'Patient']
V3_ACT_REASON = 'http://terminology.hl7.org/CodeSystem/v3-ActReason'
BASE = 'https://engine.example.test'

# --- vectors from RX Genie (prototype/src/shl.rs create(), 2026-10-22) -------
#: shl::create("https://rxgenie.us/shl", {"resourceType":"Bundle",
#: "type":"collection"}, "test"): a JWE and its shlink, random key and IV.
RX_GENIE_SHLINK = (
    'shlink:/eyJleHAiOjE3OTI2OTgzOTEsImZsYWciOiJVIiwia2V5IjoiLUx3Nl9lWHU5SUh'
    'RUExnQ1U1NDlzSmdLVHJNMFBlLUw4N0g0QU1VajFBRSIsImxhYmVsIjoidGVzdCIsInVybCI'
    '6Imh0dHBzOi8vcnhnZW5pZS51cy9zaGwvX2kwdm44QkVRZjlPMzIzOXhCbDh6c3U1bWdONW'
    'dKLWc1cl9fVGJIM1RXYyJ9')
RX_GENIE_JWE = (
    'eyJhbGciOiJkaXIiLCJjdHkiOiJhcHBsaWNhdGlvbi9maGlyK2pzb24iLCJlbmMiOiJBMjU2'
    'R0NNIn0..g7CV1DYMMIQMU3Fj.lwdfBV2hfb9fvwvKOcMlKV8MKsexDNanlXCgMCW6T3hacV'
    'q3y0pv99gZ7G20.a7GkpA-XrXjlCi_JQkSOcQ')
RX_GENIE_CONTENT = {'resourceType': 'Bundle', 'type': 'collection'}


def _b64d(text):
    """Strict base64url without padding, as shl.rs decodes a JWE segment."""
    assert '=' not in text and '+' not in text and '/' not in text
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def rx_genie_decrypt(key, jwe):
    """prototype/src/shl.rs decrypt(), line for line."""
    parts = jwe.strip().split('.')
    assert len(parts) == 5
    header = json.loads(_b64d(parts[0]))
    assert header['alg'] == 'dir' and header['enc'] == 'A256GCM'
    iv = _b64d(parts[2])
    ct = _b64d(parts[3]) + _b64d(parts[4])
    return AESGCM(key).decrypt(iv, ct, parts[0].encode('ascii'))


def rx_genie_parse(shlink):
    """prototype/src/shl.rs parse(): the checks it makes."""
    payload = json.loads(_b64d(shlink.rsplit('shlink:/', 1)[1]))
    if 'exp' in payload:
        assert isinstance(payload['exp'], int)
        assert payload['exp'] >= int(time.time())
    assert 'P' not in payload.get('flag', '')
    assert 'U' in payload.get('flag', '')
    key = _b64d(payload['key'])
    assert len(key) == 32
    return payload, key


# --- crypto and payload -------------------------------------------------------

def test_round_trip_in_the_receivers_format():
    key = shl.new_key()
    content = json.dumps({'resourceType': 'Bundle', 'type': 'collection'})
    jwe = shl.encrypt(key, content.encode())
    parts = jwe.split('.')
    assert len(parts) == 5 and parts[1] == ''
    assert json.loads(_b64d(parts[0])) == {
        'alg': 'dir', 'enc': 'A256GCM', 'cty': 'application/fhir+json'}
    assert len(_b64d(parts[2])) == 12 and len(_b64d(parts[4])) == 16
    assert rx_genie_decrypt(key, jwe).decode() == content
    assert shl.decrypt(key, jwe).decode() == content


def test_no_zip_header_ever():
    """RX Genie does not inflate. MUTATION: add zip DEF to HEADER -> red."""
    header = json.loads(_b64d(shl.encrypt(shl.new_key(), b'x').split('.')[0]))
    assert 'zip' not in header


def test_each_file_gets_a_fresh_iv():
    key = shl.new_key()
    assert shl.encrypt(key, b'same').split('.')[2] != \
        shl.encrypt(key, b'same').split('.')[2]


def test_a_fixed_key_and_iv_give_a_fixed_file():
    """Known-answer: AES-256-GCM with AAD = the header segment."""
    key, iv = bytes(range(32)), bytes(range(12))
    jwe = shl.encrypt(key, b'{"a":1}', iv=iv)
    protected = jwe.split('.')[0]
    sealed = AESGCM(key).encrypt(iv, b'{"a":1}', protected.encode())
    assert jwe == (f'{protected}..{shl.b64url(iv)}.'
                   f'{shl.b64url(sealed[:-16])}.{shl.b64url(sealed[-16:])}')


def test_wrong_key_and_tampering_fail():
    key = shl.new_key()
    jwe = shl.encrypt(key, b'{"a":1}')
    with pytest.raises(ValueError):
        shl.decrypt(shl.new_key(), jwe)
    parts = jwe.split('.')
    parts[0] = shl.b64url(json.dumps(
        {'alg': 'dir', 'enc': 'A256GCM'}).encode())
    with pytest.raises(ValueError):
        shl.decrypt(key, '.'.join(parts))


def test_the_shlink_payload_is_what_rx_genie_parses():
    key = shl.new_key()
    exp = int(time.time()) + 3600
    link = shl.build_shlink(f'{BASE}/shl/abc', key, exp, 'Shared health records')
    assert link.startswith('shlink:/')
    payload, got_key = rx_genie_parse(link)
    assert got_key == key
    assert payload == {'url': f'{BASE}/shl/abc', 'key': shl.b64url(key),
                       'flag': 'U', 'exp': exp,
                       'label': 'Shared health records'}
    assert shl.parse_shlink(link) == payload


@pytest.mark.parametrize('url,label', [
    ('http://engine.example.test/shl/x', None),
    ('https://e.test/' + 'x' * 120, None),
    (f'{BASE}/shl/x', 'x' * 81),
])
def test_the_payload_limits_hold(url, label):
    with pytest.raises(ValueError):
        shl.build_shlink(url, shl.new_key(), time.time() + 60, label)


def test_a_file_made_by_rx_genie_decrypts_here():
    """The other direction (a link made here, read by shl.rs parse and
    decrypt) was checked once with a throwaway cargo test on 2026-10-22."""
    payload = shl.parse_shlink(RX_GENIE_SHLINK)
    assert payload['flag'] == 'U' and payload['label'] == 'test'
    key = shl.b64url_decode(payload['key'])
    assert json.loads(shl.decrypt(key, RX_GENIE_JWE)) == RX_GENIE_CONTENT
    assert json.loads(rx_genie_decrypt(key, RX_GENIE_JWE)) == RX_GENIE_CONTENT


# --- the export, delivered as a link -----------------------------------------

@pytest.fixture(autouse=True)
def _engine_env(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', f.MINT_SECRET)
    monkeypatch.setenv('KTC_EXPORT_ENABLED', 'true')
    monkeypatch.setenv('PUBLIC_BASE_URL', BASE)
    monkeypatch.delenv('KTC_LINK_REAL_RECORDS', raising=False)
    monkeypatch.delenv('SHL_LINK_TTL_SECONDS', raising=False)
    monkeypatch.delenv('SHL_LINK_MAX_USES', raising=False)


def _htest(res):
    res.setdefault('meta', {})['tag'] = [
        {'system': V3_ACT_REASON, 'code': 'HTEST'}]
    return res


def _records(test_data=True):
    rows = [f.patient(), f.condition(), f.nka_allergy(), f.coverage(),
            f.organization()]
    return [_htest(r) for r in rows] if test_data else rows


def ktc_token(client, types=ALL):
    resp = client.post(
        '/r6/fhir/internal/step-up-token',
        json={'tenant_id': f.TENANT, 'purpose': 'ktc',
              'resource_types': types},
        headers={'X-Tenant-Id': f.TENANT, 'X-Internal-Secret': f.MINT_SECRET})
    assert resp.status_code == 200
    return resp.get_json()['token']


def link_export(client, tok, types=ALL, **kw):
    return f.export(client, tok, types, profile='ktc', deliver='link', **kw)



def test_a_link_carries_the_profiled_bundle_and_no_plaintext(app, client):
    f.seed(app, *_records())
    resp = link_export(client, ktc_token(client))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.headers['Cache-Control'] == 'no-store'
    body = resp.get_json()
    assert set(body) == {'link_id', 'shlink', 'exp'}
    text = resp.get_data(as_text=True)
    assert 'Testperson' not in text and 'entry' not in text

    payload, key = rx_genie_parse(body['shlink'])
    assert payload['url'] == f'{BASE}/shl/{body["link_id"]}'
    assert payload['label'] == ktc.LINK_LABEL
    assert 3590 <= payload['exp'] - time.time() <= 3600
    assert payload['exp'] == body['exp']

    got = client.get(f'/shl/{body["link_id"]}',
                     query_string={'recipient': 'RX Genie Demo Pharmacy'})
    assert got.status_code == 200
    assert got.mimetype == 'application/jose'
    assert got.headers['Cache-Control'] == 'no-store'
    bundle = json.loads(rx_genie_decrypt(key, got.get_data(as_text=True)))
    assert bundle['resourceType'] == 'Bundle'
    types = {e['resource']['resourceType'] for e in bundle['entry']}
    assert 'Patient' in types and 'Coverage' in types
    assert all(ktc.is_test_data(e['resource']) for e in bundle['entry'])
    assert f.CANARY_SSN not in json.dumps(bundle)


def test_the_store_holds_no_key_and_no_plaintext(app, client):
    f.seed(app, *_records())
    body = link_export(client, ktc_token(client)).get_json()
    key = shl.b64url(rx_genie_parse(body['shlink'])[1])
    with app.app_context():
        rows = shl_links.ShlLink.query.all()
        assert len(rows) == 1
        stored = json.dumps([r.ciphertext for r in rows]) + rows[0].id_hash
    assert key not in stored and body['link_id'] not in stored
    assert 'Testperson' not in stored


def test_one_use_then_404_and_the_ciphertext_is_dropped(app, client):
    """MUTATION: drop the uses_left decrement -> red."""
    f.seed(app, *_records())
    link_id = link_export(client, ktc_token(client)).get_json()['link_id']
    assert client.get(f'/shl/{link_id}').status_code == 200
    assert client.get(f'/shl/{link_id}').status_code == 404
    with app.app_context():
        assert shl_links.ShlLink.query.one().ciphertext is None


def test_max_uses_is_configurable(app, client, monkeypatch):
    monkeypatch.setenv('SHL_LINK_MAX_USES', '2')
    f.seed(app, *_records())
    link_id = link_export(client, ktc_token(client)).get_json()['link_id']
    assert [client.get(f'/shl/{link_id}').status_code
            for _ in range(3)] == [200, 200, 404]


def test_an_expired_link_answers_404(app, client, monkeypatch):
    f.seed(app, *_records())
    link_id = link_export(client, ktc_token(client)).get_json()['link_id']
    later = time.time() + 3601
    monkeypatch.setattr(shl_links.time, 'time', lambda: later)
    assert client.get(f'/shl/{link_id}').status_code == 404


def test_unknown_and_malformed_ids_answer_404(client):
    assert client.get('/shl/' + 'A' * 43).status_code == 404
    assert client.get('/shl/short').status_code == 404


def test_real_records_are_refused_and_the_token_kept(app, client):
    """Sendblue is not under a BAA: a link carries HTEST records only.

    MUTATION: drop the is_test_data check -> red."""
    f.seed(app, *_records(test_data=False))
    tok = ktc_token(client)
    resp = link_export(client, tok)
    assert resp.status_code == 403
    assert resp.get_json() == {'error': 'ktc_not_test_data'}
    with app.app_context():
        assert shl_links.ShlLink.query.count() == 0
    # Unspent: the bundle answer still works with the same token.
    assert f.export(client, tok, ALL, profile='ktc').status_code == 200


def test_one_real_record_among_test_ones_is_refused(app, client):
    rows = _records()
    rows[1]['meta'].pop('tag')
    f.seed(app, *rows)
    assert link_export(client, ktc_token(client)).status_code == 403


def test_the_owner_flag_lets_real_records_through(app, client, monkeypatch):
    monkeypatch.setenv('KTC_LINK_REAL_RECORDS', 'true')
    f.seed(app, *_records(test_data=False))
    assert link_export(client, ktc_token(client)).status_code == 200


@pytest.mark.parametrize('base', [None, '', 'http://engine.example.test'])
def test_no_https_public_base_means_no_link(app, client, monkeypatch, base):
    if base is None:
        monkeypatch.delenv('PUBLIC_BASE_URL')
    else:
        monkeypatch.setenv('PUBLIC_BASE_URL', base)
    f.seed(app, *_records())
    resp = link_export(client, ktc_token(client))
    assert resp.status_code == 503
    assert resp.get_json() == {'error': 'ktc_link_unavailable'}


@pytest.mark.parametrize('deliver', ['bundle', 'LINK', 1, True])
def test_an_unknown_deliver_is_a_400(app, client, deliver):
    f.seed(app, *_records())
    resp = f.export(client, ktc_token(client), ALL, profile='ktc',
                    deliver=deliver)
    assert resp.status_code == 400


def test_the_link_path_has_a_lower_size_ceiling(app, client, monkeypatch):
    monkeypatch.setattr(ktc, 'LINK_MAX_BUNDLE_BYTES', 10)
    f.seed(app, *_records())
    resp = link_export(client, ktc_token(client))
    assert resp.status_code == 413
    assert resp.get_json() == {'error': 'ktc_too_large'}


def test_the_flag_still_guards_the_link(app, client, monkeypatch):
    monkeypatch.delenv('KTC_EXPORT_ENABLED')
    f.seed(app, *_records())
    resp = link_export(client, None)
    assert resp.status_code == 404


def test_audits_are_fixed_words_without_the_recipient(app, client):
    f.seed(app, *_records())
    link_id = link_export(client, ktc_token(client)).get_json()['link_id']
    client.get(f'/shl/{link_id}', query_string={'recipient': 'CANARY-rx'})
    with app.app_context():
        rows = [(a.agent_id, a.resource_id, a.detail)
                for a in AuditEventRecord.query.filter_by(
                    tenant_id=f.TENANT).all()]
    details = json.dumps(rows)
    assert 'CANARY' not in details and link_id not in details
    assert ('careagents-ktc', 'shl-file',
            'shl file served to a link holder (one use spent)') in rows
    assert any(d.startswith('share-bundle export (profile=ktc, deliver=link)')
               for _, _, d in rows)


def test_issue_clears_expired_rows(app, monkeypatch):
    with app.app_context():
        shl_links.issue('t1', b'{}', 'x', now=1000)
        db.session.commit()
        shl_links.issue('t1', b'{}', 'x', now=1000 + 7200)
        db.session.commit()
        assert shl_links.ShlLink.query.count() == 1
