"""
Public tenants — the tenants that bypass read authentication (demo use).

Shared by the FHIR routes, OAuth, purge and the Fasten routes, so it lives
in the core rather than in any one optional module.
"""

from __future__ import annotations

import os


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


def is_public(tenant_id: str) -> bool:
    """Tenants in PUBLIC_TENANTS bypass auth entirely (demo use).

    Re-reads env every call so changes without a restart take effect.
    """
    return tenant_id in _public_tenants()
