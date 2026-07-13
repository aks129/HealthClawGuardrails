"""Shared tenant-bound read authorization for Flask route surfaces."""

from __future__ import annotations

import os
import re

from flask import request, session

from r6.oauth import validate_bearer_token
from r6.stepup import validate_step_up_token


_TRUE_VALUES = frozenset({"1", "true", "yes"})
_TENANT_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
TENANT_SESSION_KEY = "cc_tenant"
_OAUTH_READ_SCOPES = frozenset({
    "patient/*.read",
    "smart/patient/*.read",
    "fhir.read",
    "user/*.read",
    "system/*.read",
})


def read_auth_enabled() -> bool:
    """Return whether tenant read authentication is explicitly enabled."""
    return os.environ.get("READ_AUTH_ENABLED", "").strip().lower() in _TRUE_VALUES


def public_tenants() -> frozenset[str]:
    """Return the explicit allowlist of synthetic public tenants."""
    raw = os.environ.get("PUBLIC_TENANTS", "").strip()
    if not raw:
        return frozenset()
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def is_public_tenant(tenant_id: str) -> bool:
    return tenant_id in public_tenants()


def read_auth_required(tenant_id: str) -> bool:
    return read_auth_enabled() and not is_public_tenant(tenant_id)


def _oauth_authorizes(token: str, tenant_id: str) -> bool:
    ok, info = validate_bearer_token(token)
    if not ok or not isinstance(info, dict):
        return False
    if info.get("tenant_id") != tenant_id:
        return False
    return bool(set(info.get("scopes") or ()) & _OAUTH_READ_SCOPES)


def authorize_tenant_read(
    tenant_id: str,
    *,
    session_tenant: str | None = None,
    always_require: bool = False,
) -> str | None:
    """Return the tenant only after its read claim is authorized.

    Public tenants bypass credentials only through ``PUBLIC_TENANTS``. When
    auth is required, accepted credentials are a matching authenticated
    session, tenant-bound step-up token (including read-scoped tokens), or a
    tenant-bound SMART OAuth bearer carrying a read scope.

    ``always_require`` is used by surfaces such as command center that were
    already private before the rollout flag existed. Other routes preserve
    local/testing compatibility while production startup requires the flag.
    """
    if not tenant_id or not _TENANT_PATTERN.fullmatch(tenant_id):
        return None
    if is_public_tenant(tenant_id):
        return tenant_id
    if not always_require and not read_auth_enabled():
        return tenant_id
    if session_tenant is None:
        session_tenant = session.get(TENANT_SESSION_KEY)
    if session_tenant == tenant_id:
        return tenant_id

    auth = (request.headers.get("Authorization") or "").strip()
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    step_up = (request.headers.get("X-Step-Up-Token") or "").strip() or bearer

    if step_up:
        valid, _error = validate_step_up_token(
            step_up,
            tenant_id,
            require_scope=None,
        )
        if valid:
            return tenant_id
    if bearer and _oauth_authorizes(bearer, tenant_id):
        return tenant_id
    return None
