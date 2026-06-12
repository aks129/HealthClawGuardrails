"""Provider webhook callbacks — secret verification + status resolution."""
from r6.actions.models import ProposedAction

PROPOSE_BODY = {
    'kind': 'phone-call',
    'payload': {'to': 'CVS', 'phone': '617-555-0100', 'body': 'script'},
}


def _executing_action(client, tenant_headers, app):
    resp = client.post('/r6/actions/propose', json=PROPOSE_BODY,
                       headers=tenant_headers)
    action_id = resp.get_json()['id']
    with app.app_context():
        from models import db
        row = db.session.get(ProposedAction, action_id)
        row.transition('confirmed')
        row.transition('executing')
        row.external_ref = 'bl-123'
        db.session.commit()
    return action_id


def test_bland_callback_requires_secret(client, tenant_headers, app, monkeypatch):
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    action_id = _executing_action(client, tenant_headers, app)
    resp = client.post(
        '/r6/actions/callback/bland?action_id=%s&secret=wrong' % action_id,
        json={'call_id': 'bl-123', 'status': 'completed',
              'summary': 'Refill confirmed'})
    assert resp.status_code == 403


def test_callback_rejected_when_secret_unconfigured(client, tenant_headers, app,
                                                    monkeypatch):
    monkeypatch.delenv('ACTIONS_WEBHOOK_SECRET', raising=False)
    action_id = _executing_action(client, tenant_headers, app)
    resp = client.post(
        '/r6/actions/callback/bland?action_id=%s&secret=' % action_id,
        json={'status': 'completed'})
    assert resp.status_code == 403


def test_bland_callback_completes_action(client, tenant_headers, app, monkeypatch):
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    action_id = _executing_action(client, tenant_headers, app)
    resp = client.post(
        '/r6/actions/callback/bland?action_id=%s&secret=hook-secret' % action_id,
        json={'call_id': 'bl-123', 'status': 'completed',
              'summary': 'Refill confirmed, ready after 3pm'})
    assert resp.status_code == 200
    with app.app_context():
        from models import db
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'completed'
        assert 'ready after 3pm' in row.outcome_summary


def test_bland_callback_failed_call(client, tenant_headers, app, monkeypatch):
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    action_id = _executing_action(client, tenant_headers, app)
    resp = client.post(
        '/r6/actions/callback/bland?action_id=%s&secret=hook-secret' % action_id,
        json={'call_id': 'bl-123', 'status': 'failed', 'summary': 'no answer'})
    assert resp.status_code == 200
    with app.app_context():
        from models import db
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'failed'


def test_callback_duplicate_is_noop(client, tenant_headers, app, monkeypatch):
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    action_id = _executing_action(client, tenant_headers, app)
    url = '/r6/actions/callback/bland?action_id=%s&secret=hook-secret' % action_id
    first = client.post(url, json={'status': 'completed', 'summary': 'done'})
    assert first.status_code == 200
    second = client.post(url, json={'status': 'failed', 'summary': 'late dupe'})
    assert second.status_code == 200
    with app.app_context():
        from models import db
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'completed'  # first verdict wins
        assert 'late dupe' not in (row.outcome_summary or '')


def test_callback_notifies_tenant_summary_only(client, tenant_headers, app,
                                               monkeypatch):
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    sent = {}

    def fake_notify(tenant_id, message, parse_mode='Markdown'):
        sent['tenant'] = tenant_id
        sent['message'] = message
        return 1

    monkeypatch.setattr('r6.actions.routes.notify_tenant', fake_notify)
    action_id = _executing_action(client, tenant_headers, app)
    client.post(
        '/r6/actions/callback/bland?action_id=%s&secret=hook-secret' % action_id,
        json={'call_id': 'bl-123', 'status': 'completed', 'summary': 'done'})
    assert sent['tenant'] == tenant_headers['X-Tenant-Id']
    # PHI-safe: recipient label OK, phone number NOT
    assert '617-555-0100' not in sent['message']
