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
import time

logger = logging.getLogger(__name__)

# Default TTL for step-up tokens (5 minutes)
DEFAULT_TOKEN_TTL_SECONDS = 300

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
# Process-local only (resets on restart, not shared across workers). For a
# multi-worker deployment this should be backed by Redis; the in-memory map is
# adequate for the single-process reference deployment and for tests.
# ---------------------------------------------------------------------------
_seen_nonces = {}  # nonce -> exp (unix seconds)


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
    _evict_expired_nonces(now)
    if nonce in _seen_nonces and _seen_nonces[nonce] >= now:
        return False
    _seen_nonces[nonce] = exp
    return True


def clear_nonce_cache():
    """Clear the replay-guard nonce cache. Intended for tests."""
    _seen_nonces.clear()


def _get_secret():
    """Get the HMAC secret from environment."""
    return os.environ.get('STEP_UP_SECRET', '')


def generate_step_up_token(tenant_id, agent_id=None,
                           ttl_seconds=DEFAULT_TOKEN_TTL_SECONDS):
    """
    Generate a signed step-up authorization token.

    Args:
        tenant_id: Tenant the token is scoped to
        agent_id: Optional agent identifier
        ttl_seconds: Token lifetime in seconds

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
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':')).encode()
    ).decode()
    sig = hmac.new(
        secret.encode(), payload_b64.encode(), hashlib.sha256
    ).hexdigest()
    return f'{payload_b64}.{sig}'


def validate_step_up_token(token, tenant_id, consume_nonce=False):
    """
    Validate a step-up authorization token.

    Checks:
    - HMAC signature matches
    - Token is not expired
    - Tenant ID matches
    - (when consume_nonce=True) the token's nonce has not been used before

    Args:
        token: The token string to validate
        tenant_id: Expected tenant ID
        consume_nonce: When True, enforce strict single-use — the nonce is
            recorded on first successful validation and any subsequent
            validation of the same token is rejected as a replay. Defaults to
            False, preserving the historical multi-use behavior (no replay
            tracking) so existing callers are unaffected.

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

    # Verify HMAC signature (constant-time comparison)
    expected_sig = hmac.new(
        secret.encode(), payload_b64.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False, 'Invalid token signature'

    # Decode and validate payload
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return False, 'Malformed token payload'

    # Check expiry
    if payload.get('exp', 0) < time.time():
        return False, 'Step-up token expired'

    # Check tenant binding
    if payload.get('tid') != tenant_id:
        return False, 'Token tenant mismatch'

    # Optional replay guard — only when the caller opts in.
    if consume_nonce:
        exp = int(payload.get('exp', 0))
        if not mark_nonce_used(payload.get('nonce'), exp):
            return False, 'Token already used (replay)'

    return True, None
