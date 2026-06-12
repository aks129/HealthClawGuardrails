"""Executor tests — simulation mode (no keys) and mocked provider contracts."""
from unittest.mock import patch, MagicMock

from r6.actions.executors import execute_action, ExecutionResult


def test_phone_call_simulated_without_key(monkeypatch):
    monkeypatch.delenv('BLAND_AI_API_KEY', raising=False)
    result = execute_action(
        kind='phone-call',
        payload={'phone': '617-555-0100', 'body': 'script', 'to': 'CVS'},
    )
    assert isinstance(result, ExecutionResult)
    assert result.simulated is True
    assert result.ok is True
    assert result.external_ref.startswith('sim-')


def test_sms_simulated_without_keys(monkeypatch):
    for var in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER'):
        monkeypatch.delenv(var, raising=False)
    result = execute_action(kind='sms', payload={'phone': '+16175550100', 'body': 'hi'})
    assert result.simulated is True
    assert result.ok is True


def test_phone_call_real_contract(monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://app.healthclaw.io')
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {'call_id': 'bl-123'}
    with patch('r6.actions.executors.requests.post', return_value=fake) as post:
        result = execute_action(
            kind='phone-call',
            payload={'phone': '617-555-0100', 'body': 'script', 'to': 'CVS'},
            action_id='act-1',
        )
    assert result.ok is True
    assert result.simulated is False
    assert result.external_ref == 'bl-123'
    args, kwargs = post.call_args
    assert args[0] == 'https://api.bland.ai/v1/calls'
    assert kwargs['headers']['Authorization'] == 'test-key'
    assert kwargs['json']['phone_number'] == '617-555-0100'
    assert kwargs['json']['task'] == 'script'
    assert 'act-1' in kwargs['json']['webhook']


def test_phone_call_provider_error(monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    fake = MagicMock()
    fake.status_code = 502
    fake.text = 'upstream error'
    with patch('r6.actions.executors.requests.post', return_value=fake):
        result = execute_action(
            kind='phone-call',
            payload={'phone': '617-555-0100', 'body': 'script'},
        )
    assert result.ok is False
    assert result.simulated is False
    assert 'upstream error' not in (result.error or '')
    assert len(result.error) < 200


def test_missing_phone_fails_fast():
    result = execute_action(kind='phone-call', payload={'body': 'script'})
    assert result.ok is False
    assert 'phone' in result.error


def test_phone_call_timeout_is_outcome_unknown(monkeypatch):
    import requests as req
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    with patch('r6.actions.executors.requests.post', side_effect=req.Timeout('slow')):
        result = execute_action(
            kind='phone-call',
            payload={'phone': '617-555-0100', 'body': 'script'},
        )
    assert result.ok is False
    assert result.outcome_unknown is True


def test_partial_twilio_config_fails_not_simulates(monkeypatch):
    monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'sid')
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'tok')
    monkeypatch.delenv('TWILIO_FROM_NUMBER', raising=False)
    result = execute_action(kind='sms', payload={'phone': '+1', 'body': 'hi'})
    assert result.ok is False
    assert result.simulated is False
    assert 'misconfigured' in result.error


def test_connection_error_is_outcome_unknown(monkeypatch):
    import requests as req
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    with patch('r6.actions.executors.requests.post',
               side_effect=req.ConnectionError('reset')):
        result = execute_action(
            kind='phone-call',
            payload={'phone': '617-555-0100', 'body': 'script'},
        )
    assert result.ok is False
    assert result.outcome_unknown is True
