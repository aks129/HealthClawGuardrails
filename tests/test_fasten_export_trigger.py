"""EHI export trigger — Fasten exports do NOT fire automatically.

Root cause of the 2026-07-08 pre-flight stall: after a connection, records
are retrieved only when POST /v1/bridge/fhir/ehi-export is called with the
org_connection_id (idempotent). Nobody called it. The trigger now fires
server-side when a connection is webhook-verified.
"""

import json
from unittest.mock import patch, MagicMock

from r6.fasten.api import trigger_ehi_export


class TestTrigger:
    def test_posts_to_fasten_with_basic_auth(self, monkeypatch):
        monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public_test_id")
        monkeypatch.setenv("FASTEN_PRIVATE_KEY", "private_test_key")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"success": True,
                                  "data": {"task_id": "t1", "status": "pending"}}
        with patch("r6.fasten.api.requests.post", return_value=resp) as post:
            out = trigger_ehi_export("oc-123")
        assert out == {"task_id": "t1", "status": "pending"}
        args, kwargs = post.call_args
        assert args[0].endswith("/bridge/fhir/ehi-export")
        assert kwargs["auth"] == ("public_test_id", "private_test_key")
        assert kwargs["json"] == {"org_connection_id": "oc-123"}

    def test_missing_keys_returns_none(self, monkeypatch):
        monkeypatch.delenv("FASTEN_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("FASTEN_PRIVATE_KEY", raising=False)
        assert trigger_ehi_export("oc-123") is None

    def test_http_error_returns_none(self, monkeypatch):
        monkeypatch.setenv("FASTEN_PUBLIC_KEY", "p")
        monkeypatch.setenv("FASTEN_PRIVATE_KEY", "k")
        resp = MagicMock(status_code=500)
        with patch("r6.fasten.api.requests.post", return_value=resp):
            assert trigger_ehi_export("oc-123") is None


class TestWebhookTriggersExport:
    def test_connection_success_triggers_export(self, client, monkeypatch):
        payload = {"type": "patient.connection_success",
                   "data": {"org_connection_id": "oc-trig-1",
                            "external_id": "trig-tenant"}}
        with patch("r6.fasten.routes.verify_webhook", return_value=True), \
             patch("r6.fasten.routes.trigger_ehi_export") as trig:
            resp = client.post("/fasten/webhook", data=json.dumps(payload),
                               content_type="application/json")
        assert resp.status_code == 200
        trig.assert_called_once_with("oc-trig-1")
