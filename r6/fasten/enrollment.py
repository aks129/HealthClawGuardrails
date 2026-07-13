"""Short-lived, one-time browser proof for Fasten patient enrollment."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time

from flask import session

from r6.runtime_config import resolve_app_env


logger = logging.getLogger(__name__)

SESSION_KEY = "fasten_enrollment"
ENROLLMENT_TTL_SECONDS = 10 * 60

_redis_client = None
_proofs: dict[str, tuple[str, int]] = {}
_proofs_lock = threading.Lock()

_CONSUME_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""


def _get_redis_client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        return None
    import redis

    _redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
    return _redis_client


def _storage_key(tenant_id: str, org_connection_id: str) -> str:
    binding = f"{tenant_id}\0{org_connection_id}".encode("utf-8")
    digest = hashlib.sha256(binding).hexdigest()
    return f"healthclaw:fasten-enrollment:{digest}"


def _proof_digest(proof: str) -> str:
    return hashlib.sha256(proof.encode("utf-8")).hexdigest()


def _prune_expired(now: int) -> None:
    for key, (_digest, expires_at) in list(_proofs.items()):
        if expires_at < now:
            _proofs.pop(key, None)


def establish_enrollment(tenant_id: str, org_connection_id: str) -> None:
    """Rotate the browser's proof and bind it to one tenant/connection."""
    proof = secrets.token_urlsafe(32)
    digest = _proof_digest(proof)
    expires_at = int(time.time()) + ENROLLMENT_TTL_SECONDS
    key = _storage_key(tenant_id, org_connection_id)

    client = _get_redis_client()
    if client is not None:
        try:
            client.set(key, digest, ex=ENROLLMENT_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - Redis clients vary
            logger.error("Fasten enrollment Redis write failed: %s", type(exc).__name__)
            if resolve_app_env() == "production":
                raise RuntimeError("Fasten enrollment store unavailable") from None
            client = None

    if client is None:
        with _proofs_lock:
            _prune_expired(int(time.time()))
            _proofs[key] = (digest, expires_at)

    session[SESSION_KEY] = {
        "tenant_id": tenant_id,
        "org_connection_id": org_connection_id,
        "proof": proof,
        "expires_at": expires_at,
    }


def enrollment_tenant(org_connection_id: str) -> str | None:
    """Return the signed-session tenant for this connection, if current."""
    claim = session.get(SESSION_KEY)
    if not isinstance(claim, dict):
        return None
    if claim.get("org_connection_id") != org_connection_id:
        return None
    if not isinstance(claim.get("tenant_id"), str):
        return None
    if not isinstance(claim.get("proof"), str):
        return None
    if int(claim.get("expires_at") or 0) < int(time.time()):
        session.pop(SESSION_KEY, None)
        return None
    return claim["tenant_id"]


def consume_enrollment(tenant_id: str, org_connection_id: str) -> bool:
    """Atomically consume the server-side proof bound to the signed session."""
    claim = session.get(SESSION_KEY)
    if not isinstance(claim, dict):
        return False
    if (
        claim.get("tenant_id") != tenant_id
        or claim.get("org_connection_id") != org_connection_id
        or int(claim.get("expires_at") or 0) < int(time.time())
    ):
        session.pop(SESSION_KEY, None)
        return False
    proof = claim.get("proof")
    if not isinstance(proof, str):
        session.pop(SESSION_KEY, None)
        return False

    key = _storage_key(tenant_id, org_connection_id)
    digest = _proof_digest(proof)
    consumed = False
    client = _get_redis_client()
    if client is not None:
        try:
            consumed = bool(client.eval(_CONSUME_SCRIPT, 1, key, digest))
        except Exception as exc:  # noqa: BLE001 - Redis clients vary
            logger.error(
                "Fasten enrollment Redis consume failed: %s", type(exc).__name__
            )
            if resolve_app_env() == "production":
                return False
            client = None

    if client is None:
        with _proofs_lock:
            now = int(time.time())
            _prune_expired(now)
            stored = _proofs.get(key)
            if stored and stored[0] == digest and stored[1] >= now:
                _proofs.pop(key, None)
                consumed = True

    if consumed:
        session.pop(SESSION_KEY, None)
    return consumed
