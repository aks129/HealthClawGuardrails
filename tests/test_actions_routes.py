"""Action routes — propose / commit / status / callback."""
import json

from r6.actions.models import ProposedAction


PROPOSE_BODY = {
    'kind': 'phone-call',
    'payload': {
        'to': 'CVS Pharmacy',
        'phone': '617-555-0100',
        'body': 'Hi, calling for a refill of metformin 500mg for John Smith.',
    },
}


def test_propose_requires_tenant(client):
    resp = client.post('/r6/actions/propose', json=PROPOSE_BODY)
    assert resp.status_code == 400


def test_propose_creates_action(client, tenant_headers, app):
    resp = client.post('/r6/actions/propose', json=PROPOSE_BODY,
                       headers=tenant_headers)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['status'] == 'proposed'
    assert data['payload']['body'].startswith('Hi, calling')
    with app.app_context():
        row = ProposedAction.query.get(data['id'])
        assert row is not None
        assert row.tenant_id == tenant_headers['X-Tenant-Id']


def test_propose_rejects_bad_kind(client, tenant_headers):
    resp = client.post('/r6/actions/propose',
                       json={'kind': 'teleport', 'payload': {}},
                       headers=tenant_headers)
    assert resp.status_code == 400


def test_propose_emits_audit_event(client, tenant_headers, app):
    client.post('/r6/actions/propose', json=PROPOSE_BODY, headers=tenant_headers)
    with app.app_context():
        from r6.models import AuditEventRecord
        events = AuditEventRecord.query.filter_by(
            tenant_id=tenant_headers['X-Tenant-Id'],
            resource_type='ProposedAction').all()
        assert len(events) == 1
        # PHI-safe: no script text or phone number in audit detail
        assert '617-555-0100' not in (events[0].detail or '')
        assert 'metformin' not in (events[0].detail or '')


def test_propose_non_object_body_returns_400(client, tenant_headers):
    resp = client.post('/r6/actions/propose', data='[1,2]',
                       content_type='application/json', headers=tenant_headers)
    assert resp.status_code == 400


def test_propose_non_string_body_returns_400(client, tenant_headers):
    resp = client.post('/r6/actions/propose',
                       json={'kind': 'sms', 'payload': {'body': {'x': 1}}},
                       headers=tenant_headers)
    assert resp.status_code == 400


def test_propose_oversize_payload_returns_400(client, tenant_headers):
    resp = client.post('/r6/actions/propose',
                       json={'kind': 'sms', 'payload': {'body': 'x' * 70000}},
                       headers=tenant_headers)
    assert resp.status_code == 400
