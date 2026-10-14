"""
Step-up token generation and validation.

Tokens are HMAC-SHA256 signed with a shared secret and include:
- Expiration timestamp
- Tenant ID binding
- Random nonce for replay prevention

Token format: {base64url_payload}.{hmac_hex_signature}
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

from r6 import constant_time
from r6.runtime_config import resolve_app_env

logger = logging.getLogger(__name__)

# Default TTL for step-up tokens (5 minutes)
DEFAULT_TOKEN_TTL_SECONDS = 300

# TTL for patient connect tokens (read-scoped, issued once at the moment of an
# identity-verified data connection — see r6/fasten/routes.py). Renewal =
# reconnect. Read-scoped tokens can never authorize writes (H4 stays intact).
READ_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days

# The `sub` of exactly those patient connect tokens. A Disconnect ends them
# at the read gate (r6/read_auth.py, SEC940-F2); tokens CareAgents mints
# server-side carry another subject and keep reading.
PATIENT_CONNECT_SUBJECT = 'patient-connect'

# The `scope` claims a write gate accepts (FHI-57). An allow-list, not a
# denylist: None is a token minted without the claim, the historical
# full-capability shape and the only one any minter writes today; 'write'
# is the explicit spelling of the same capability, which
# tests/test_patient_connect_token.py already pins as write-capable.
# Adding a scope here is a decision, and it gets its own test.
WRITE_SCOPES = frozenset({None, 'write'})

# The `scope` claims a tenant-bound (read) gate accepts: the write scopes and
# 'read'. Also an allow-list, for the same reason: a narrow scope minted
# later (a check-in export, say) must not read the whole tenant by default
# (#962 security review).
READ_SCOPES = WRITE_SCOPES | {'read'}


def _scope_in(claim, allowed):
    """True only for a claim in `allowed`. A list or object claim is
    refused here rather than raising TypeError on the set lookup."""
    return (claim is None or isinstance(claim, str)) and claim in allowed


# ---------------------------------------------------------------------------
# Replay guard (opt-in).
#
# Tokens carry a random `nonce`. By default a token may be reused freely
# within its TTL (multi-call write/read bursts depend on this — flipping every
# validation to strict single-use would break those flows). Callers that want
# strict single-use semantics pass consume_nonce=True to validate_step_up_token;
# the first such validation records the nonce, and any later validation of the
# same nonce is rejected as a replay.
#
# Backed by Redis (SET NX EX) when REDIS_URL is set. Without it the map below
# is process-local: it resets on restart and is not shared across workers, so
# a token consumed on one worker replays on a sibling. That is adequate for a
# single-process dev server and for tests, and for nothing else — production
# refuses to fall back to it (#212), at boot (r6/runtime_config.py) and again
# here, for the paths that skip the boot gate.
# ---------------------------------------------------------------------------
_seen_nonces: dict[str, float] = {}  # nonce -> exp (unix seconds)
_nonce_lock = threading.Lock()
_redis_client = None
_MAX_SEEN_NONCES = 10_000


def _get_redis_client():
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
    return resolve_app_env() == 'production'


def _evict_expired_nonces(now=None):
    """Lazily drop nonces whose token has already expired."""
    now = now if now is not None else time.time()
    expired = [n for n, exp in _seen_nonces.items() if exp < now]
    for n in expired:
        _seen_nonces.pop(n, None)


def mark_nonce_used(nonce, exp):
    """
    Record a nonce as consumed until `exp`.

    Returns:
        bool: True if the nonce was newly recorded, False if it had already
              been consumed (i.e. this is a replay).
    """
    if not nonce:
        # No nonce to track — treat as a fresh use, never a replay.
        return True
    now = time.time()
    client = _get_redis_client()
    if client is not None:
        key_hash = hashlib.sha256(nonce.encode('utf-8')).hexdigest()
        ttl = max(1, int(exp - now))
        try:
            return bool(client.set(
                f'healthclaw:stepup-nonce:{key_hash}', 'used', nx=True, ex=ttl
            ))
        except Exception as exc:  # noqa: BLE001 - Redis client errors vary
            logger.error('Redis nonce consumption failed: %s',
                         type(exc).__name__)
            if _is_production():
                return False
    elif _is_production():
        # Absence, not failure. Raised rather than answered False: a missing
        # store has decided nothing, and False would reach the caller as
        # 'Token already used (replay)' — a config fault filed as an auth
        # result. r6.access._evaluate lets it out as the error it is.
        raise RuntimeError(
            'REDIS_URL is required in production: single-use nonces need a '
            'store shared by every worker')

    # Development/testing fallback: bounded and atomic within this process.
    with _nonce_lock:
        _evict_expired_nonces(now)
        if nonce in _seen_nonces and _seen_nonces[nonce] >= now:
            return False
        if len(_seen_nonces) >= _MAX_SEEN_NONCES:
            oldest = min(_seen_nonces, key=_seen_nonces.get)
            _seen_nonces.pop(oldest, None)
        _seen_nonces[nonce] = exp
        return True


def clear_nonce_cache():
    """Clear the replay-guard nonce cache. Intended for tests."""
    with _nonce_lock:
        _seen_nonces.clear()


def _get_secret():
    """Get the HMAC secret from environment."""
    return os.environ.get('STEP_UP_SECRET', '')


def generate_step_up_token(tenant_id, agent_id=None,
                           ttl_seconds=DEFAULT_TOKEN_TTL_SECONDS,
                           scope=None, audience=None, operation=None):
    """
    Generate a signed step-up authorization token.

    Args:
        tenant_id: Tenant the token is scoped to
        agent_id: Optional agent identifier
        ttl_seconds: Token lifetime in seconds
        scope: Optional capability scope. 'read' produces a token the read
            gate accepts but every write path rejects. None (default) omits
            the claim — the historical full-capability token shape.
        audience: Optional service or feature audience binding.
        operation: Optional exact operation/resource binding.

    Returns:
        Signed token string: {base64_payload}.{hmac_signature}

    Raises:
        ValueError: If STEP_UP_SECRET is not configured
    """
    secret = _get_secret()
    if not secret:
        raise ValueError('STEP_UP_SECRET environment variable is required')

    payload = {
        'exp': int(time.time()) + ttl_seconds,
        'tid': tenant_id,
        'sub': agent_id or 'system',
        'nonce': secrets.token_hex(16)
    }
    if scope:
        payload['scope'] = scope
    if audience:
        payload['aud'] = audience
    if operation:
        payload['op'] = operation
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':')).encode()
    ).decode()
    sig = hmac.new(
        secret.encode(), payload_b64.encode(), hashlib.sha256
    ).hexdigest()
    return f'{payload_b64}.{sig}'


def token_subject(token):
    """The `sub` claim of a token that has ALREADY been validated, or None.

    Checks no signature, expiry or tenant: it only reads the payload half.
    Call it after validate_step_up_token (or the kernel) accepted the same
    token, never as a decision of its own.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.rsplit('.', 1)[0]))
    except Exception:
        return None
    sub = payload.get('sub') if isinstance(payload, dict) else None
    return sub if isinstance(sub, str) else None


