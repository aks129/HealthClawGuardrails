"""A live walkthrough against the Synthetic Hospital FHIR R4 simulator.

Opt-in, never default: skipped unless SYNTHETIC_HOSPITAL_URL is set. The
simulator (github.com/sparkcpark/synthetic_hospital, MIT) serves fully
synthetic records. It is a benchmark, never a source of real ones.

It is worth running because its data is shaped the way real feeds are, not
the way hand-written fixtures are. Patient names are placeholders; the
realistic names are CLINICIAN names, in Encounter.participant[].individual
.display. Clinical content sits in code.text, note text and attachment data.
Allergies and medications carry their content only in note text, with no
codes at all.

Each test fetches the same records twice: raw, straight from the simulator,
and through HealthClaw's proxy and redaction. The raw copy supplies the
strings that must not come out the other side.

Run with:
  SYNTHETIC_HOSPITAL_URL=http://localhost:58000/fhir \\
  SYNTHETIC_HOSPITAL_USERNAME=... SYNTHETIC_HOSPITAL_PASSWORD=... \\
  uv run python -m pytest tests/test_synthetic_hospital_live.py -v -rA
"""

import os
from urllib.parse import urlsplit, urlunsplit

import pytest

from r6 import terminology
from r6.fhir_proxy import reset_proxy

BASE = os.environ.get('SYNTHETIC_HOSPITAL_URL', '').strip().rstrip('/')

pytestmark = pytest.mark.skipif(
    not BASE, reason='live simulator; set SYNTHETIC_HOSPITAL_URL to run')

SEARCHED = ('Encounter', 'Condition', 'DocumentReference',
            'MedicationRequest', 'AllergyIntolerance')


def _credentials():
    user = os.environ.get('SYNTHETIC_HOSPITAL_USERNAME', '').strip()
    password = os.environ.get('SYNTHETIC_HOSPITAL_PASSWORD', '').strip()
    if not (user and password):
        pytest.fail('SYNTHETIC_HOSPITAL_URL is set but '
                    'SYNTHETIC_HOSPITAL_USERNAME/_PASSWORD are not')
    return user, password


@pytest.fixture(scope='module')
def raw():
    """Fetch the records straight from the simulator, bypassing HealthClaw."""
    import httpx

    user, password = _credentials()
    parts = urlsplit(BASE)
    token_url = urlunsplit((parts.scheme, parts.netloc, '/auth/token', '', ''))
    resp = httpx.post(token_url, json={'username': user, 'password': password},
                      timeout=10)
    resp.raise_for_status()
    headers = {'Authorization': f"Bearer {resp.json()['access_token']}"}

    with httpx.Client(base_url=BASE, headers=headers, timeout=15) as c:
        first = c.get('/Patient', params={'_count': '1'}).json()
        patient_id = first['entry'][0]['resource']['id']
        out = {'patient_id': patient_id,
               'Patient': c.get(f'/Patient/{patient_id}').json()}
        for rt in SEARCHED:
            r = c.get(f'/{rt}', params={'patient': patient_id})
            r.raise_for_status()
            out[rt] = r.json()
    return out


@pytest.fixture
def upstream(monkeypatch):
    user, password = _credentials()
    monkeypatch.setenv('FHIR_UPSTREAM_KIND', 'synthetic_hospital')
    monkeypatch.setenv('FHIR_UPSTREAM_URL', BASE)
    monkeypatch.setenv('FHIR_UPSTREAM_CLIENT_ID', user)
    monkeypatch.setenv('FHIR_UPSTREAM_CLIENT_SECRET', password)
    reset_proxy()
    yield
    reset_proxy()


