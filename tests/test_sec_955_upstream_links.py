"""Security review of PR #955 (#954): no upstream host or page cursor in any
proxied response.

Drives a real FHIRUpstreamProxy (MockTransport upstream) through the real
Flask routes, with FHIR_LOCAL_BASE_URL unset and set. Synthetic data only.

The review's two findings on f39d389 are fixed and pinned here as ordinary
tests: an upstream Bundle.total is kept only as a non-negative int, and an
entry's fullUrl is built only from a type and id that fully match and are
not `.` or `..`. Absolute upstream URLs inside a resource's own fields are a
separate, tracked gap and are not tested here.
"""

import json
from unittest.mock import patch

import httpx
import pytest

from r6 import fhir_proxy

UP_HOST = 'upstream-sec955.example'
UP_BASE = f'https://{UP_HOST}/fhir'
CURSOR = 'CURSOR-sec955-zz'
CANARY = 'Zebulon Quixote-Canary'
LOCAL_BASES = [None, 'http://localhost/r6/fhir']


@pytest.fixture(autouse=True)
def _reset():
    fhir_proxy.reset_proxy()
    yield
    fhir_proxy.reset_proxy()


def _make_proxy(monkeypatch, local_base, search_body=None, read_body=None,
                seen=None):
    monkeypatch.setenv('FHIR_UPSTREAM_URL', UP_BASE)
    if local_base is None:
        monkeypatch.delenv('FHIR_LOCAL_BASE_URL', raising=False)
    else:
        monkeypatch.setenv('FHIR_LOCAL_BASE_URL', local_base)
    proxy = fhir_proxy.get_proxy()
    assert proxy is not None

    def handler(request):
        if seen is not None:
            seen.append(str(request.url))
        parts = [p for p in request.url.path.split('/') if p]
        if len(parts) >= 3 and read_body is not None:
            return httpx.Response(200, json=read_body)
        return httpx.Response(200, json=search_body)

    proxy._client = httpx.Client(base_url=UP_BASE,
                                 transport=httpx.MockTransport(handler))
    return proxy


HOSTILE_LINKS = [
    {'relation': 'self', 'url': f'{UP_BASE}/Condition?_getpages={CURSOR}'},
    {'relation': 'next', 'url': f'Condition?_getpages={CURSOR}&_offset=10'},
    {'relation': 'previous', 'url': f'/fhir/Condition?page={CURSOR}'},
    {'relation': 'last', 'url': f'//{UP_HOST}/fhir/Condition?c={CURSOR}'},
    {'relation': 'first', 'url': f'&_offset=0&cursor={CURSOR}'},
    {'relation': 'next', 'url': {'nested': f'{UP_BASE}/{CURSOR}'}},
    'not-a-dict',
]


def _odd_entries():
    def cond(rid, rtype='Condition'):
        return {'resourceType': rtype, 'id': rid}
    return [
        {'fullUrl': f'{UP_BASE}/Condition/ok-1', 'resource': cond('ok-1')},
        {'fullUrl': f'urn:uuid:{CURSOR}', 'resource': cond('a/b')},
        {'fullUrl': f'Condition/{CURSOR}', 'resource': cond('..')},
        {'fullUrl': 'javascript:alert(1)', 'resource': cond('../../x')},
        {'fullUrl': f'{UP_BASE}/x', 'resource': cond(f'{UP_BASE}/Condition/1')},
        {'fullUrl': f'{UP_BASE}/x', 'resource': cond('ok-2\n')},
        {'fullUrl': f'{UP_BASE}/x', 'resource': cond(123)},
        {'fullUrl': f'{UP_BASE}/x', 'resource': cond(None)},
        {'fullUrl': f'{UP_BASE}/x', 'resource': cond('ok-3', 'Condition/../Patient')},
        {'fullUrl': f'{UP_BASE}/x', 'resource': cond('ok-4', f'{UP_BASE}')},
        {'fullUrl': f'{UP_BASE}/x', 'resource': cond('ok-5', 'Condition\n')},
        {'fullUrl': f'{UP_BASE}/OperationOutcome/w-{CURSOR}',
         'resource': {
             'resourceType': 'OperationOutcome', 'id': f'w-{CURSOR}',
             'meta': {'source': f'{UP_BASE}/{CURSOR}'},
             'extension': [{'url': f'{UP_BASE}/ext', 'valueString': CANARY}],
             'issue': [{'severity': 'warning', 'code': 'processing',
                        'diagnostics': f'{CANARY} see {UP_BASE}?{CURSOR}',
                        'details': {'text': f'{CANARY} {UP_BASE}'},
                        'expression': [f'{UP_BASE}']}]},
         'search': {'mode': 'outcome',
                    'extension': [{'url': f'{UP_BASE}/{CURSOR}'}]}},
        {'fullUrl': f'{UP_BASE}/x',
         'resource': cond('ok-6'),
         'search': {'mode': {'x': f'{UP_BASE}'}, 'score': f'{UP_BASE}'}},
        {'fullUrl': f'{UP_BASE}/x', 'resource': f'{UP_BASE}'},
        f'{UP_BASE}/Condition/bare-string-entry',
    ]


