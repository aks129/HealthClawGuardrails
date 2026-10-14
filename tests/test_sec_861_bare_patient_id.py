"""Security review of #861: bare id for the `patient` search parameter.

Each test tries to make a bare-id `patient` value cross a tenant, widen a
match, reach another reference type, echo the submitted value, or skip its
audit row. All data is synthetic.
"""

import json

import pytest

from r6.search_fidelity import normalize_patient_ref

PATHS = ('/r6/fhir/Observation',
         '/r6/fhir/Observation/$stats',
         '/r6/fhir/Observation/$lastn')

CANARY = 'canary-zz-7731'


def _post(client, headers, obs_id, subject, value=100, extra=None):
    body = {
        'resourceType': 'Observation', 'id': obs_id, 'status': 'final',
        'code': {'coding': [{'code': '2339-0'}]},
        'subject': {'reference': subject},
        'valueQuantity': {'value': value, 'unit': 'mg/dL'},
    }
    body.update(extra or {})
    resp = client.post('/r6/fhir/Observation', data=json.dumps(body),
                       content_type='application/json',
                       headers={**headers, 'X-Human-Confirmed': 'true'})
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)


def _ids(resp):
    return {e['resource']['id'] for e in resp.get_json().get('entry', [])
            if e.get('resource', {}).get('resourceType') == 'Observation'}


def _stats_count(resp):
    return {p['name']: p for p in resp.get_json()['parameter']}[
        'count']['valueInteger']


@pytest.fixture
def other_write_headers(other_tenant_headers):
    return other_tenant_headers


# --- unit level: the normaliser itself -------------------------------------

@pytest.mark.parametrize('value', [
    '', 'a' * 65, 'Patient/' + 'a' * 65, 'x\n', 'Patient/x\n', '\nx',
    'x\r', 'x ', ' x', 'x%', 'x_', '%', '_', 'x"', 'x\\', "x'",
    'x/y', 'Patient/x/_history/1', 'Group/x', 'patient/x', 'PATIENT/x',
    '١٢', 'x٣', 'Ａ', 'Patient/١', 'x\x00',
    'Patient/x"', 'Patient/x","reference":"Patient/y',
    'urn:uuid:x', 'http://h/Patient/x', 'Patient//x', '/x',
])
def test_normaliser_refuses(value):
    assert normalize_patient_ref(value) is None


@pytest.mark.parametrize('value,expected', [
    ('x', 'Patient/x'),
    ('.', 'Patient/.'),
    ('..', 'Patient/..'),
    ('a' * 64, 'Patient/' + 'a' * 64),
    ('Patient/x', 'Patient/x'),
    # A bare id that *is* 'Patient' is read as an id, not a type.
    ('Patient', 'Patient/Patient'),
])
def test_normaliser_accepts(value, expected):
    assert normalize_patient_ref(value) == expected


# --- tenant isolation -------------------------------------------------------

def test_bare_id_of_other_tenant_patient_returns_nothing(
        client, auth_headers, tenant_headers, other_write_headers):
    # Same patient id in both tenants; each tenant sees only its own rows.
    _post(client, auth_headers, 's861-mine', 'Patient/s861-shared', 10)
    _post(client, other_write_headers, 's861-theirs', 'Patient/s861-shared', 999)
    _post(client, other_write_headers, 's861-only-theirs',
          'Patient/s861-foreign', 555)

    for value in ('s861-shared', 'Patient/s861-shared'):
        resp = client.get(f'/r6/fhir/Observation?patient={value}',
                          headers=tenant_headers)
        assert resp.status_code == 200
        assert _ids(resp) == {'s861-mine'}, value

        resp = client.get(
            f'/r6/fhir/Observation/$stats?code=2339-0&patient={value}',
            headers=tenant_headers)
        assert resp.status_code == 200
        assert _stats_count(resp) == 1, value

        resp = client.get(
            f'/r6/fhir/Observation/$lastn?max=100&patient={value}',
            headers=tenant_headers)
        assert resp.status_code == 200
        assert _ids(resp) == {'s861-mine'}, value

    # A patient that exists only in the other tenant: empty, not 403/404
    # (no existence oracle) and certainly not their rows.
    for path in PATHS:
        resp = client.get(f'{path}?patient=s861-foreign',
                          headers=tenant_headers)
        assert resp.status_code == 200, path
        body = resp.get_data(as_text=True)
        assert 's861-only-theirs' not in body, path
        assert '555' not in body, path


