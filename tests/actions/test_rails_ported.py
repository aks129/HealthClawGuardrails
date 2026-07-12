"""Tests for the new fail-loud rail executors (r6/actions/rails/phone.py,
r6/actions/rails/sms.py) — ported from the old executors.py provider
behavior but with NO silent simulation: missing credentials must fail loud.
"""

import pytest

from r6.actions import errors
from r6.actions.registry import _clear, all_kinds, get_executor


class _FakeAction:
    def __init__(self, payload, external_ref=None, action_id='test-action-1'):
        self.payload = payload
        self.external_ref = external_ref
        self.id = action_id


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Every test in this module gets a clean, fully-registered registry."""
    from r6.actions.rails import register_all
    _clear()
    register_all()
    yield
    _clear()
    register_all()


# --- registration ---

def test_phone_and_sms_kinds_registered():
    assert 'phone-call' in all_kinds()
    assert 'sms' in all_kinds()


def test_register_all_is_idempotent():
    from r6.actions.rails import register_all
    register_all()  # second call must not raise
    register_all()
    assert 'phone-call' in all_kinds()
    assert 'sms' in all_kinds()


# --- validate ---

def test_phone_validate_missing_phone():
    ex = get_executor('phone-call')
    assert ex.validate({'body': 'hello'}) == [errors.PAYLOAD_INVALID]


def test_phone_validate_missing_body():
    ex = get_executor('phone-call')
    assert ex.validate({'phone': '+15551234567'}) == [errors.PAYLOAD_INVALID]


def test_phone_validate_ok():
    ex = get_executor('phone-call')
    assert ex.validate({'phone': '+15551234567', 'body': 'hello'}) == []


def test_sms_validate_missing_phone():
    ex = get_executor('sms')
    assert ex.validate({'body': 'hello'}) == [errors.PAYLOAD_INVALID]


def test_sms_validate_missing_body():
    ex = get_executor('sms')
    assert ex.validate({'phone': '+15551234567'}) == [errors.PAYLOAD_INVALID]


def test_sms_validate_ok():
    ex = get_executor('sms')
    assert ex.validate({'phone': '+15551234567', 'body': 'hello'}) == []


# --- execute: no credentials -> fail loud, no network call ---

def test_phone_execute_no_credentials_fails_loud(monkeypatch):
    monkeypatch.delenv('BLAND_AI_API_KEY', raising=False)
    monkeypatch.delenv('BLAND_API_KEY', raising=False)

    def _boom(*a, **kw):
        raise AssertionError('requests.post should not be called without credentials')
    monkeypatch.setattr('requests.request', _boom)

    ex = get_executor('phone-call')
    action = _FakeAction({'phone': '+15551234567', 'body': 'hi'})
    result = ex.execute(action)
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_NOT_CONFIGURED


def test_sms_execute_no_credentials_fails_loud(monkeypatch):
    monkeypatch.delenv('TWILIO_ACCOUNT_SID', raising=False)
    monkeypatch.delenv('TWILIO_AUTH_TOKEN', raising=False)
    monkeypatch.delenv('TWILIO_FROM_NUMBER', raising=False)

    def _boom(*a, **kw):
        raise AssertionError('requests.post should not be called without credentials')
    monkeypatch.setattr('requests.request', _boom)

    ex = get_executor('sms')
    action = _FakeAction({'phone': '+15551234567', 'body': 'hi'})
    result = ex.execute(action)
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_NOT_CONFIGURED


def test_sms_execute_partially_configured_fails_loud(monkeypatch):
    monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'ACxxx')
    monkeypatch.delenv('TWILIO_AUTH_TOKEN', raising=False)
    monkeypatch.delenv('TWILIO_FROM_NUMBER', raising=False)
    monkeypatch.setattr('requests.request',
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError('should not call provider')))
    ex = get_executor('sms')
    result = ex.execute(_FakeAction({'phone': '+15551234567', 'body': 'hi'}))
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_NOT_CONFIGURED


# --- execute: keys set + mocked 200 -> executing with provider_ref ---

class _Resp200Phone:
    status_code = 200

    def json(self):
        return {'call_id': 'CALL123'}


class _Resp200Sms:
    status_code = 201

    def json(self):
        return {'sid': 'SM123'}


def test_phone_execute_success(monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'key-123')
    calls = []

    def fake_request(method, url, **kw):
        calls.append((method, url, kw))
        return _Resp200Phone()
    monkeypatch.setattr('requests.request', fake_request)

    ex = get_executor('phone-call')
    result = ex.execute(_FakeAction({'phone': '+15551234567', 'body': 'hi'}))
    assert result.status == 'executing'
    assert result.provider_ref == 'CALL123'
    assert calls[0][0] == 'POST'
    assert calls[0][1] == 'https://api.bland.ai/v1/calls'


def test_sms_execute_success(monkeypatch):
    monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'ACxxx')
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'tok')
    monkeypatch.setenv('TWILIO_FROM_NUMBER', '+15550000000')
    calls = []

    def fake_request(method, url, **kw):
        calls.append((method, url, kw))
        return _Resp200Sms()
    monkeypatch.setattr('requests.request', fake_request)

    ex = get_executor('sms')
    result = ex.execute(_FakeAction({'phone': '+15551234567', 'body': 'hi'}))
    assert result.status == 'executing'
    assert result.provider_ref == 'SM123'
    assert calls[0][0] == 'POST'
    assert 'ACxxx' in calls[0][1]


# --- execute: provider 500 -> failed, outcome_unknown=True ---

class _Resp500:
    status_code = 500

    def json(self):
        return {}


def test_phone_execute_provider_500(monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'key-123')
    monkeypatch.setattr('requests.request', lambda *a, **kw: _Resp500())
    ex = get_executor('phone-call')
    result = ex.execute(_FakeAction({'phone': '+15551234567', 'body': 'hi'}))
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_ERROR
    assert result.outcome_unknown is True


def test_sms_execute_provider_500(monkeypatch):
    monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'ACxxx')
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'tok')
    monkeypatch.setenv('TWILIO_FROM_NUMBER', '+15550000000')
    monkeypatch.setattr('requests.request', lambda *a, **kw: _Resp500())
    ex = get_executor('sms')
    result = ex.execute(_FakeAction({'phone': '+15551234567', 'body': 'hi'}))
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_ERROR
    assert result.outcome_unknown is True


# --- execute: other non-200 -> failed, outcome_unknown=False ---

class _Resp400:
    status_code = 400

    def json(self):
        return {}


def test_phone_execute_provider_400_not_outcome_unknown(monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'key-123')
    monkeypatch.setattr('requests.request', lambda *a, **kw: _Resp400())
    ex = get_executor('phone-call')
    result = ex.execute(_FakeAction({'phone': '+15551234567', 'body': 'hi'}))
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_ERROR
    assert result.outcome_unknown is False


# --- execute: transport failures ---

def test_phone_execute_timeout_is_outcome_unknown(monkeypatch):
    import requests
    monkeypatch.setenv('BLAND_AI_API_KEY', 'key-123')

    def raise_timeout(*a, **kw):
        raise requests.Timeout('timed out')
    monkeypatch.setattr('requests.request', raise_timeout)

    ex = get_executor('phone-call')
    result = ex.execute(_FakeAction({'phone': '+15551234567', 'body': 'hi'}))
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_ERROR
    assert result.outcome_unknown is True


def test_sms_execute_connection_error_is_outcome_unknown(monkeypatch):
    import requests
    monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'ACxxx')
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'tok')
    monkeypatch.setenv('TWILIO_FROM_NUMBER', '+15550000000')

    def raise_conn(*a, **kw):
        raise requests.ConnectionError('reset')
    monkeypatch.setattr('requests.request', raise_conn)

    ex = get_executor('sms')
    result = ex.execute(_FakeAction({'phone': '+15551234567', 'body': 'hi'}))
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_ERROR
    assert result.outcome_unknown is True


# --- reconcile: happy path ---

class _ReconcileCompletedPhone:
    status_code = 200

    def json(self):
        return {'status': 'completed'}


def test_phone_reconcile_completed(monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'key-123')
    monkeypatch.setattr('requests.request', lambda *a, **kw: _ReconcileCompletedPhone())
    ex = get_executor('phone-call')
    result = ex.reconcile(_FakeAction({}, external_ref='CALL123'))
    assert result.status == 'completed'


class _ReconcileDeliveredSms:
    status_code = 200

    def json(self):
        return {'status': 'delivered'}


def test_sms_reconcile_delivered(monkeypatch):
    monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'ACxxx')
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'tok')
    monkeypatch.setattr('requests.request', lambda *a, **kw: _ReconcileDeliveredSms())
    ex = get_executor('sms')
    result = ex.reconcile(_FakeAction({}, external_ref='SM123'))
    assert result.status == 'completed'


class _ReconcileFailedPhone:
    status_code = 200

    def json(self):
        return {'status': 'no-answer'}


def test_phone_reconcile_no_answer_maps_to_failed(monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'key-123')
    monkeypatch.setattr('requests.request', lambda *a, **kw: _ReconcileFailedPhone())
    ex = get_executor('phone-call')
    result = ex.reconcile(_FakeAction({}, external_ref='CALL123'))
    assert result.status == 'failed'


class _ReconcileInFlight:
    status_code = 200

    def json(self):
        return {'status': 'queued'}


def test_phone_reconcile_in_flight_stays_executing(monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'key-123')
    monkeypatch.setattr('requests.request', lambda *a, **kw: _ReconcileInFlight())
    ex = get_executor('phone-call')
    result = ex.reconcile(_FakeAction({}, external_ref='CALL123'))
    assert result.status == 'executing'


def test_phone_reconcile_missing_keys_fails_loud(monkeypatch):
    monkeypatch.delenv('BLAND_AI_API_KEY', raising=False)
    monkeypatch.delenv('BLAND_API_KEY', raising=False)
    ex = get_executor('phone-call')
    result = ex.reconcile(_FakeAction({}, external_ref='CALL123'))
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_NOT_CONFIGURED


def test_phone_reconcile_network_error_is_needs_review_not_a_verdict(monkeypatch):
    import requests
    monkeypatch.setenv('BLAND_AI_API_KEY', 'key-123')

    def raise_conn(*a, **kw):
        raise requests.ConnectionError('reset')
    monkeypatch.setattr('requests.request', raise_conn)

    ex = get_executor('phone-call')
    result = ex.reconcile(_FakeAction({}, external_ref='CALL123'))
    assert result.status == 'needs_review'
    assert result.outcome.get('reason') == 'reconcile_unreachable'
