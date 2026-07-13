"""Signed-session helpers for one-time Fasten browser enrollment."""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone

from flask import session


SESSION_KEY = "fasten_enrollment"
ENROLLMENT_TTL_SECONDS = 10 * 60


def _proof_digest(proof: str) -> str:
    return hashlib.sha256(proof.encode("utf-8")).hexdigest()


def establish_enrollment(
    tenant_id: str,
    org_connection_id: str,
) -> tuple[str, datetime]:
    """Create a raw browser proof and return only its hash for persistence."""
    proof = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=ENROLLMENT_TTL_SECONDS
    )
    session[SESSION_KEY] = {
        "tenant_id": tenant_id,
        "org_connection_id": org_connection_id,
        "proof": proof,
        "expires_at": int(expires_at.timestamp()),
    }
    return _proof_digest(proof), expires_at


def _session_claim(org_connection_id: str) -> dict | None:
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
    return claim


def enrollment_tenant(org_connection_id: str) -> str | None:
    """Return the tenant bound into the current signed browser session."""
    claim = _session_claim(org_connection_id)
    return claim["tenant_id"] if claim else None


def enrollment_proof_hash(org_connection_id: str) -> str | None:
    """Hash the current raw session proof for a conditional database claim."""
    claim = _session_claim(org_connection_id)
    if claim is None:
        return None
    return _proof_digest(claim["proof"])


def clear_enrollment_session() -> None:
    """Forget the raw proof only after its database transaction commits."""
    session.pop(SESSION_KEY, None)
