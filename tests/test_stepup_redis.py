"""Distributed replay prevention and operation-bound step-up tokens."""

from concurrent.futures import ThreadPoolExecutor
import time

import r6.stepup as stepup


class FakeRedis:
    def __init__(self, error=None):
        self.error = error
        self.values = {}
        self.calls = []

    def set(self, key, value, *, nx, ex):
        self.calls.append((key, value, nx, ex))
        if self.error:
            raise self.error
        if key in self.values:
            return None
        self.values[key] = value
        return True


def test_nonce_consumption_uses_atomic_redis_set(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid/0")
    monkeypatch.setattr(stepup, "_redis_client", fake, raising=False)

    exp = int(time.time()) + 60
    assert stepup.mark_nonce_used("one-time", exp) is True
    assert stepup.mark_nonce_used("one-time", exp) is False
    assert len(fake.calls) == 2
    assert fake.calls[0][2] is True
    assert 1 <= fake.calls[0][3] <= 60
    assert "one-time" not in fake.calls[0][0]


def test_nonce_redis_failure_is_fail_closed_in_production(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid/0")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        stepup, "_redis_client", FakeRedis(ConnectionError("unavailable")),
        raising=False,
    )

    assert stepup.mark_nonce_used("nonce", int(time.time()) + 60) is False


def test_in_memory_nonce_consumption_is_thread_safe(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setattr(stepup, "_redis_client", None, raising=False)
    stepup.clear_nonce_cache()
    exp = int(time.time()) + 60

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _index: stepup.mark_nonce_used("racy", exp), range(32)
        ))

    assert results.count(True) == 1
    assert results.count(False) == 31


def test_step_up_token_can_be_bound_to_audience_and_operation(tenant_id):
    token = stepup.generate_step_up_token(
        tenant_id,
        audience="curatr",
        operation="apply-fix:Condition/example",
    )

    assert stepup.validate_step_up_token(
        token,
        tenant_id,
        require_audience="curatr",
        require_operation="apply-fix:Condition/example",
    ) == (True, None)
    assert stepup.validate_step_up_token(
        token, tenant_id, require_audience="different"
    ) == (False, "Token audience mismatch")
    assert stepup.validate_step_up_token(
        token, tenant_id, require_operation="apply-fix:Condition/other"
    ) == (False, "Token operation mismatch")
