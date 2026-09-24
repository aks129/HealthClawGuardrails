"""Today's answers at the raw X-Tenant-Id reads that move to the kernel.

Written against main BEFORE the swap and kept byte-identical through it
(tests/test_ratchets.py, _RAW_TENANT_READS). Each row is a (status, body) a
client can see today; a row that changes is a behaviour change, and those
belong in their own PR, not in a refactor.

Only the sites whose answer the kernel can reproduce exactly are pinned
here. The ones it cannot — a looser id pattern, a live .strip(), a
malformed id that dev-open mode accepts — stay raw and are listed beside
the ratchet.
"""

import hashlib
import logging
from unittest.mock import patch

import pytest

from r6.fhir_proxy import SHARP_ACCESS_TOKEN_HEADER, SHARP_SERVER_URL_HEADER

_ABSENT = {'resourceType': 'OperationOutcome', 'issue': [{
    'severity': 'error', 'code': 'security',
    'diagnostics': 'X-Tenant-Id header is required'}]}
_MALFORMED = {'resourceType': 'OperationOutcome', 'issue': [{
    'severity': 'error', 'code': 'invalid',
    'diagnostics': 'X-Tenant-Id must match [a-zA-Z0-9_-]{1,64}'}]}


# ---------------------------------------------------------------------------
# enforce_tenant_id, and the handlers it protects
# ---------------------------------------------------------------------------

_HOOKED = [
    ('GET', '/r6/fhir/Patient'),
    ('GET', '/r6/fhir/AppointmentBrief'),
    ('GET', '/r6/fhir/Patient/$care-gaps'),
    ('POST', '/r6/fhir/Observation/$interpret'),
    ('POST', '/r6/fhir/Measure/nqf0018-controlling-high-bp/$evaluate-measure'),
]


@pytest.mark.parametrize('method,path', _HOOKED)
@pytest.mark.parametrize('headers,expected', [
    ({}, _ABSENT),
    ({'X-Tenant-Id': ''}, _ABSENT),
    ({'X-Tenant-Id': 'bad tenant'}, _MALFORMED),
    ({'X-Tenant-Id': ' test-tenant'}, _MALFORMED),
    ({'X-Tenant-Id': 'x' * 65}, _MALFORMED),
])
def test_the_tenant_hook_answers_before_any_handler(client, method, path,
                                                     headers, expected):
    """The hook's two refusals, on the blueprint's own reads and on the four
    handlers whose local `_tenant()` wrappers go away."""
    r = client.open(path, method=method, headers=headers, json={})
    assert r.status_code == 400
    assert r.mimetype == 'application/json'
    assert r.get_json() == expected


def _sharp_tenant(url):
    return 'sharp-' + hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]


@pytest.mark.parametrize('tenant_header', [None, ''])
def test_a_sharp_request_reaches_the_handler_with_its_synthesized_tenant(
        client, tenant_header):
    """The hook writes the SHARP tenant back into the environ; the handler's
    own tenant read must see it, not an absent header."""
    url = 'https://hapi.fhir.org/baseR4'
    seen = {}

    def read(*_args, **_kwargs):
        from flask import request
        seen['tenant'] = request.headers.get('X-Tenant-Id')
        return None, 404

    headers = {SHARP_SERVER_URL_HEADER: url, SHARP_ACCESS_TOKEN_HEADER: 'tok'}
    if tenant_header is not None:
        headers['X-Tenant-Id'] = tenant_header
    with patch('r6.fhir_proxy.FHIRUpstreamProxy.read', side_effect=read):
        r = client.get('/r6/fhir/Patient/upstream-only', headers=headers)
    assert r.status_code == 404
    assert seen['tenant'] == _sharp_tenant(url)


def test_a_malformed_tenant_is_refused_even_in_a_sharp_context(client):
    """A present header wins over SHARP synthesis, so a malformed one is
    refused rather than replaced."""
    headers = {SHARP_SERVER_URL_HEADER: 'https://hapi.fhir.org/baseR4',
               SHARP_ACCESS_TOKEN_HEADER: 'tok', 'X-Tenant-Id': 'bad tenant'}
    r = client.get('/r6/fhir/Patient/upstream-only', headers=headers)
    assert r.status_code == 400
    assert r.get_json() == _MALFORMED


# ---------------------------------------------------------------------------
# command center: _tenant() behind _require_session_or_public
# ---------------------------------------------------------------------------