def test_other_tenant_cannot_reach_mine_with_bare_id(
        client, auth_headers, other_tenant_headers):
    _post(client, auth_headers, 's861-private', 'Patient/s861-victim', 42)
    for path in PATHS:
        resp = client.get(f'{path}?patient=s861-victim',
                          headers=other_tenant_headers)
        assert resp.status_code == 200, path
        assert 's861-private' not in resp.get_data(as_text=True), path
    resp = client.get('/r6/fhir/Observation/$stats?patient=s861-victim',
                      headers=other_tenant_headers)
    assert _stats_count(resp) == 0


# --- match widening ----------------------------------------------------------

def test_bare_id_is_exact_not_prefix_or_wildcard(
        client, auth_headers, tenant_headers):
    _post(client, auth_headers, 's861-p1', 'Patient/s861-1', 1)
    _post(client, auth_headers, 's861-p12', 'Patient/s861-12', 2)
    _post(client, auth_headers, 's861-px', 'Patient/s861a1', 3)
    for path in PATHS:
        resp = client.get(f'{path}?patient=s861-1', headers=tenant_headers)
        assert resp.status_code == 200
        if path.endswith('$stats'):
            assert _stats_count(resp) == 1
        else:
            assert _ids(resp) == {'s861-p1'}, path
    # '.' and '-' are not LIKE wildcards; '%' and '_' are refused outright.
    for path in PATHS:
        for value in ('.', '..', '%25', '_', 's861_1', 's861%251'):
            resp = client.get(f'{path}?patient={value}',
                              headers=tenant_headers)
            body = resp.get_data(as_text=True)
            for leaked in ('s861-p1', 's861-p12', 's861-px'):
                assert leaked not in body, (path, value)


def test_bare_id_does_not_reach_other_reference_types(
        client, auth_headers, tenant_headers):
    # The id appears only as a Group / Practitioner / absolute reference.
    _post(client, auth_headers, 's861-grp', 'Group/s861-g', 7)
    _post(client, auth_headers, 's861-prac', 'Patient/s861-someone', 8,
          extra={'performer': [{'reference': 'Practitioner/s861-g'}]})
    _post(client, auth_headers, 's861-abs',
          'https://other.example/fhir/Patient/s861-g', 9)
    for path in PATHS:
        resp = client.get(f'{path}?patient=s861-g', headers=tenant_headers)
        assert resp.status_code == 200, path
        body = resp.get_data(as_text=True)
        for leaked in ('s861-grp', 's861-prac', 's861-abs'):
            assert leaked not in body, (path, leaked)


# --- echo and audit ---------------------------------------------------------

@pytest.mark.parametrize('raw', [
    f'Practitioner/{CANARY}', f'{CANARY}%0A', f'{CANARY}%20x',
    f'{"a" * 65}{CANARY}', f'http://h/Patient/{CANARY}',
    f'Patient/{CANARY}%22', f'{CANARY}%25',
])
def test_rejected_value_never_echoed(client, tenant_headers, raw):
    from r6.models import AuditEventRecord
    for path in PATHS:
        resp = client.get(f'{path}?patient={raw}', headers=tenant_headers)
        assert resp.status_code == 400, (path, raw)
        assert CANARY not in resp.get_data(as_text=True), (path, raw)
    for row in AuditEventRecord.query.all():
        assert CANARY not in (row.detail or ''), raw
        assert CANARY not in (row.resource_id or ''), raw


def test_bare_id_search_writes_one_phi_free_audit_row(
        client, auth_headers, tenant_headers):
    from r6.models import AuditEventRecord
    _post(client, auth_headers, 's861-aud', f'Patient/{CANARY}', 5)
    for path in PATHS:
        before = AuditEventRecord.query.count()
        resp = client.get(f'{path}?patient={CANARY}', headers=tenant_headers)
        assert resp.status_code == 200, path
        rows = AuditEventRecord.query.order_by(
            AuditEventRecord.recorded.desc()
            if hasattr(AuditEventRecord, 'recorded')
            else AuditEventRecord.id).all()
        assert AuditEventRecord.query.count() == before + 1, path
        for row in rows:
            if row.event_type == 'read':
                assert CANARY not in (row.detail or ''), (path, row.detail)
                assert CANARY not in (row.resource_id or ''), path


def test_search_rejection_is_audited_but_stats_lastn_rejection_is_not(
        client, tenant_headers):
    """Pins current behaviour (pre-existing, not introduced by #861):
    search audits a rejected `patient`; $stats and $lastn do not."""
    from r6.models import AuditEventRecord
    counts = {}
    for path in PATHS:
        before = AuditEventRecord.query.count()
        client.get(f'{path}?patient=Group/x', headers=tenant_headers)
        counts[path] = AuditEventRecord.query.count() - before
    assert counts['/r6/fhir/Observation'] == 1
    assert counts['/r6/fhir/Observation/$stats'] == 0
    assert counts['/r6/fhir/Observation/$lastn'] == 0