def _hostile_bundle(total=3):
    return {'resourceType': 'Bundle', 'type': 'searchset', 'total': total,
            'id': f'b-{CURSOR}',
            'meta': {'source': f'{UP_BASE}/{CURSOR}'},
            'identifier': {'system': UP_BASE, 'value': CURSOR},
            'signature': {'who': {'reference': f'{UP_BASE}/Practitioner/1'}},
            'link': HOSTILE_LINKS, 'entry': _odd_entries()}


class TestLinksAndFullUrls:

    @pytest.mark.parametrize('local_base', LOCAL_BASES)
    def test_hostile_bundle_shell_names_no_upstream(
            self, client, tenant_headers, monkeypatch, local_base):
        proxy = _make_proxy(monkeypatch, local_base, search_body=_hostile_bundle())
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get('/r6/fhir/Condition?patient=Patient/p1',
                              headers=tenant_headers)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body['link'] == [{
            'relation': 'self',
            'url': 'http://localhost/r6/fhir/Condition?patient=Patient%2Fp1'}]
        # The shell: everything except each entry's resource body (a
        # resource's own fields are a separate, tracked gap).
        shell = dict(body)
        shell['entry'] = [{k: v for k, v in e.items() if k != 'resource'}
                          for e in body['entry']]
        outcomes = [e['resource'] for e in body['entry']
                    if e['resource'].get('resourceType') == 'OperationOutcome']
        text = json.dumps([shell, outcomes])
        assert UP_HOST not in text
        assert CURSOR not in text
        assert CANARY not in text
        for entry in body['entry']:
            assert set(entry) <= {'resource', 'fullUrl', 'search'}
            if 'search' in entry:
                assert set(entry['search']) <= {'mode', 'score'}

    @pytest.mark.parametrize('local_base', LOCAL_BASES)
    def test_a_dot_or_dot_dot_id_gets_no_fullurl(
            self, client, tenant_headers, monkeypatch, local_base):
        """`.` and `..` match the id class, and a client normalises
        .../Condition/.. to /r6/fhir/. They get no fullUrl at all.

        MUTATION: drop the `.`/`..` rejection in upstream_entry -> red."""
        bundle = {'resourceType': 'Bundle', 'type': 'searchset', 'entry': [
            {'resource': {'resourceType': 'Condition', 'id': '..'}},
            {'resource': {'resourceType': 'Condition', 'id': '.'}},
            {'resource': {'resourceType': 'Condition', 'id': 'ok-1'}}]}
        proxy = _make_proxy(monkeypatch, local_base, search_body=bundle)
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get('/r6/fhir/Condition', headers=tenant_headers)
        entries = resp.get_json()['entry']
        assert 'fullUrl' not in entries[0]
        assert 'fullUrl' not in entries[1]
        assert entries[2]['fullUrl'] == 'http://localhost/r6/fhir/Condition/ok-1'

    @pytest.mark.parametrize('local_base', LOCAL_BASES)
    def test_a_trailing_newline_in_type_or_id_gets_no_fullurl(
            self, client, tenant_headers, monkeypatch, local_base):
        """`$` matches before a trailing newline, so `id: "ok-2\\n"` passed
        a re.match check. The check is re.fullmatch, so neither entry gets a
        fullUrl.

        MUTATION: fullmatch -> match -> red."""
        bundle = {'resourceType': 'Bundle', 'type': 'searchset', 'entry': [
            {'resource': {'resourceType': 'Condition', 'id': 'ok-2\n'}},
            {'resource': {'resourceType': 'Condition\n', 'id': 'ok-5'}}]}
        proxy = _make_proxy(monkeypatch, local_base, search_body=bundle)
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get('/r6/fhir/Condition', headers=tenant_headers)
        for entry in resp.get_json()['entry']:
            assert 'fullUrl' not in entry, entry['fullUrl']


