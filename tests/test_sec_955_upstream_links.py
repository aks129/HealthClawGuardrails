"""Security review of PR #955 (#954): no upstream host or page cursor in any
proxied response.

Drives a real FHIRUpstreamProxy (MockTransport upstream) through the real
Flask routes, with FHIR_LOCAL_BASE_URL unset and set. Synthetic data only.
Tests named `test_finding_*` pin behaviour the review found to be wrong;
they fail at the PR head on purpose.
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
        # resource's own fields are the separate gap, TestResourceFieldsGap).
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
    def test_finding_dot_dot_id_gets_a_traversing_fullurl(
            self, client, tenant_headers, monkeypatch, local_base):
        """`..` and `.` match [A-Za-z0-9\\-.]{1,64}, so fullUrl becomes
        http://localhost/r6/fhir/Condition/.. which a client normalises to
        /r6/fhir/. Our host only; informational."""
        bundle = {'resourceType': 'Bundle', 'type': 'searchset', 'entry': [
            {'resource': {'resourceType': 'Condition', 'id': '..'}}]}
        proxy = _make_proxy(monkeypatch, local_base, search_body=bundle)
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get('/r6/fhir/Condition', headers=tenant_headers)
        assert not resp.get_json()['entry'][0].get('fullUrl', '').endswith('/..')

    @pytest.mark.parametrize('local_base', LOCAL_BASES)
    def test_finding_id_with_trailing_newline_gets_a_fullurl(
            self, client, tenant_headers, monkeypatch, local_base):
        """`$` in _ENTRY_ID/_ENTRY_TYPE matches before a trailing newline, so
        `id: "ok-2\\n"` passes the "plain id" check and yields a fullUrl that
        ends in a newline. Low: the host is ours; but the check is not the
        check it claims to be. Fix: re.fullmatch or \\Z."""
        bundle = {'resourceType': 'Bundle', 'type': 'searchset', 'entry': [
            {'resource': {'resourceType': 'Condition', 'id': 'ok-2\n'}},
            {'resource': {'resourceType': 'Condition\n', 'id': 'ok-5'}}]}
        proxy = _make_proxy(monkeypatch, local_base, search_body=bundle)
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get('/r6/fhir/Condition', headers=tenant_headers)
        for entry in resp.get_json()['entry']:
            assert '\n' not in entry.get('fullUrl', ''), entry['fullUrl']


class TestTotal:

    @pytest.mark.parametrize('local_base', LOCAL_BASES)
    def test_finding_upstream_total_passes_through_unvalidated(
            self, client, tenant_headers, monkeypatch, local_base):
        """Bundle.total is copied raw from upstream (r6/routes.py, upstream
        branch `bundle.get('total', ...)`). A hostile upstream puts its host,
        a cursor, or free text there. Pre-existing, not introduced by #955,
        but it falsifies "no response carries the upstream host"."""
        bundle = _hostile_bundle(total=f'{UP_BASE}/Condition?_getpages={CURSOR} {CANARY}')
        proxy = _make_proxy(monkeypatch, local_base, search_body=bundle)
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get('/r6/fhir/Condition', headers=tenant_headers)
        text = resp.get_data(as_text=True)
        assert UP_HOST not in text and CANARY not in text


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


class TestResourceFieldsGap:
    """The gap the builder left open: absolute upstream URLs inside a
    resource's own fields. Pinned so the ruling is visible."""

    RES = {'resourceType': 'Condition', 'id': 'c1',
           'subject': {'reference': f'{UP_BASE}/Patient/p1'},
           'meta': {'source': f'{UP_BASE}/{CURSOR}'},
           'extension': [{'url': f'{UP_BASE}/StructureDefinition/x',
                          'valueReference': {'reference': f'{UP_BASE}/Encounter/e1'}}],
           'evidence': [{'detail': [{'reference': f'{UP_BASE}/Observation/o1'}]}]}

    @pytest.mark.parametrize('path', ['/r6/fhir/Condition/c1', '/r6/fhir/Condition'])
    def test_finding_unset_base_resource_fields_name_upstream(
            self, client, tenant_headers, monkeypatch, path):
        proxy = _make_proxy(monkeypatch, None, read_body=self.RES,
                            search_body={'resourceType': 'Bundle', 'type': 'searchset',
                                         'entry': [{'resource': self.RES}]})
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get(path, headers=tenant_headers)
        assert resp.status_code == 200
        assert UP_HOST not in resp.get_data(as_text=True)

    @pytest.mark.parametrize('variant', [
        f'https://{UP_HOST.upper()}/fhir',
        f'http://{UP_HOST}/fhir',
        f'https://{UP_HOST}:443/fhir',
        f'https://{UP_HOST}/fhir/',  # trailing slash still contains base
    ])
    def test_finding_set_base_rewrite_is_exact_string_only(
            self, client, tenant_headers, monkeypatch, variant):
        """With FHIR_LOCAL_BASE_URL set, _rewrite_urls replaces only the exact
        upstream_url string; a case, scheme or port variant survives."""
        res = json.loads(json.dumps(self.RES).replace(UP_BASE, variant))
        proxy = _make_proxy(monkeypatch, 'http://localhost/r6/fhir', read_body=res)
        with patch('r6.routes.get_proxy_for_request', return_value=proxy):
            resp = client.get('/r6/fhir/Condition/c1', headers=tenant_headers)
        assert UP_HOST not in resp.get_data(as_text=True).lower()


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