def _walk(obj, key=None, parent=None):
    """Yield (key, value, parent dict) for every leaf in a JSON tree."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, k, obj)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, key, parent)
    else:
        yield key, obj, parent


def _server_labels(tree):
    """Every label r6/terminology.py would attach to a coding in `tree`."""
    labels = set()
    for key, value, parent in _walk(tree):
        if key == 'code' and isinstance(parent, dict) and 'system' in parent:
            label = terminology.lookup(parent.get('system'), value)
            if label:
                labels.add(label)
    return labels


def _upstream_free_text(tree):
    """display, text and attachment data strings the simulator wrote."""
    return {v for k, v, _ in _walk(tree)
            if k in ('display', 'text', 'data') and isinstance(v, str)
            and v.strip()}


def _clinician_names(raw):
    names = set()
    for entry in raw['Encounter'].get('entry', []):
        for p in entry['resource'].get('participant', []):
            display = (p.get('individual') or {}).get('display')
            if display:
                names.add(display)
    return names


def _through_healthclaw(client, tenant_headers, raw):
    pid = raw['patient_id']
    out = {}
    r = client.get(f'/r6/fhir/Patient/{pid}', headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    out['Patient'] = r
    for rt in SEARCHED:
        r = client.get(f'/r6/fhir/{rt}?patient={pid}', headers=tenant_headers)
        assert r.status_code == 200, (rt, r.get_data(as_text=True)[:300])
        out[rt] = r
    return out


def test_the_walkthrough_reads_every_type(client, tenant_headers, upstream,
                                          raw):
    got = _through_healthclaw(client, tenant_headers, raw)
    for rt in SEARCHED:
        body = got[rt].get_json()
        assert body['_source'] == 'upstream'
        assert len(body['entry']) == len(raw[rt].get('entry', [])), (
            f'{rt}: HealthClaw returned a different number of records than '
            'the simulator. A dropped record is worse than an unlabelled one.')
    print(f"\nwalkthrough: patient + {', '.join(SEARCHED)}; entries "
          + ', '.join(f"{rt}={len(got[rt].get_json()['entry'])}"
                      for rt in SEARCHED))


def test_clinician_names_never_come_out(client, tenant_headers, upstream,
                                        raw):
    names = _clinician_names(raw)
    assert names, 'the simulator returned no clinician names to look for'
    got = _through_healthclaw(client, tenant_headers, raw)
    for rt, resp in got.items():
        body = resp.get_data(as_text=True)
        leaked = sorted(n for n in names if n in body)
        assert not leaked, f'{rt} leaked {len(leaked)} clinician name(s)'
    print(f'\nclinician names checked: {len(names)}, leaked: 0')


def test_no_upstream_display_or_text_survives(client, tenant_headers,
                                              upstream, raw):
    """A surviving label must be the server's own, keyed by code."""
    got = _through_healthclaw(client, tenant_headers, raw)
    upstream_text = set()
    labels = set()
    for rt in ('Patient',) + SEARCHED:
        upstream_text |= _upstream_free_text(raw[rt])
        labels |= _server_labels(raw[rt])
    forbidden = upstream_text - labels
    assert forbidden, 'nothing to check: the simulator sent no free text'

    for rt, resp in got.items():
        tree = resp.get_json()
        body = resp.get_data(as_text=True)
        for key, value, parent in _walk(tree):
            if not isinstance(value, str):
                continue
            assert value not in forbidden, f'{rt}.{key} kept upstream text'
            if key == 'display':
                expected = terminology.lookup(parent.get('system'),
                                              parent.get('code'))
                assert value == expected, (
                    f'{rt}: a display that is not the server label for its code')
        long_forbidden = [t for t in forbidden if len(t) >= 8]
        assert not [t for t in long_forbidden if t in body], (
            f'{rt} contains upstream free text inside another string')
    print(f'\nupstream free-text strings checked: {len(forbidden)}, '
          f'server labels allowed: {len(labels)}')


def test_attachment_data_and_note_text_are_stripped(client, tenant_headers,
                                                    upstream, raw):
    got = _through_healthclaw(client, tenant_headers, raw)
    docs = got['DocumentReference'].get_json()['entry']
    assert docs
    for entry in docs:
        for content in entry['resource'].get('content', []):
            attachment = content.get('attachment', {})
            assert 'data' not in attachment and 'title' not in attachment
    for rt in ('MedicationRequest', 'AllergyIntolerance'):
        for entry in got[rt].get_json()['entry']:
            for note in entry['resource'].get('note', []):
                assert note.get('text') in (None, '[Redacted]'), rt


def test_every_access_is_audited(app, client, tenant_headers, tenant_id,
                                 upstream, raw):
    from r6.models import AuditEventRecord

    _through_healthclaw(client, tenant_headers, raw)
    rows = AuditEventRecord.query.filter_by(tenant_id=tenant_id).all()
    upstream_rows = [r for r in rows if r.detail and 'upstream' in r.detail]
    assert len(upstream_rows) >= 1 + len(SEARCHED), [r.detail for r in rows]
    assert all(r.outcome == 'success' for r in upstream_rows)
    print(f'\naudit rows for upstream access: {len(upstream_rows)}')


def test_malformed_paging_links_are_not_passed_through(client, tenant_headers,
                                                       upstream, raw):
    """The simulator sends self url "" and next url "&_offset=N"."""
    broken = [link for rt in SEARCHED for link in raw[rt].get('link', [])
              if not urlsplit(link.get('url', '')).netloc]
    assert broken, 'the simulator no longer sends malformed links'
    got = _through_healthclaw(client, tenant_headers, raw)
    for rt in SEARCHED:
        body = got[rt].get_json()
        for link in body.get('link', []):
            parts = urlsplit(link['url'])
            assert parts.scheme in ('http', 'https') and parts.netloc, (
                f'{rt} passed through a broken link: {link!r}')
    print(f'\nmalformed upstream links seen: {len(broken)}, passed through: 0')
