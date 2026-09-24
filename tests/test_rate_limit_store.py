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


# ---------------------------------------------------------------------------
# check_rate_limit_or_raise: "unavailable" is not "over budget" (#648)
#
# check_rate_limit answers a production Redis outage with the same tuple as
# an exhausted budget, which is right for the request limiter (fail closed)
# and wrong for a budget whose caller must fail OPEN — the refusal-audit
# budget in r6/access.py. The new function differs from check_rate_limit in
# exactly that one case; every other answer is the same tuple.
#
# MUTATIONS (commit first; PYTHONDONTWRITEBYTECODE=1; purge __pycache__):
#   in check_rate_limit_or_raise, return the deny tuple instead of raising
#       -> test_or_raise_reports_a_production_outage_as_unavailable red
#   in check_rate_limit, raise instead of returning the deny tuple
#       -> test_redis_failure_denies_in_production red
# ---------------------------------------------------------------------------

def test_redis_failure_still_denies_and_logs_once_by_type(monkeypatch, caplog):
    """The request limiter's outage answer is unchanged, and so is its log:
    one line, the exception's type name, never its text."""
    fake = FakeRedis(error=ConnectionError("redis://secret-host:6379 refused"))
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid/0")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(rate_limit, "_redis_client", fake, raising=False)

    with caplog.at_level("ERROR", logger="r6.rate_limit"):
        result = rate_limit.check_rate_limit("tenant-a", max_requests=5,
                                             window_seconds=60)

    assert result[0:2] == (False, 0)
    assert [r.getMessage() for r in caplog.records] == [
        "Redis rate-limit check failed: ConnectionError"]
    assert "secret-host" not in caplog.text


def test_or_raise_reports_a_production_outage_as_unavailable(monkeypatch,
                                                             caplog):
    fake = FakeRedis(error=ConnectionError("redis://secret-host:6379 refused"))
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid/0")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(rate_limit, "_redis_client", fake, raising=False)

    with caplog.at_level("DEBUG", logger="r6.rate_limit"):
        try:
            rate_limit.check_rate_limit_or_raise("tenant-a")
        except rate_limit.RateLimitUnavailable as exc:
            unavailable = exc
        else:
            raise AssertionError("an outage was answered as a budget")

    # The type name only; the caller logs it, so the limiter does not.
    assert str(unavailable) == "ConnectionError"
    assert unavailable.__cause__ is None and unavailable.__suppress_context__
    assert caplog.records == []


def test_or_raise_answers_over_budget_as_a_deny_not_an_outage(monkeypatch):
    fake = FakeRedis(replies=[[1, 60], [2, 59]])
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid/0")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(rate_limit, "_redis_client", fake, raising=False)

    assert rate_limit.check_rate_limit_or_raise(
        "tenant-a", max_requests=1)[0:2] == (True, 0)
    assert rate_limit.check_rate_limit_or_raise(
        "tenant-a", max_requests=1)[0:2] == (False, 0)


def test_or_raise_matches_check_rate_limit_on_every_other_path(monkeypatch):
    """Redis healthy, Redis absent, and a non-production Redis failure (which
    falls back to memory in both) give the same tuples from both functions."""
    monkeypatch.setattr(rate_limit.time, "time", lambda: 1_000.0)

    def run(fn):
        rate_limit._rate_limits.clear()
        return [fn("tenant-a", max_requests=2, window_seconds=60)
                for _ in range(3)]

    # Redis healthy.
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid/0")
    monkeypatch.setenv("APP_ENV", "production")
    replies = [[1, 60], [2, 59], [3, 58]]
    monkeypatch.setattr(rate_limit, "_redis_client",
                        FakeRedis(replies=replies), raising=False)
    healthy = run(rate_limit.check_rate_limit)
    monkeypatch.setattr(rate_limit, "_redis_client",
                        FakeRedis(replies=replies), raising=False)
    assert run(rate_limit.check_rate_limit_or_raise) == healthy
    assert [r[0] for r in healthy] == [True, True, False]

    # Non-production Redis failure: both fall back to the memory store.
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr(rate_limit, "_redis_client",
                        FakeRedis(error=ConnectionError("down")),
                        raising=False)
    fallback = run(rate_limit.check_rate_limit)
    assert run(rate_limit.check_rate_limit_or_raise) == fallback
    assert [r[0] for r in fallback] == [True, True, False]

    # No Redis configured.
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(rate_limit, "_redis_client", None, raising=False)
    memory = run(rate_limit.check_rate_limit)
    assert run(rate_limit.check_rate_limit_or_raise) == memory
    assert memory == fallback
