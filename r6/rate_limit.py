"""
Rate limiting middleware for R6 FHIR routes.

Per-tenant rate limiting using an in-memory token bucket.
Production deployments should use Redis-backed rate limiting.
"""

import hashlib
import logging
import os
import threading
import time
from flask import g, request, jsonify

logger = logging.getLogger(__name__)

# Configuration
DEFAULT_RATE_LIMIT = 120  # requests per minute
DEFAULT_WINDOW_SECONDS = 60

# In-memory store: tenant_id -> {count, reset_at}
_rate_limits = {}
_rate_limits_lock = threading.Lock()
_redis_client = None
_MAX_MEMORY_BUCKETS = 10_000

_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


def _get_redis_client():
    """Return the shared Redis client when REDIS_URL is configured."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    redis_url = os.environ.get('REDIS_URL', '').strip()
    if not redis_url:
        return None
    import redis
    _redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
    return _redis_client


def _is_production():
    return (os.environ.get('APP_ENV') or os.environ.get('FLASK_ENV')) == 'production'


def _prune_memory_buckets(now):
    expired = [key for key, entry in _rate_limits.items()
               if now >= entry['reset_at']]
    for key in expired:
        _rate_limits.pop(key, None)
    overflow = len(_rate_limits) - _MAX_MEMORY_BUCKETS
    if overflow > 0:
        oldest = sorted(_rate_limits,
                        key=lambda key: _rate_limits[key]['reset_at'])[:overflow]
        for key in oldest:
            _rate_limits.pop(key, None)


def _client_ip():
    """
    Best-effort client IP for rate-limit keying. Used only as a rate-limit
    bucket key — never for auth.

    Behind a single trusted proxy (Railway/Vercel edge), the LAST entry in
    X-Forwarded-For is the IP appended by that trusted proxy — i.e. the real
    peer it saw. The leftmost entries are attacker-controllable: a client can
    inject "X-Forwarded-For: spoofed" and split the bucket arbitrarily. Taking
    the rightmost hop removes that spoofing surface for our 1-proxy topology.
    (If proxy depth changes, count back that many hops instead.)
    """
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        hops = [h.strip() for h in fwd.split(',') if h.strip()]
        if hops:
            return hops[-1]
    return request.remote_addr or 'unknown'


def rate_limit_key():
    """
    Resolve the bucket key for the current request.

    Prefers the X-Tenant-Id header so authenticated traffic is throttled per
    tenant. When no tenant header is present (e.g. provider webhook callbacks
    at /r6/actions/callback/<provider>, which carry no tenant), key by client
    IP instead of dumping every untenanted request into one shared 'anonymous'
    bucket — that shared bucket would let one source exhaust the limit for all
    untenanted callers. The IP key is prefixed so it can never collide with a
    real tenant id.
    """
    tenant_id = request.headers.get('X-Tenant-Id')
    if tenant_id:
        return tenant_id
    return f'ip:{_client_ip()}'


def check_rate_limit(tenant_id, max_requests=DEFAULT_RATE_LIMIT,
                     window_seconds=DEFAULT_WINDOW_SECONDS):
    """
    Check if a tenant has exceeded their rate limit.

    Returns:
        tuple: (allowed: bool, remaining: int, reset_at: float)
    """
    now = time.time()
    client = _get_redis_client()
    if client is not None:
        digest = hashlib.sha256(tenant_id.encode('utf-8')).hexdigest()
        key = f'healthclaw:rate-limit:{digest}'
        try:
            count, ttl = client.eval(
                _RATE_LIMIT_SCRIPT, 1, key, int(window_seconds)
            )
            count = int(count)
            ttl = max(0, int(ttl))
            remaining = max(0, max_requests - count)
            return count <= max_requests, remaining, now + ttl
        except Exception as exc:  # noqa: BLE001 - Redis client errors vary
            logger.error('Redis rate-limit check failed: %s',
                         type(exc).__name__)
            if _is_production():
                return False, 0, now + window_seconds

    # Development/testing fallback is bounded and protected from thread races.
    with _rate_limits_lock:
        _prune_memory_buckets(now)
        entry = _rate_limits.get(tenant_id)
        if not entry:
            reset_at = now + window_seconds
            _rate_limits[tenant_id] = {'count': 1, 'reset_at': reset_at}
            return True, max_requests - 1, reset_at

        entry['count'] += 1
        remaining = max(0, max_requests - entry['count'])
        return entry['count'] <= max_requests, remaining, entry['reset_at']


def rate_limit_middleware(blueprint):
    """
    Register rate limiting as a before_request hook on the blueprint.
    Adds X-RateLimit-* headers to responses.
    """

    @blueprint.after_request
    def add_rate_limit_headers(response):
        """Add rate limit headers to every response."""
        state = getattr(g, 'rate_limit_state', None)
        if state:
            remaining, reset_at = state
            response.headers['X-RateLimit-Limit'] = str(DEFAULT_RATE_LIMIT)
            response.headers['X-RateLimit-Remaining'] = str(remaining)
            response.headers['X-RateLimit-Reset'] = str(int(reset_at))
        return response

    @blueprint.before_request
    def enforce_rate_limit():
        """Block requests that exceed the rate limit."""
        # Skip rate limiting for metadata (discovery)
        if request.path.endswith('/metadata'):
            return None
        if request.path.endswith('/oauth-authorization-server'):
            return None
        if request.path.endswith('/smart-configuration'):
            return None

        allowed, remaining, reset_at = check_rate_limit(rate_limit_key())
        g.rate_limit_state = (remaining, reset_at)

        if not allowed:
            response = jsonify({
                'resourceType': 'OperationOutcome',
                'issue': [{
                    'severity': 'error',
                    'code': 'throttled',
                    'diagnostics': 'Rate limit exceeded. Try again later.'
                }]
            })
            response.status_code = 429
            response.headers['X-RateLimit-Limit'] = str(DEFAULT_RATE_LIMIT)
            response.headers['X-RateLimit-Remaining'] = '0'
            response.headers['X-RateLimit-Reset'] = str(int(reset_at))
            response.headers['Retry-After'] = str(int(reset_at - time.time()))
            return response
