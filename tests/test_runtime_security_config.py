"""Startup security invariants for production deployments."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


_GOOD_PRODUCTION_ENV = {
    "APP_ENV": "production",
    "SESSION_SECRET": "session-secret-with-at-least-32-random-characters",
    "STEP_UP_SECRET": "step-up-secret-with-at-least-32-random-characters",
    "READ_AUTH_ENABLED": "true",
    # An explicitly empty allowlist is valid for private-only deployments.
    "PUBLIC_TENANTS": "",
    "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
    "DISABLE_COMMAND_CENTER": "1",
    "REDIS_URL": "redis://localhost:6379/0",
}


def _startup(overrides=None, removed=(), code="import main"):
    env = os.environ.copy()
    for name in (
        "APP_ENV",
        "FLASK_ENV",
        "SESSION_SECRET",
        "STEP_UP_SECRET",
        "READ_AUTH_ENABLED",
        "PUBLIC_TENANTS",
        "OPEN_WEARABLES_URL",
        "VERCEL",
    ):
        env.pop(name, None)
    env.update(_GOOD_PRODUCTION_ENV)
    env.update(overrides or {})
    for name in removed:
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    "name,value",
    [
        ("SESSION_SECRET", ""),
        ("SESSION_SECRET", "a-development-secret-key"),
        ("SESSION_SECRET", "change-me-in-prod"),
        ("STEP_UP_SECRET", ""),
        ("STEP_UP_SECRET", "change-me-hmac-secret"),
        ("STEP_UP_SECRET", "dev-step-up-secret-change-in-production"),
        ("SESSION_SECRET", "too-short"),
        ("STEP_UP_SECRET", "too-short"),
    ],
)
def test_production_rejects_missing_or_default_secrets(name, value):
    result = _startup({name: value})
    assert result.returncode != 0
    assert name in (result.stdout + result.stderr)


@pytest.mark.parametrize("value", ["", "false", "0", "no"])
def test_production_rejects_disabled_read_auth(value):
    result = _startup({"READ_AUTH_ENABLED": value})
    assert result.returncode != 0
    assert "READ_AUTH_ENABLED" in (result.stdout + result.stderr)


def test_production_requires_public_tenants_to_be_explicit():
    result = _startup(removed=("PUBLIC_TENANTS",))
    assert result.returncode != 0
    assert "PUBLIC_TENANTS" in (result.stdout + result.stderr)


def test_production_requires_shared_redis_state():
    result = _startup(removed=("REDIS_URL",))
    assert result.returncode != 0
    assert "REDIS_URL" in (result.stdout + result.stderr)


def test_explicit_empty_public_tenant_allowlist_is_valid():
    result = _startup()
    assert result.returncode == 0, result.stdout + result.stderr


def test_app_env_production_is_the_canonical_production_switch():
    result = _startup({"SESSION_SECRET": ""})
    assert result.returncode != 0
    assert "SESSION_SECRET" in (result.stdout + result.stderr)


def test_invalid_app_env_is_rejected():
    result = _startup({"APP_ENV": "staging"})
    assert result.returncode != 0
    assert "APP_ENV" in (result.stdout + result.stderr)


def test_conflicting_app_and_flask_environments_are_rejected():
    result = _startup({"APP_ENV": "development", "FLASK_ENV": "production"})
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "APP_ENV" in output and "FLASK_ENV" in output


def test_legacy_flask_env_production_remains_supported():
    result = _startup({"FLASK_ENV": "production"}, removed=("APP_ENV",))
    assert result.returncode == 0, result.stdout + result.stderr


def test_testing_environment_keeps_local_defaults_compatible():
    result = _startup(
        {"APP_ENV": "testing"},
        removed=(
            "SESSION_SECRET",
            "STEP_UP_SECRET",
            "READ_AUTH_ENABLED",
            "PUBLIC_TENANTS",
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_production_session_cookie_flags_are_hardened():
    code = """
import json
from main import app
print(json.dumps({
    'secure': app.config['SESSION_COOKIE_SECURE'],
    'httponly': app.config['SESSION_COOKIE_HTTPONLY'],
    'samesite': app.config['SESSION_COOKIE_SAMESITE'],
}))
"""
    result = _startup(code=code)
    assert result.returncode == 0, result.stdout + result.stderr
    flags = json.loads(result.stdout.strip().splitlines()[-1])
    assert flags == {"secure": True, "httponly": True, "samesite": "Lax"}


def test_app_env_production_closes_internal_token_mint(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("INTERNAL_TOKEN_MINT_SECRET", raising=False)
    monkeypatch.setenv("PUBLIC_TENANTS", "desktop-demo")

    response = client.post(
        "/r6/fhir/internal/step-up-token",
        json={"tenant_id": "private-tenant"},
    )
    assert response.status_code == 403
    assert "token" not in response.get_json()
