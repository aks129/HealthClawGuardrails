"""Validated process environment and production startup invariants."""

from __future__ import annotations

import os
from collections.abc import Mapping


VALID_APP_ENVS = frozenset({"development", "testing", "production"})
TRUE_VALUES = frozenset({"1", "true", "yes"})

_INSECURE_SESSION_SECRETS = frozenset({
    "a-development-secret-key",
    "change-me-in-prod",
})
_INSECURE_STEP_UP_SECRETS = frozenset({
    "change-me-hmac-secret",
    "dev-step-up-secret-change-in-production",
})
_MIN_SECRET_LENGTH = 32


def read_auth_enabled() -> bool:
    """Whether tenant read authentication is explicitly enabled."""
    return os.environ.get("READ_AUTH_ENABLED", "").strip().lower() in TRUE_VALUES


def public_tenants() -> frozenset[str]:
    """The explicit allowlist of synthetic public tenants."""
    raw = os.environ.get("PUBLIC_TENANTS", "").strip()
    if not raw:
        return frozenset()
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def is_public_tenant(tenant_id: str) -> bool:
    return tenant_id in public_tenants()


def resolve_app_env(environ: Mapping[str, str] | None = None) -> str:
    """Resolve one canonical environment while preserving ``FLASK_ENV``.

    ``APP_ENV`` is authoritative when supplied. Older deployments that only
    set ``FLASK_ENV`` retain their behavior, and the test harness can opt in
    through ``TESTING=1``. Explicit but unknown values fail at startup instead
    of silently selecting development behavior.
    """
    env = os.environ if environ is None else environ
    if "APP_ENV" in env:
        source = "APP_ENV"
        value = env.get(source, "")
        if value not in VALID_APP_ENVS:
            allowed = ", ".join(sorted(VALID_APP_ENVS))
            raise RuntimeError(
                f"APP_ENV must be exactly one of: {allowed} "
                "(lowercase, without surrounding whitespace)"
            )
        if "FLASK_ENV" in env:
            legacy = env.get("FLASK_ENV", "").strip().lower()
            if legacy not in VALID_APP_ENVS:
                allowed = ", ".join(sorted(VALID_APP_ENVS))
                raise RuntimeError(f"FLASK_ENV must be one of: {allowed}")
            if legacy != value:
                raise RuntimeError(
                    "APP_ENV and FLASK_ENV must match when both are set"
                )
    elif "FLASK_ENV" in env:
        source = "FLASK_ENV"
        value = env.get(source, "").strip().lower()
    elif env.get("TESTING", "").strip().lower() in TRUE_VALUES:
        return "testing"
    else:
        return "development"

    if value not in VALID_APP_ENVS:
        allowed = ", ".join(sorted(VALID_APP_ENVS))
        raise RuntimeError(f"{source} must be one of: {allowed}")
    return value


def validate_runtime_environment(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Validate fail-closed production settings and return the app env."""
    env = os.environ if environ is None else environ
    app_env = resolve_app_env(env)

    # EVERY environment, not just production: an unknown FHIR_UPSTREAM_KIND is
    # a typo, and a typo here produced a deployment that STARTED, reported
    # itself started, and then answered 500 to every request including
    # /health — an opaque HTML error page, while the registry's own excellent
    # explanation went to a log nobody reads during a deploy.
    #
    # resolve_upstream_config raises with that explanation. Calling it here
    # moves the failure to boot, where an orchestrator keeps the previous
    # version running instead of rolling out a broken one. It reads the
    # environment and returns a dataclass: no client is built and no network
    # call is made, so this costs nothing when the config is right.
    from r6.upstream_connectors import resolve_upstream_config
    resolve_upstream_config(environ=env)

    if app_env != "production":
        return app_env

    if env.get("READ_AUTH_ENABLED", "").strip().lower() not in TRUE_VALUES:
        raise RuntimeError("READ_AUTH_ENABLED must be true in production")

    # Presence is distinct from truthiness: an explicit empty value is the
    # safest private-only allowlist and must remain valid.
    if "PUBLIC_TENANTS" not in env:
        raise RuntimeError(
            "PUBLIC_TENANTS must be explicitly set in production "
            "(use an empty value for no public tenants)"
        )

    # Vercel serves the public, read-only marketing/demo copy. Mutations are
    # refused by api/index.py before any blueprint handler runs, command-center
    # sessions are disabled, and only explicitly public synthetic data is
    # readable. Requiring signing secrets or shared Redis on that stateless
    # surface would force secrets into vercel.json and prevent cold starts.
    read_only_vercel = (
        env.get("VERCEL", "").strip().lower() in TRUE_VALUES
        and env.get("READ_ONLY_DEPLOYMENT", "").strip().lower() in TRUE_VALUES
    )
    if read_only_vercel:
        if env.get("DISABLE_COMMAND_CENTER", "").strip().lower() not in TRUE_VALUES:
            raise RuntimeError(
                "DISABLE_COMMAND_CENTER must be true for a read-only Vercel deployment"
            )
        public = {
            tenant.strip()
            for tenant in env.get("PUBLIC_TENANTS", "").split(",")
            if tenant.strip()
        }
        if public != {"desktop-demo"}:
            raise RuntimeError(
                "read-only Vercel PUBLIC_TENANTS must be exactly desktop-demo"
            )
        return app_env

    session_secret = env.get("SESSION_SECRET", "").strip()
    if (
        len(session_secret) < _MIN_SECRET_LENGTH
        or session_secret in _INSECURE_SESSION_SECRETS
    ):
        raise RuntimeError(
            "SESSION_SECRET must be explicitly set to a non-default value "
            "of at least 32 characters in production"
        )

    step_up_secret = env.get("STEP_UP_SECRET", "").strip()
    if (
        len(step_up_secret) < _MIN_SECRET_LENGTH
        or step_up_secret in _INSECURE_STEP_UP_SECRETS
    ):
        raise RuntimeError(
            "STEP_UP_SECRET must be explicitly set to a non-default value "
            "of at least 32 characters in production"
        )

    if not env.get("REDIS_URL", "").strip():
        raise RuntimeError(
            "REDIS_URL is required in production for shared nonce, OAuth, "
            "rate-limit, and worker state"
        )

    return app_env
