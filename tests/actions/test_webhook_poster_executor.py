"""webhook-poster cookbook rail tests."""

from r6.actions import errors
from r6.actions.models import VALID_KINDS
from r6.actions.registry import all_kinds, get_executor


class _Action:
    def __init__(self, payload, action_id='test-action-1'):
        self.payload = payload
        self.external_ref = None
        self.id = action_id


class _Resp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def _payload():
    return {
        'to': 'Sandbox receiver',
        'body': 'Synthetic cookbook payload only.',
        'metadata': {'fixture': 'cookbook'},
    }


def test_webhook_poster_is_registered_and_proposable(action_registry):
    assert 'webhook-poster' in all_kinds()
    assert 'webhook-poster' in VALID_KINDS


def test_required_env_documents_provider_config(action_registry):
    ex = get_executor('webhook-poster')
    assert ex.required_env == ('WEBHOOK_POSTER_URL', 'WEBHOOK_POSTER_TOKEN')


def test_validate_rejects_empty_payload(action_registry):
    ex = get_executor('webhook-poster')
    assert ex.validate({}) == [errors.PAYLOAD_INVALID]


def test_validate_accepts_synthetic_payload(action_registry):
    ex = get_executor('webhook-poster')
    assert ex.validate(_payload()) == []


def test_validate_rejects_non_object_metadata(action_registry):
    ex = get_executor('webhook-poster')
    payload = _payload()
    payload['metadata'] = 'not an object'
    assert ex.validate(payload) == [errors.PAYLOAD_INVALID]


def test_execute_without_config_fails_loud(action_registry, monkeypatch):
    monkeypatch.delenv('WEBHOOK_POSTER_URL', raising=False)
    monkeypatch.delenv('WEBHOOK_POSTER_TOKEN', raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError('provider should not be called without config')
    monkeypatch.setattr('requests.request', _boom)

    ex = get_executor('webhook-poster')
    result = ex.execute(_Action(_payload()))
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_NOT_CONFIGURED


def test_execute_with_invalid_url_fails_loud(action_registry, monkeypatch):
    monkeypatch.setenv('WEBHOOK_POSTER_URL', 'not-a-url')
    monkeypatch.setenv('WEBHOOK_POSTER_TOKEN', 'secret')

    def _boom(*args, **kwargs):
        raise AssertionError('provider should not be called with invalid URL')
    monkeypatch.setattr('requests.request', _boom)

    ex = get_executor('webhook-poster')
    result = ex.execute(_Action(_payload()))
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_NOT_CONFIGURED


def test_execute_posts_synthetic_payload(action_registry, monkeypatch):
    monkeypatch.setenv('WEBHOOK_POSTER_URL',
                       'https://webhook.example.test/actions')
    monkeypatch.setenv('WEBHOOK_POSTER_TOKEN', 'secret-token')
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Resp(202, headers={'X-Request-Id': 'req-123'})
    monkeypatch.setattr('requests.request', fake_request)

    ex = get_executor('webhook-poster')
    result = ex.execute(_Action(_payload(), action_id='act-123'))

    assert result.status == 'completed'
    assert result.provider_ref == 'req-123'
    assert result.outcome == {'status_code': 202}
    method, url, kwargs = calls[0]
    assert method == 'POST'
    assert url == 'https://webhook.example.test/actions'
    assert kwargs['headers']['Authorization'] == 'Bearer secret-token'
    assert kwargs['json'] == {
        'action_id': 'act-123',
        'kind': 'webhook-poster',
        'to': 'Sandbox receiver',
        'body': 'Synthetic cookbook payload only.',
        'metadata': {'fixture': 'cookbook'},
    }


def test_execute_provider_500_is_unknown(action_registry, monkeypatch):
    monkeypatch.setenv('WEBHOOK_POSTER_URL',
                       'https://webhook.example.test/actions')
    monkeypatch.setenv('WEBHOOK_POSTER_TOKEN', 'secret-token')
    monkeypatch.setattr('requests.request',
                        lambda *args, **kwargs: _Resp(500))

    ex = get_executor('webhook-poster')
    result = ex.execute(_Action(_payload()))
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_ERROR
    assert result.outcome_unknown is True


def test_reconcile_is_honest_needs_review(action_registry):
    ex = get_executor('webhook-poster')
    result = ex.reconcile(_Action(_payload()))
    assert result.status == 'needs_review'
