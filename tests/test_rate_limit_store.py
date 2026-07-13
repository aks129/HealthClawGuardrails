"""Rate-limit storage must be shared, expiring, and bounded."""

import time

import r6.rate_limit as rate_limit


class FakeRedis:
    def __init__(self, replies=None, error=None):
        self.replies = list(replies or [])
        self.error = error
        self.calls = []

    def eval(self, script, key_count, key, window):
        self.calls.append((script, key_count, key, window))
        if self.error:
            raise self.error
        return self.replies.pop(0)


def test_redis_rate_limit_uses_atomic_expiring_counter(monkeypatch):
    fake = FakeRedis(replies=[[1, 60], [2, 59]])
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid/0")
    monkeypatch.setattr(rate_limit, "_redis_client", fake, raising=False)

    first = rate_limit.check_rate_limit("tenant-a", max_requests=2)
    second = rate_limit.check_rate_limit("tenant-a", max_requests=2)

    assert first[0:2] == (True, 1)
    assert second[0:2] == (True, 0)
    assert len(fake.calls) == 2
    assert all(call[1] == 1 for call in fake.calls)
    assert all(call[2].startswith("healthclaw:rate-limit:") for call in fake.calls)


def test_in_memory_rate_limit_prunes_expired_keys(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(rate_limit, "_redis_client", None, raising=False)
    now = time.time()
    rate_limit._rate_limits.clear()
    rate_limit._rate_limits.update({
        f"expired-{i}": {"count": 1, "reset_at": now - 1}
        for i in range(100)
    })

    rate_limit.check_rate_limit("active", max_requests=2, window_seconds=60)

    assert set(rate_limit._rate_limits) == {"active"}


def test_redis_failure_denies_in_production(monkeypatch):
    fake = FakeRedis(error=ConnectionError("redis unavailable"))
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid/0")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(rate_limit, "_redis_client", fake, raising=False)

    allowed, remaining, _reset = rate_limit.check_rate_limit("tenant-a")

    assert allowed is False
    assert remaining == 0
