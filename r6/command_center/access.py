"""
Command center access control — signed shareable URLs.

The dashboard supports two access modes:

1. **Public demo tenant** (`desktop-demo`) — no auth required. Anyone landing
   on /command-center sees the demo data.

2. **Personal tenants** (anything else) — require a signed access token in
   either a Flask session (after clicking a signed link) or a `?t=<token>`
   query param. Tokens are HMAC-signed with itsdangerous and expire after
   `DASHBOARD_TTL_HOURS` (default 24h).

The Telegram bot generates tokens via POST /command-center/api/generate-link
and sends the resulting URL to the user. The bot itself authenticates with
a step-up token, so only the bot owner can mint dashboard links.
"""

from __future__ import annotations

import logging
import os

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

logger = logging.getLogger(__name__)

def _public_tenants() -> frozenset[str]:
    """
    Tenants that are accessible without authentication.

    Configurable via PUBLIC_TENANTS env var (comma-separated). Empty/unset
    means NO public tenants — every dashboard request requires a signed
    link or session. Set to "desktop-demo" on demo hosts (healthclaw.io)
    to keep the synthetic demo tenant browsable.
    """
    raw = os.environ.get("PUBLIC_TENANTS", "").strip()
    if not raw:
        return frozenset()
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


PUBLIC_TENANTS = _public_tenants()
DASHBOARD_TTL_HOURS = int(os.environ.get("DASHBOARD_TOKEN_TTL_HOURS", "24"))
_SALT = "command-center-access-v1"


def _secret() -> str:
    """
    Reuse STEP_UP_SECRET for signing (already required in production).
    Falls back to SESSION_SECRET, then a dev default.
    """
    return (
        os.environ.get("STEP_UP_SECRET")
        or os.environ.get("SESSION_SECRET")
        or "dev-dashboard-secret"
    )


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt=_SALT)


def generate_access_token(tenant_id: str, agent_id: str | None = None) -> str:
    """
    Produce a signed, URL-safe token that grants read access to `tenant_id`
    for DASHBOARD_TTL_HOURS.
    """
    payload = {"tenant_id": tenant_id}
    if agent_id:
        payload["agent_id"] = agent_id
    return _serializer().dumps(payload)


def verify_access_token(token: str) -> dict | None:
    """
    Return the decoded payload if the token is valid and unexpired;
    None otherwise. Does NOT raise.
    """
    if not token:
        return None
    try:
        return _serializer().loads(
            token,
            max_age=DASHBOARD_TTL_HOURS * 3600,
        )
    except SignatureExpired:
        logger.info("Dashboard access token expired")
        return None
    except BadSignature:
        logger.info("Dashboard access token failed signature check")
        return None


def is_public(tenant_id: str) -> bool:
    """Tenants in PUBLIC_TENANTS bypass auth entirely (demo use).

    Re-reads env every call so changes without a restart take effect.
    """
    return tenant_id in _public_tenants()


def build_dashboard_url(base_url: str, tenant_id: str,
                        agent_id: str | None = None) -> str:
    """
    Build a full dashboard URL with an embedded signed token. The caller
    supplies `base_url` (e.g., "https://healthclaw.io").
    """
    token = generate_access_token(tenant_id, agent_id=agent_id)
    base = base_url.rstrip("/")
    return f"{base}/command-center?tenant={tenant_id}&t={token}"