def validate_step_up_token(token, tenant_id, consume_nonce=False,
                           require_scope='write', require_audience=None,
                           require_operation=None):
    """
    Validate a step-up authorization token.

    Checks:
    - HMAC signature matches
    - Token is not expired
    - Tenant ID matches
    - Scope satisfies `require_scope`
    - (when consume_nonce=True) the token's nonce has not been used before

    Args:
        token: The token string to validate
        tenant_id: Expected tenant ID
        consume_nonce: When True, enforce strict single-use — the nonce is
            recorded on first successful validation and any subsequent
            validation of the same token is rejected as a replay. Defaults to
            False, preserving the historical multi-use behavior (no replay
            tracking) so existing callers are unaffected.
        require_scope: Capability the caller is authorizing. The default
            'write' REJECTS read-scoped tokens, so every existing write call
            site stays strict without modification (fail-safe). Read paths
            pass require_scope=None, which accepts a scope in READ_SCOPES
            (none, 'read' or 'write') and refuses any other. Any other
            require_scope value refuses every token.
            'write' accepts only a scope in WRITE_SCOPES: no scope claim at
            all (the legacy full-capability shape) or 'write'. Any other
            claim, 'read' or a string nobody planned for, is refused.
        require_audience: Exact audience claim required for this operation.
            None means the gate serves no audience, so a token that carries
            one is refused.
        require_operation: Exact operation claim required for this operation.

    Returns:
        tuple: (is_valid: bool, error_message: str or None)
    """
    secret = _get_secret()
    if not secret:
        logger.warning('STEP_UP_SECRET not configured; rejecting step-up token')
        return False, 'Server step-up validation not configured'

    if not token or '.' not in token:
        return False, 'Malformed step-up token'

    parts = token.rsplit('.', 1)
    if len(parts) != 2:
        return False, 'Malformed step-up token'

    payload_b64, sig = parts

    # Verify HMAC signature (constant-time comparison).
    #
    # Both halves of the token are caller-supplied, so both reach a byte
    # operation through r6/constant_time (#557). Neither `str.encode` nor
    # `hmac.compare_digest` is total over `str`: strict UTF-8 refuses a lone
    # surrogate, and compare_digest refuses any non-ASCII `str` outright.
    # Either one raised TypeError/UnicodeEncodeError out of this validator,
    # past a `_evaluate` that deliberately does not catch, and answered an
    # anonymous caller with a 500 that said their token was NOT merely wrong.
    # A token that cannot be spelled is a token that does not verify, and it
    # is refused right here with every other one that does not verify.
    expected_sig = hmac.new(
        secret.encode(), constant_time.as_bytes(payload_b64), hashlib.sha256
    ).hexdigest()
    if not constant_time.equal(sig, expected_sig):
        return False, 'Invalid token signature'

    # Decode and validate payload
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return False, 'Malformed token payload'
    # Signed JSON that is not an object (a list, a number) is malformed, not
    # an AttributeError on the next line.
    if not isinstance(payload, dict):
        return False, 'Malformed token payload'

    # Check expiry
    if payload.get('exp', 0) < time.time():
        return False, 'Step-up token expired'

    # Check tenant binding
    if payload.get('tid') != tenant_id:
        return False, 'Token tenant mismatch'

    # Scope gate: a write path accepts an allow-listed write scope and
    # nothing else (FHI-57). It used to refuse 'read' and pass every other
    # string, so the first new scope anybody minted was write-capable at
    # every write gate. 'read' keeps its own sentence, which clients know.
    scope_claim = payload.get('scope')
    if require_scope == 'write':
        if not _scope_in(scope_claim, WRITE_SCOPES):
            if scope_claim == 'read':
                return False, 'Read-scoped token cannot authorize this operation'
            return False, 'Token scope cannot authorize this operation'
    elif require_scope is None:
        # Tenant-bound reads: any known scope, nothing unplanned.
        if not _scope_in(scope_claim, READ_SCOPES):
            return False, 'Token scope cannot authorize this operation'
    else:
        # No gate asks for another capability; one that did would otherwise
        # skip the scope check altogether.
        return False, 'Token scope cannot authorize this operation'

    # Audience, both ways (FHI-57, #963 security review F1). A gate that asks
    # for an audience needs exactly it, and a gate that asks for none refuses
    # a token that carries one: a token minted for one purpose (an action
    # approval, a check-in export) is not a general credential for the rest
    # of the tenant. Tokens minted without `aud` are unaffected.
    if payload.get('aud') != require_audience:
        return False, 'Token audience mismatch'

    if require_operation is not None and payload.get('op') != require_operation:
        return False, 'Token operation mismatch'

    # Optional replay guard — only when the caller opts in.
    if consume_nonce:
        exp = int(payload.get('exp', 0))
        if not mark_nonce_used(payload.get('nonce'), exp):
            return False, 'Token already used (replay)'

    return True, None