class TestTotal:

    @pytest.mark.parametrize('local_base', LOCAL_BASES)
    def test_a_non_integer_upstream_total_is_replaced_by_the_entry_count(
            self, client, tenant_headers, monkeypatch, local_base):
        """A hostile upstream can put its host, a cursor or free text in
        Bundle.total. It reached MCP clients raw, and CareAgents' int() on it
        raised. Only a non-negative int (not a bool) is kept.

        MUTATION: copy bundle['total'] raw again -> red."""
        # Entries without upstream URLs in their own fields: those are the
        # separate, tracked gap. This isolates Bundle.total.
        bundle = {'resourceType': 'Bundle', 'type': 'searchset',
                  'total': f'{UP_BASE}/Condition?_getpages={CURSOR} {CANARY}',
                  'link': HOSTILE_LINKS,
                  'entry': [{'fullUrl': f'{UP_BASE}/Condition/c1',
                             'resource': {'resourceType': 'Condition',
                                          'id': 'c1'}}]}
        proxy = _make_proxy(monkeypatch, local_base, search_body=bundle)
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get('/r6/fhir/Condition', headers=tenant_headers)
        text = resp.get_data(as_text=True)
        assert UP_HOST not in text and CANARY not in text
        body = resp.get_json()
        assert body['total'] == len(body['entry'])

    @pytest.mark.parametrize('total,expected', [
        (7, 7), (0, 0), (-1, 1), (True, 1), (2.5, 1), ('7', 1), (None, 1),
        ([7], 1)])
    def test_only_a_non_negative_int_total_is_kept(
            self, client, tenant_headers, monkeypatch, total, expected):
        bundle = {'resourceType': 'Bundle', 'type': 'searchset',
                  'total': total,
                  'entry': [{'resource': {'resourceType': 'Condition',
                                          'id': 'c1'}}]}
        proxy = _make_proxy(monkeypatch, None, search_body=bundle)
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get('/r6/fhir/Condition', headers=tenant_headers)
        got = resp.get_json()['total']
        assert got == expected and not isinstance(got, bool)


class TestSelfLink:

    @pytest.mark.parametrize('local_base', LOCAL_BASES)
    def test_crafted_query_puts_nothing_sensitive_in_self(
            self, client, tenant_headers, monkeypatch, local_base):
        seen = []
        proxy = _make_proxy(monkeypatch, local_base, seen=seen,
                            search_body={'resourceType': 'Bundle',
                                         'type': 'searchset', 'entry': []})
        q = ('/r6/fhir/Condition?patient=Patient/p1&_count=5&_count=999'
             '&context-id=ctx-SECRET1&tenant=t2&tenant_id=t3&t=t4'
             '&X-Step-Up-Token=STEPUP-SECRET&_getpages=' + CURSOR +
             '&_offset=10&page=2&Context-Id=ctx-SECRET2&_format=json'
             '&_include=Condition:subject')
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get(q, headers=tenant_headers)
        assert resp.status_code == 200
        self_url = resp.get_json()['link'][0]['url']
        assert self_url == ('http://localhost/r6/fhir/Condition'
                            '?patient=Patient%2Fp1&_count=5')
        for bad in ('SECRET', 'tenant', 't2', 'STEPUP', CURSOR, '_offset',
                    'page', 'context', '_include', '_format'):
            assert bad not in self_url
        # And none of it went upstream either.
        assert len(seen) == 1
        for bad in ('SECRET', 'tenant', 'STEPUP', CURSOR, '_offset',
                    'page=', 'context'):
            assert bad not in seen[0], seen[0]

    def test_step_up_header_and_tenant_header_never_reach_self(
            self, client, tenant_headers, monkeypatch, step_up_token):
        proxy = _make_proxy(monkeypatch, None,
                            search_body={'resourceType': 'Bundle',
                                         'type': 'searchset', 'entry': []})
        headers = dict(tenant_headers, **{'X-Step-Up-Token': step_up_token})
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get('/r6/fhir/Condition?code=1234', headers=headers)
        text = resp.get_data(as_text=True)
        assert step_up_token not in text
        assert tenant_headers['X-Tenant-Id'] not in resp.get_json()['link'][0]['url']


class TestAuditUnchanged:

    def test_search_upstream_audits_once_without_urls(
            self, app, client, tenant_headers, monkeypatch):
        from r6.models import AuditEventRecord
        proxy = _make_proxy(monkeypatch, None, search_body=_hostile_bundle())
        with app.app_context():
            before = AuditEventRecord.query.count()
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get('/r6/fhir/Condition?patient=Patient/p1&bogus=1',
                              headers=tenant_headers)
        assert resp.status_code == 200
        with app.app_context():
            rows = AuditEventRecord.query.order_by(
                AuditEventRecord.id).all()[before:]
        assert len(rows) == 1
        detail = rows[0].detail or ''
        assert detail.startswith('search (upstream): ')
        assert UP_HOST not in detail and CURSOR not in detail
        assert 'Patient/p1' not in detail


class TestAuditFailsClosed:

    def test_audit_write_error_blocks_the_upstream_search(
            self, client, tenant_headers, monkeypatch):
        from r6.audit import AuditWriteError
        proxy = _make_proxy(monkeypatch, None, search_body=_hostile_bundle())

        def boom(*a, **k):
            raise AuditWriteError('forced')
        with patch('r6.routes.get_proxy_for_request', return_value=proxy), \
                patch('r6.routes.record_audit_event', side_effect=boom):
            # TESTING propagates the exception: no body is ever returned.
            with pytest.raises(AuditWriteError):
                client.get('/r6/fhir/Condition', headers=tenant_headers)