_CC_DENIED = {'error': 'authentication required for this tenant'}


@pytest.mark.parametrize('query,headers,status', [
    ('', {}, 200),                                      # default tenant
    ('', {'X-Tenant-Id': 'desktop-demo'}, 200),
    ('', {'X-Tenant-Id': 'bad tenant'}, 401),
    ('', {'X-Tenant-Id': 'private-tenant'}, 401),
    ('?tenant=bad%20tenant', {'X-Tenant-Id': 'desktop-demo'}, 401),
    ('?tenant=desktop-demo', {'X-Tenant-Id': 'bad tenant'}, 200),
    ('?tenant=', {'X-Tenant-Id': 'bad tenant'}, 401),
])
def test_command_center_tenant_resolution(client, query, headers, status):
    r = client.get(f'/command-center/api/overview{query}', headers=headers)
    assert r.status_code == status
    if status == 401:
        assert r.get_json() == _CC_DENIED


def test_command_center_session_outranks_query_and_header(client):
    from r6.read_auth import TENANT_SESSION_KEY
    with client.session_transaction() as sess:
        sess[TENANT_SESSION_KEY] = 'private-tenant'
    r = client.get('/command-center/api/overview?tenant=bad%20tenant',
                   headers={'X-Tenant-Id': 'bad tenant'})
    assert r.status_code == 200


def test_command_center_malformed_session_tenant_is_refused_not_raised(client):
    from r6.read_auth import TENANT_SESSION_KEY
    with client.session_transaction() as sess:
        sess[TENANT_SESSION_KEY] = 'bad tenant'
    r = client.get('/command-center/api/overview',
                   headers={'X-Tenant-Id': 'desktop-demo'})
    assert r.status_code == 401
    assert r.get_json() == _CC_DENIED


# ---------------------------------------------------------------------------
# wearables /sync-status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('query,headers,status,body', [
    ('', {}, 400, {'error': 'tenant_id required'}),
    ('?tenant_id=', {}, 400, {'error': 'tenant_id required'}),
    ('', {'X-Tenant-Id': ''}, 400, {'error': 'tenant_id required'}),
    ('', {'X-Tenant-Id': 'bad tenant'}, 401,
     {'error': 'authentication required for this tenant'}),
    ('?tenant_id=bad%20tenant', {'X-Tenant-Id': 'desktop-demo'}, 401,
     {'error': 'authentication required for this tenant'}),
])
def test_wearables_sync_status_refusals(client, query, headers, status, body):
    r = client.get(f'/wearables/sync-status{query}', headers=headers)
    assert r.status_code == status
    assert r.get_json() == body


@pytest.mark.parametrize('query,headers', [
    ('?tenant_id=desktop-demo', {'X-Tenant-Id': 'bad tenant'}),
    ('', {'X-Tenant-Id': 'desktop-demo'}),
    ('?tenant_id=', {'X-Tenant-Id': 'desktop-demo'}),
])
def test_wearables_sync_status_query_then_header(client, query, headers):
    r = client.get(f'/wearables/sync-status{query}', headers=headers)
    assert r.status_code == 200
    assert r.get_json()['tenant_id'] == 'desktop-demo'


# ---------------------------------------------------------------------------
# /internal/connect-diagnostic — the tenant only reaches a log line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('headers', [
    {}, {'X-Tenant-Id': 'desktop-demo'}, {'X-Tenant-Id': 'bad tenant'}])
@pytest.mark.parametrize('payload,status,body', [
    ({'step': 'connect'}, 202, None),
    ({'entry': []}, 422, {'error': 'diagnostic payload rejected'}),
])
def test_connect_diagnostic_answer_ignores_the_tenant(client, headers, payload,
                                                      status, body):
    r = client.post('/r6/fhir/internal/connect-diagnostic', headers=headers,
                    json={'payload': payload})
    assert r.status_code == status
    if body is not None:
        assert r.get_json() == body
    else:
        assert set(r.get_json()) == {'reference'}


def test_connect_diagnostic_logs_a_valid_tenant(client, caplog):
    with caplog.at_level(logging.WARNING, logger='r6.routes'):
        client.post('/r6/fhir/internal/connect-diagnostic',
                    headers={'X-Tenant-Id': 'desktop-demo'},
                    json={'payload': {'step': 'connect'}})
    assert 'tenant=desktop-demo ' in caplog.text
