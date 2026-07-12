"""Action routes — propose / commit(submit) / confirm / status / callback.

Commit is submit-for-confirmation (202, nothing executes); the human's
out-of-band POST /<id>/confirm is what executes. See
tests/actions/test_confirm_is_commit.py for the full Approve-is-the-commit
contract; this module keeps the route-level basics (auth, expiry,
double-submit, tenant isolation, audit hygiene).
"""
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


def test_status_hides_payload_without_step_up(client, tenant_headers):
    """H4: a tenant-header-only status read must NOT leak the callee phone
    number or the free-text message body — only the PHI-safe summary."""
    action_id = _propose(client, tenant_headers)
    resp = client.get(f'/r6/actions/{action_id}', headers=tenant_headers)
    assert resp.status_code == 200
    d = resp.get_json()
    assert 'payload' not in d
    blob = json.dumps(d)
    assert '617-555-0100' not in blob   # phone
    assert 'metformin' not in blob      # message body
    assert d['status'] == 'proposed'
    assert d['id'] == action_id
    assert d['to'] == 'CVS Pharmacy'    # recipient label is fine


def test_status_shows_payload_with_step_up(client, tenant_headers, auth_headers):
    action_id = _propose(client, tenant_headers)
    resp = client.get(f'/r6/actions/{action_id}', headers=auth_headers)
    assert resp.status_code == 200
    assert 'payload' in resp.get_json()


def test_commit_requires_step_up(client, tenant_headers):
    action_id = _propose(client, tenant_headers)
    resp = client.post('/r6/actions/%s/commit' % action_id,
                       headers=tenant_headers)
    assert resp.status_code == 401


def test_commit_submits_for_confirmation(client, tenant_headers, auth_headers,
                                         app):
    """Commit is submit-only: 202, awaiting_confirmation, audited."""
    action_id = _propose(client, tenant_headers)
    resp = client.post('/r6/actions/%s/commit' % action_id,
                       headers=auth_headers)
    assert resp.status_code == 202
    data = resp.get_json()
    assert data['status'] == 'awaiting_confirmation'
    assert 'next_step' in data
    with app.app_context():
        from models import db
        from r6.models import AuditEventRecord
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'awaiting_confirmation'
        commits = AuditEventRecord.query.filter_by(
            event_type='update', resource_type='ProposedAction',
            resource_id=action_id).all()
        assert len(commits) == 1  # proposed -> awaiting_confirmation


def test_commit_then_confirm_executes(client, tenant_headers, auth_headers,
                                      app, action_registry, fake_providers,
                                      monkeypatch):
    """Full happy path: submit executes nothing; the out-of-band Approve is
    what dials the provider (once)."""
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _propose(client, tenant_headers)
    resp = client.post('/r6/actions/%s/commit' % action_id,
                       headers=auth_headers)
    assert resp.status_code == 202
    assert fake_providers == []
    resp = client.post('/r6/actions/%s/confirm' % action_id,
                       headers=auth_headers, json={})
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'executing'
    assert len(fake_providers) == 1
    with app.app_context():
        from r6.models import AuditEventRecord
        details = [e.detail or '' for e in AuditEventRecord.query.filter_by(
            resource_type='ProposedAction', resource_id=action_id).all()]
        assert any('approved via dashboard' in d for d in details)


def test_commit_expired_returns_410(client, tenant_headers, auth_headers, app):
    from datetime import datetime, timedelta, timezone
    action_id = _propose(client, tenant_headers)
    with app.app_context():
        from models import db
        row = db.session.get(ProposedAction, action_id)
        row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.session.commit()
    resp = client.post('/r6/actions/%s/commit' % action_id,
                       headers=auth_headers)
    assert resp.status_code == 410


def test_commit_double_commit_conflict(client, tenant_headers, auth_headers):
    action_id = _propose(client, tenant_headers)
    first = client.post('/r6/actions/%s/commit' % action_id,
                        headers=auth_headers)
    assert first.status_code == 202
    second = client.post('/r6/actions/%s/commit' % action_id,
                         headers=auth_headers)
    assert second.status_code == 409


def test_commit_wrong_tenant_404(client, tenant_headers, auth_headers):
    action_id = _propose(client, tenant_headers)
    headers = dict(auth_headers)
    headers['X-Tenant-Id'] = 'other-tenant'
    # step-up token is tenant-bound, so this fails at validation -> 401
    resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code in (401, 404)


def _submit(client, tenant_headers, auth_headers):
    """propose + commit -> awaiting_confirmation; returns action_id."""
    action_id = _propose(client, tenant_headers)
    resp = client.post('/r6/actions/%s/commit' % action_id,
                       headers=auth_headers)
    assert resp.status_code == 202
    return action_id


def test_confirm_outcome_unknown_maps_to_unknown_status(
        client, tenant_headers, auth_headers, app, action_registry,
        monkeypatch):
    import requests as req
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _submit(client, tenant_headers, auth_headers)

    def timeout(method, url, **kw):
        raise req.Timeout('slow')

    monkeypatch.setattr('requests.request', timeout)
    resp = client.post('/r6/actions/%s/confirm' % action_id,
                       headers=auth_headers, json={})
    assert resp.status_code == 502
    with app.app_context():
        from models import db
        row = db.session.get(ProposedAction, action_id)
        # NEVER 'failed' on ambiguity — re-propose could double-place the call
        assert row.status == 'unknown'


def test_confirm_4xx_provider_error_is_failed(client, tenant_headers,
                                              auth_headers, app,
                                              action_registry, monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _submit(client, tenant_headers, auth_headers)

    class _Resp:
        status_code = 400

    monkeypatch.setattr('requests.request', lambda method, url, **kw: _Resp())
    resp = client.post('/r6/actions/%s/confirm' % action_id,
                       headers=auth_headers, json={})
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


def test_confirm_5xx_provider_error_is_unknown(client, tenant_headers,
                                               auth_headers, app,
                                               action_registry, monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    action_id = _submit(client, tenant_headers, auth_headers)

    class _Resp:
        status_code = 503

    monkeypatch.setattr('requests.request', lambda method, url, **kw: _Resp())
    resp = client.post('/r6/actions/%s/confirm' % action_id,
                       headers=auth_headers, json={})
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
