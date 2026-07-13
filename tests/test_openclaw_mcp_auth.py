"""Regression coverage for OpenClaw's authenticated MCP HTTP transport."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import yaml


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-mcp-auth")
sys.path.insert(0, str(ROOT / "openclaw"))

import bot  # noqa: E402


def test_rpc_sends_configured_mcp_bearer_token(monkeypatch):
    response = MagicMock()
    response.json.return_value = {"result": {"ok": True}}
    monkeypatch.setattr(bot, "MCP_AUTH_TOKEN", "test-mcp-secret")
    post = MagicMock(return_value=response)
    monkeypatch.setattr(bot.requests, "post", post)

    assert bot._rpc("fhir_search", resource_type="Patient") == {"ok": True}

    post.assert_called_once()
    assert post.call_args.kwargs["headers"] == {
        "Authorization": "Bearer test-mcp-secret"
    }


def test_rpc_keeps_unauthenticated_local_http_compatibility(monkeypatch):
    response = MagicMock()
    response.json.return_value = {"result": {"ok": True}}
    monkeypatch.setattr(bot, "MCP_AUTH_TOKEN", "")
    post = MagicMock(return_value=response)
    monkeypatch.setattr(bot.requests, "post", post)

    bot._rpc("fhir_search", resource_type="Patient")

    assert post.call_args.kwargs["headers"] == {}


def test_openclaw_compose_profile_requires_and_receives_mcp_token():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    environment = compose["services"]["openclaw"]["environment"]

    assert (
        "MCP_AUTH_TOKEN=${MCP_AUTH_TOKEN:?MCP_AUTH_TOKEN is required}" in environment
    )
