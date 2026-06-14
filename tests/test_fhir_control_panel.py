"""
Tests for FHIR control panel aggregate endpoints:
  - GET /r6/fhir/$inventory
  - GET /r6/fhir/$profile-adherence

Both are read-only, tenant-scoped, and emit counts-only audit events
(no PHI in the audit detail).
"""

import json

from r6.models import R6Resource, AuditEventRecord
from models import db


def _seed(resource_type, resource, tenant_id):
    """Persist a single FHIR resource for a tenant via R6Resource directly."""
    row = R6Resource(
        resource_type=resource_type,
        resource_json=json.dumps(resource),
        tenant_id=tenant_id,
    )
    db.session.add(row)
    return row


def _param(parameters, name):
    """Pluck a parameter dict by name from a Parameters resource."""
    for p in parameters:
        if p.get('name') == name:
            return p
    return None


# --- $inventory ---------------------------------------------------------


def test_inventory_requires_tenant(client):
    resp = client.get('/r6/fhir/$inventory')
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['resourceType'] == 'OperationOutcome'
    assert 'X-Tenant-Id' in body['issue'][0]['diagnostics']


def test_inventory_grouped_counts(client, app, tenant_headers, tenant_id):
    with app.app_context():
        for i in range(2):
            _seed('Patient', {'resourceType': 'Patient', 'id': f'p{i}',
                              'gender': 'male'}, tenant_id)
        for i in range(3):
            _seed('Observation', {'resourceType': 'Observation', 'id': f'o{i}',
                                 'status': 'final',
                                 'code': {'coding': [{'code': 'x'}]}}, tenant_id)
        db.session.commit()

    resp = client.get('/r6/fhir/$inventory', headers=tenant_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['resourceType'] == 'Parameters'

    params = body['parameter']
    assert _param(params, 'tenant')['valueString'] == tenant_id
    assert _param(params, 'total')['valueInteger'] == 5
    assert _param(params, 'lastUpdated')['valueDateTime'].endswith('Z')

    by_type = _param(params, 'byType')['part']
    counts = {p['name']: p['valueInteger'] for p in by_type}
    assert counts == {'Observation': 3, 'Patient': 2}
    # Sorted desc by count: Observation (3) before Patient (2).
    assert [p['name'] for p in by_type] == ['Observation', 'Patient']


def test_inventory_tenant_isolation(client, app, tenant_headers, tenant_id):
    with app.app_context():
        _seed('Patient', {'resourceType': 'Patient', 'id': 'mine'}, tenant_id)
        _seed('Observation', {'resourceType': 'Observation', 'id': 'other',
                             'status': 'final',
                             'code': {'coding': [{'code': 'x'}]}},
              'some-other-tenant')
        db.session.commit()

    resp = client.get('/r6/fhir/$inventory', headers=tenant_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    params = body['parameter']
    assert _param(params, 'total')['valueInteger'] == 1
    by_type = _param(params, 'byType')['part']
    counts = {p['name']: p['valueInteger'] for p in by_type}
    assert counts == {'Patient': 1}
    assert 'Observation' not in counts


def test_inventory_emits_audit_no_phi(client, app, tenant_headers, tenant_id):
    with app.app_context():
        _seed('Patient', {'resourceType': 'Patient', 'id': 'p0',
                          'name': [{'family': 'Secret', 'given': ['Person']}]},
              tenant_id)
        db.session.commit()

    resp = client.get('/r6/fhir/$inventory', headers=tenant_headers)
    assert resp.status_code == 200

    with app.app_context():
        events = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_id='inventory'
        ).all()
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == 'read'
        assert ev.resource_type == 'Parameters'
        # Counts only — no PHI (names) leaked into detail.
        assert 'Secret' not in (ev.detail or '')
        assert 'Person' not in (ev.detail or '')


# --- $profile-adherence -------------------------------------------------


def test_profile_adherence_requires_tenant(client):
    resp = client.get('/r6/fhir/$profile-adherence')
    assert resp.status_code == 400


def test_profile_adherence_per_type(client, app, tenant_headers, tenant_id):
    with app.app_context():
        # One conformant Observation (status + code present).
        _seed('Observation', {'resourceType': 'Observation', 'id': 'good',
                             'status': 'final',
                             'code': {'coding': [{'code': 'x'}]}}, tenant_id)
        # One non-conformant Observation (missing status).
        _seed('Observation', {'resourceType': 'Observation', 'id': 'bad',
                             'code': {'coding': [{'code': 'x'}]}}, tenant_id)
        db.session.commit()

    resp = client.get('/r6/fhir/$profile-adherence', headers=tenant_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['resourceType'] == 'Parameters'

    params = body['parameter']
    assert _param(params, 'tenant')['valueString'] == tenant_id
    # 1 of 2 conformant overall.
    assert _param(params, 'overallAdherence')['valueDecimal'] == 0.5

    by_type = _param(params, 'byType')['part']
    obs = next(p for p in by_type if p['name'] == 'Observation')
    obs_parts = obs['part']
    assert _param(obs_parts, 'total')['valueInteger'] == 2
    assert _param(obs_parts, 'sampled')['valueInteger'] == 2
    assert _param(obs_parts, 'conformant')['valueInteger'] == 1
    assert _param(obs_parts, 'adherence')['valueDecimal'] == 0.5
    top_issues = _param(obs_parts, 'topIssues')['valueString']
    assert 'Observation.status is required' in top_issues
    assert '(1)' in top_issues


def test_profile_adherence_emits_audit_no_phi(client, app, tenant_headers,
                                              tenant_id):
    with app.app_context():
        _seed('Patient', {'resourceType': 'Patient', 'id': 'p0',
                          'name': [{'family': 'Secret', 'given': ['Person']}]},
              tenant_id)
        db.session.commit()

    resp = client.get('/r6/fhir/$profile-adherence', headers=tenant_headers)
    assert resp.status_code == 200

    with app.app_context():
        events = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_id='profile-adherence'
        ).all()
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == 'read'
        assert ev.resource_type == 'Parameters'
        assert 'Secret' not in (ev.detail or '')
        assert 'Person' not in (ev.detail or '')
