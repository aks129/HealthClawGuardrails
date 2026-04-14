"""
Tests for scripts/import_healthex.py

Uses unittest.mock to avoid real HTTP calls. Verifies:
- Step-up token is generated and sent in X-Step-Up-Token header
- X-Tenant-ID and X-Human-Confirmed headers are set correctly
- Correct endpoint is called
- Dry-run skips the HTTP call
- Missing bundle file exits with an error
- Non-Bundle resourceType exits with an error
"""

import json
import os
import sys
import importlib
import types
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on path so import_healthex can find r6.stepup
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_BUNDLE = {
    "resourceType": "Bundle",
    "type": "transaction",
    "entry": [
        {
            "resource": {
                "resourceType": "Patient",
                "name": [{"family": "Rivera", "given": ["Maria"]}],
                "birthDate": "1985-03-15",
            },
            "request": {"method": "POST", "url": "Patient"},
        }
    ],
}

SAMPLE_CONTEXT_RESPONSE = {
    "context_id": "ctx-abc123",
    "tenant_id": "test-tenant",
    "resource_count": 1,
    "resource_types": ["Patient"],
    "patient_reference": "Patient/test-patient-1",
    "created_at": "2026-04-07T00:00:00Z",
}


def _make_mock_response(status_code=201, json_data=None):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data or SAMPLE_CONTEXT_RESPONSE
    mock.text = json.dumps(json_data or SAMPLE_CONTEXT_RESPONSE)
    return mock


# ---------------------------------------------------------------------------
# Import the module under test (deferred so sys.path is set first)
# ---------------------------------------------------------------------------

def _import_module():
    spec = importlib.util.spec_from_file_location(
        "import_healthex",
        os.path.join(REPO_ROOT, "scripts", "import_healthex.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestImportBundle:
    """Tests for import_bundle() function."""

    def test_step_up_token_header_is_set(self, tmp_path):
        mod = _import_module()

        with patch("requests.post", return_value=_make_mock_response()) as mock_post:
            mod.import_bundle(
                bundle=SAMPLE_BUNDLE,
                tenant_id="test-tenant",
                step_up_secret="test-secret-for-hmac-validation",
                base_url="http://localhost:5000/r6/fhir",
            )

        call_kwargs = mock_post.call_args
        headers = call_kwargs[1]["headers"] if call_kwargs[1] else call_kwargs[0][1]
        assert "X-Step-Up-Token" in headers
        assert len(headers["X-Step-Up-Token"]) > 20

    def test_tenant_id_header_is_set(self):
        mod = _import_module()

        with patch("requests.post", return_value=_make_mock_response()) as mock_post:
            mod.import_bundle(
                bundle=SAMPLE_BUNDLE,
                tenant_id="my-patient-123",
                step_up_secret="test-secret-for-hmac-validation",
                base_url="http://localhost:5000/r6/fhir",
            )

        headers = mock_post.call_args[1]["headers"]
        assert headers["X-Tenant-ID"] == "my-patient-123"

    def test_human_confirmed_header_is_set(self):
        mod = _import_module()

        with patch("requests.post", return_value=_make_mock_response()) as mock_post:
            mod.import_bundle(
                bundle=SAMPLE_BUNDLE,
                tenant_id="test-tenant",
                step_up_secret="test-secret-for-hmac-validation",
                base_url="http://localhost:5000/r6/fhir",
            )

        headers = mock_post.call_args[1]["headers"]
        assert headers["X-Human-Confirmed"] == "true"

    def test_posts_to_ingest_context_endpoint(self):
        mod = _import_module()

        with patch("requests.post", return_value=_make_mock_response()) as mock_post:
            mod.import_bundle(
                bundle=SAMPLE_BUNDLE,
                tenant_id="test-tenant",
                step_up_secret="test-secret-for-hmac-validation",
                base_url="http://localhost:5000/r6/fhir",
            )

        url = mock_post.call_args[0][0]
        assert url.endswith("/Bundle/$ingest-context")

    def test_dry_run_skips_http(self):
        mod = _import_module()

        with patch("requests.post") as mock_post:
            result = mod.import_bundle(
                bundle=SAMPLE_BUNDLE,
                tenant_id="test-tenant",
                step_up_secret="test-secret-for-hmac-validation",
                base_url="http://localhost:5000/r6/fhir",
                dry_run=True,
            )

        mock_post.assert_not_called()
        assert result is None

    def test_http_error_exits(self):
        mod = _import_module()

        with patch("requests.post", return_value=_make_mock_response(status_code=400, json_data={"error": "bad request"})):
            with pytest.raises(SystemExit):
                mod.import_bundle(
                    bundle=SAMPLE_BUNDLE,
                    tenant_id="test-tenant",
                    step_up_secret="test-secret-for-hmac-validation",
                    base_url="http://localhost:5000/r6/fhir",
                )

    def test_missing_secret_exits(self):
        mod = _import_module()

        with pytest.raises(SystemExit):
            mod.import_bundle(
                bundle=SAMPLE_BUNDLE,
                tenant_id="test-tenant",
                step_up_secret="",
                base_url="http://localhost:5000/r6/fhir",
            )


class TestMain:
    """Integration-level tests for the CLI entry point."""

    def test_missing_bundle_file_exits(self):
        mod = _import_module()

        with pytest.raises(SystemExit):
            with patch("sys.argv", ["import_healthex.py", "--bundle-file", "/nonexistent.json", "--step-up-secret", "x"]):
                mod.main()

    def test_non_bundle_resourcetype_exits(self, tmp_path):
        mod = _import_module()
        bad_file = tmp_path / "bad.json"
        bad_file.write_text(json.dumps({"resourceType": "Patient"}))

        with pytest.raises(SystemExit):
            with patch("sys.argv", ["import_healthex.py", "--bundle-file", str(bad_file), "--step-up-secret", "x"]):
                mod.main()

    def test_successful_import_prints_context_id(self, tmp_path, capsys):
        mod = _import_module()
        bundle_file = tmp_path / "bundle.json"
        bundle_file.write_text(json.dumps(SAMPLE_BUNDLE))

        with patch("requests.post", return_value=_make_mock_response()), \
             patch("requests.get", return_value=_make_mock_response(status_code=200, json_data=SAMPLE_CONTEXT_RESPONSE)):
            with patch("sys.argv", [
                "import_healthex.py",
                "--bundle-file", str(bundle_file),
                "--tenant-id", "test-tenant",
                "--step-up-secret", "test-secret-for-hmac-validation",
            ]):
                mod.main()

        captured = capsys.readouterr()
        assert "ctx-abc123" in captured.out
