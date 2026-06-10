"""Tests for scripts/export_healthbankone_mcp.py and scripts/healthbankone_oauth.py."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add scripts/ to path so we can import directly
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from export_healthbankone_mcp import (
    _apply_redaction,
    _is_read_safe,
    _resolve_token,
    _try_refresh,
    _unwrap,
)


# ── _resolve_token ─────────────────────────────────────────────────────────────

class TestResolveToken:
    def test_env_var_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HBO_ACCESS_TOKEN", "env-tok")
        monkeypatch.setenv("HBO_TOKEN_CACHE", str(tmp_path / "tokens.json"))
        assert _resolve_token() == "env-tok"

    def test_cache_file_valid(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HBO_ACCESS_TOKEN", raising=False)
        cache = tmp_path / "tokens.json"
        future = time.time() + 3600
        cache.write_text(json.dumps({
            "access_token": "cached-tok",
            "expires_at": future,
        }))
        # TOKEN_CACHE is a module-level Path computed at import time, so patch it directly
        import export_healthbankone_mcp as m
        monkeypatch.setattr(m, "TOKEN_CACHE", cache)
        assert _resolve_token() == "cached-tok"

    def test_cache_file_expired_returns_none_without_refresh(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HBO_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("HBO_TOKEN_ENDPOINT", raising=False)
        cache = tmp_path / "tokens.json"
        cache.write_text(json.dumps({
            "access_token": "old-tok",
            "expires_at": time.time() - 10,
        }))
        monkeypatch.setenv("HBO_TOKEN_CACHE", str(cache))
        # No refresh config → returns None
        assert _resolve_token() is None

    def test_no_cache_no_env_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HBO_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("HBO_TOKEN_CACHE", str(tmp_path / "missing.json"))
        assert _resolve_token() is None

    def test_corrupt_cache_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HBO_ACCESS_TOKEN", raising=False)
        cache = tmp_path / "tokens.json"
        cache.write_text("not json{{{")
        monkeypatch.setenv("HBO_TOKEN_CACHE", str(cache))
        assert _resolve_token() is None


# ── _try_refresh ───────────────────────────────────────────────────────────────

class TestTryRefresh:
    def test_missing_config_returns_none(self, monkeypatch):
        monkeypatch.delenv("HBO_TOKEN_ENDPOINT", raising=False)
        result = _try_refresh({"refresh_token": "r"})
        assert result is None

    def test_missing_refresh_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("HBO_TOKEN_ENDPOINT", "https://example.com/token")
        monkeypatch.setenv("HBO_CLIENT_ID", "cid")
        result = _try_refresh({})
        assert result is None

    def test_successful_refresh_updates_cache(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HBO_TOKEN_ENDPOINT", "https://example.com/token")
        monkeypatch.setenv("HBO_CLIENT_ID", "cid")
        monkeypatch.delenv("HBO_CLIENT_SECRET", raising=False)
        cache_path = tmp_path / "tokens.json"
        monkeypatch.setenv("HBO_TOKEN_CACHE", str(cache_path))

        import httpx
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "access_token": "new-tok",
            "refresh_token": "new-ref",
            "expires_in": 3600,
        }

        with patch("httpx.post", return_value=mock_resp):
            # Patch TOKEN_CACHE inside the module
            import export_healthbankone_mcp as m
            orig = m.TOKEN_CACHE
            m.TOKEN_CACHE = cache_path
            try:
                result = _try_refresh({"refresh_token": "old-ref"})
            finally:
                m.TOKEN_CACHE = orig

        assert result == "new-tok"

    def test_http_error_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HBO_TOKEN_ENDPOINT", "https://example.com/token")
        monkeypatch.setenv("HBO_CLIENT_ID", "cid")
        monkeypatch.setenv("HBO_TOKEN_CACHE", str(tmp_path / "tokens.json"))

        import httpx
        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            result = _try_refresh({"refresh_token": "r"})
        assert result is None


# ── _is_read_safe ──────────────────────────────────────────────────────────────

class TestIsReadSafe:
    def _make_tool(self, name: str, read_only_hint=None):
        tool = MagicMock()
        tool.name = name
        if read_only_hint is not None:
            tool.annotations = MagicMock()
            tool.annotations.readOnlyHint = read_only_hint
        else:
            tool.annotations = None
        return tool

    def test_annotation_true_safe(self):
        assert _is_read_safe(self._make_tool("something", read_only_hint=True))

    def test_annotation_false_unsafe(self):
        assert not _is_read_safe(self._make_tool("get_stuff", read_only_hint=False))

    def test_write_name_fragment_unsafe(self):
        for name in ("create_record", "update_patient", "delete_entry",
                     "submit_form", "post_document"):
            assert not _is_read_safe(self._make_tool(name))

    def test_read_name_safe(self):
        for name in ("health.summary", "get_conditions", "list_medications",
                     "identity.verify", "search_records"):
            assert _is_read_safe(self._make_tool(name))

    def test_no_annotation_write_name_unsafe(self):
        assert not _is_read_safe(self._make_tool("send_message"))

    def test_no_annotation_neutral_name_safe(self):
        assert _is_read_safe(self._make_tool("health.context"))


# ── _unwrap ────────────────────────────────────────────────────────────────────

class TestUnwrap:
    def test_structured_content_preferred(self):
        result = MagicMock()
        result.structuredContent = {"key": "val"}
        assert _unwrap(result) == {"key": "val"}

    def test_single_json_text_block(self):
        result = MagicMock()
        result.structuredContent = None
        block = MagicMock()
        block.text = '{"fhir": "bundle"}'
        result.content = [block]
        assert _unwrap(result) == {"fhir": "bundle"}

    def test_multiple_blocks_returns_list(self):
        result = MagicMock()
        result.structuredContent = None
        b1, b2 = MagicMock(), MagicMock()
        b1.text = '"a"'
        b2.text = '"b"'
        result.content = [b1, b2]
        assert _unwrap(result) == ["a", "b"]

    def test_plain_text_block(self):
        result = MagicMock()
        result.structuredContent = None
        block = MagicMock()
        block.text = "not json"
        result.content = [block]
        assert _unwrap(result) == "not json"

    def test_no_content_returns_empty_list(self):
        result = MagicMock()
        result.structuredContent = None
        result.content = []
        assert _unwrap(result) == []


# ── _apply_redaction ───────────────────────────────────────────────────────────

class TestApplyRedaction:
    def test_none_mode_passthrough(self):
        payload = {"resourceType": "Patient", "name": [{"family": "Smith"}]}
        from healthclaw_redact import RedactionStats
        out, stats = _apply_redaction(payload, "none", "t1", "http://x")
        assert out["name"][0]["family"] == "Smith"

    def test_local_mode_redacts_family_name(self):
        payload = {
            "resourceType": "Patient",
            "name": [{"family": "Smith", "given": ["John"]}],
        }
        out, stats = _apply_redaction(payload, "local", "t1", "http://x")
        # PHI redaction must remove or mask the full family name "Smith"
        family = out.get("name", [{}])[0].get("family", "")
        assert family != "Smith", f"family name not redacted: {family!r}"
