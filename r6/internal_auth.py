"""Shared internal-secret authorization for infrastructure/operator surfaces.

The internal-secret scheme authenticates a caller as *infrastructure*, not as
a tenant: a request presents `X-Internal-Secret`, matched constant-time against
`INTERNAL_TOKEN_MINT_SECRET`, fail-closed in production. Unlike the mint gate
(`_internal_mint_authorized`) it grants NO public-tenant exemption — a public
tenant bypasses read-auth, but that says nothing about whether a caller may
drive an operator action across all tenants.

Extracted so `/internal/*` ingestion (#267) and `/r6/ops/*` (#304) share one
gate rather than each carrying its own copy. Tenant-independent by design.
"""
import os

from flask import request

from r6 import constant_time
from r6.runtime_config import resolve_app_env


def internal_secret_authorized():
    """True iff the caller presents the configured internal secret.

    If `INTERNAL_TOKEN_MINT_SECRET` is unset, the surface is open only outside
    production (backward compatible for local/dev and the test harness) and
    refused in production. Takes no tenant argument: this authenticates the
    caller as infrastructure, not as any one tenant.
    """
    mint_secret = os.environ.get('INTERNAL_TOKEN_MINT_SECRET')
    if mint_secret:
        provided = request.headers.get('X-Internal-Secret', '')
        return constant_time.equal(provided, mint_secret)
    return resolve_app_env() != 'production'
