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


# ---------------------------------------------------------------------------
# Commit route tests
# ---------------------------------------------------------------------------

def _propose(client, tenant_headers):
    resp = client.post('/r6/actions/propose', json=PROPOSE_BODY,
                       headers=tenant_headers)
    return resp.get_json()['id']


def test_commit_requires_step_up(client, tenant_headers):
    action_id = _propose(client, tenant_headers)
    resp = client.post('/r6/actions/%s/commit' % action_id,
                       headers=tenant_headers)
    assert resp.status_code == 401


def test_commit_requires_human_confirmation(client, tenant_headers, auth_headers):
    action_id = _propose(client, tenant_headers)
    # auth_headers has step-up but NOT X-Human-Confirmed
    resp = client.post('/r6/actions/%s/commit' % action_id,
                       headers=auth_headers)
    assert resp.status_code == 428


def test_commit_executes_in_simulation(client, tenant_headers, auth_headers,
                                        app, monkeypatch):
    monkeypatch.delenv('BLAND_AI_API_KEY', raising=False)
    monkeypatch.delenv('BLAND_API_KEY', raising=False)
    action_id = _propose(client, tenant_headers)
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code == 200
    data = resp.get_json()
    # simulation completes synchronously
    assert data['status'] == 'completed'
    assert data['simulated'] is True
    with app.app_context():
        from r6.models import AuditEventRecord
        commits = AuditEventRecord.query.filter_by(
            event_type='update', resource_type='ProposedAction',
            resource_id=action_id).all()
        assert len(commits) == 2  # claim->executing + completed


def test_commit_expired_returns_410(client, tenant_headers, auth_headers, app):
    from datetime import datetime, timedelta, timezone
    action_id = _propose(client, tenant_headers)
    with app.app_context():
        from models import db
        row = db.session.get(ProposedAction, action_id)
        row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.session.commit()
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code == 410


def test_commit_double_commit_conflict(client, tenant_headers, auth_headers,
                                        monkeypatch):
    monkeypatch.delenv('BLAND_AI_API_KEY', raising=False)
    monkeypatch.delenv('BLAND_API_KEY', raising=False)
    action_id = _propose(client, tenant_headers)
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    first = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert first.status_code == 200
    second = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert second.status_code == 409


def test_commit_wrong_tenant_404(client, tenant_headers, auth_headers):
    action_id = _propose(client, tenant_headers)
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    headers['X-Tenant-Id'] = 'other-tenant'
    # step-up token is tenant-bound, so this fails at validation -> 401
    resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code in (401, 404)


def test_commit_outcome_unknown_maps_to_unknown_status(client, tenant_headers,
                                                       auth_headers, app, monkeypatch):
    import requests as req
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _propose(client, tenant_headers)
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    from unittest.mock import patch as mock_patch
    with mock_patch('r6.actions.executors.requests.post', side_effect=req.Timeout('slow')):
        resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code == 502
    with app.app_context():
        from models import db
        row = db.session.get(ProposedAction, action_id)
        # NEVER 'failed' on ambiguity — re-propose could double-place the call
        assert row.status == 'unknown'


def test_commit_4xx_provider_error_is_failed(client, tenant_headers,
                                             auth_headers, app, monkeypatch):
    from unittest.mock import patch as mock_patch, MagicMock
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _propose(client, tenant_headers)
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    fake = MagicMock(); fake.status_code = 400
    with mock_patch('r6.actions.executors.requests.post', return_value=fake):
        resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code == 502
    with app.app_context():
        from models import db
        from r6.models import AuditEventRecord
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'failed'
        failures = AuditEventRecord.query.filter_by(
            resource_id=action_id, outcome='failure').all()
        assert len(failures) == 1
        assert '617-555-0100' not in (failures[0].detail or '')


def test_commit_5xx_provider_error_is_unknown(client, tenant_headers,
                                              auth_headers, app, monkeypatch):
    from unittest.mock import patch as mock_patch, MagicMock
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _propose(client, tenant_headers)
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    fake = MagicMock(); fake.status_code = 503
    with mock_patch('r6.actions.executors.requests.post', return_value=fake):
        resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code == 502
    with app.app_context():
        from models import db
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'unknown'


# ---------------------------------------------------------------------------
# Status route tests
# ---------------------------------------------------------------------------

def test_status_returns_action(client, tenant_headers):
    action_id = _propose(client, tenant_headers)
    resp = client.get('/r6/actions/%s' % action_id, headers=tenant_headers)
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'proposed'


def test_status_tenant_isolation(client, tenant_headers):
    action_id = _propose(client, tenant_headers)
    other = dict(tenant_headers)
    other['X-Tenant-Id'] = 'other-tenant'
    resp = client.get('/r6/actions/%s' % action_id, headers=other)
    assert resp.status_code == 404


def test_status_marks_overdue_expiry(client, tenant_headers, app):
    from datetime import datetime, timedelta, timezone
    action_id = _propose(client, tenant_headers)
    with app.app_context():
        from models import db
        row = db.session.get(ProposedAction, action_id)
        row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.session.commit()
    resp = client.get('/r6/actions/%s' % action_id, headers=tenant_headers)
    assert resp.get_json()['status'] == 'expired'
