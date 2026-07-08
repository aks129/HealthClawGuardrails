"""Contract tests for the SmartHealthConnect bridge + OAuth callback brokers.

These five live routes had zero tests (audit 2026-07-08) — the same
untested-live-path class as the Fasten webhook envelope bug. Pins:
/shc/ingest auth + bundle shape, and the MEDENT/HBO code/state round-trip.
"""

import json
from unittest.mock import patch


SECRET = "shc-test-secret"


def _ingest(client, body, secret=SECRET, tenant="shc-tenant", extra_headers=None):
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["Authorization"] = f"Bearer {secret}"
    if tenant is not None:
        headers["X-Tenant-Id"] = tenant
    headers.update(extra_headers or {})
    return client.post("/shc/ingest", data=json.dumps(body), headers=headers)


def _bundle(n=1):
    return {"resourceType": "Bundle", "type": "transaction",
            "entry": [{"resource": {"resourceType": "Observation",
                                    "status": "final",
                                    "code": {"text": f"obs-{i}"}}}
                      for i in range(n)]}


class TestShcIngest:
    def test_valid_bundle_accepted(self, client, monkeypatch):
        monkeypatch.setenv("SHC_WEBHOOK_SECRET", SECRET)
        with patch("r6.shc.routes.threading.Thread") as t:
            resp = _ingest(client, _bundle(3))
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["received"] is True and body["entries"] == 3
        assert body["job_id"]
        assert t.called

    def test_wrong_secret_401(self, client, monkeypatch):
        monkeypatch.setenv("SHC_WEBHOOK_SECRET", SECRET)
        assert _ingest(client, _bundle(), secret="wrong").status_code == 401

    def test_missing_auth_401(self, client, monkeypatch):
        monkeypatch.setenv("SHC_WEBHOOK_SECRET", SECRET)
        assert _ingest(client, _bundle(), secret=None).status_code == 401

    def test_unset_secret_fails_closed(self, client, monkeypatch):
        monkeypatch.delenv("SHC_WEBHOOK_SECRET", raising=False)
        assert _ingest(client, _bundle()).status_code == 401

    def test_missing_tenant_400(self, client, monkeypatch):
        monkeypatch.setenv("SHC_WEBHOOK_SECRET", SECRET)
        assert _ingest(client, _bundle(), tenant=None).status_code == 400

    def test_non_bundle_rejected(self, client, monkeypatch):
        monkeypatch.setenv("SHC_WEBHOOK_SECRET", SECRET)
        resp = _ingest(client, {"resourceType": "Patient"})
        assert resp.status_code == 400

    def test_empty_bundle_ok_zero_ingested(self, client, monkeypatch):
        monkeypatch.setenv("SHC_WEBHOOK_SECRET", SECRET)
        resp = _ingest(client, {"resourceType": "Bundle", "entry": []})
        assert resp.status_code == 200
        assert resp.get_json()["ingested"] == 0

    def test_bare_entries_unwrapped(self, client, monkeypatch):
        # entry items may be bare resources (no 'resource' wrapper) — line
        # `e.get('resource', e)` must count them
        monkeypatch.setenv("SHC_WEBHOOK_SECRET", SECRET)
        body = {"resourceType": "Bundle",
                "entry": [{"resourceType": "Observation", "status": "final",
                           "code": {"text": "bare"}}]}
        with patch("r6.shc.routes.threading.Thread"):
            resp = _ingest(client, body)
        assert resp.status_code == 200
        assert resp.get_json()["entries"] == 1


class TestOAuthBrokers:
    def test_medent_round_trip(self, client):
        cb = client.get("/shc/medent/callback?code=C-123&state=S-abc")
        assert cb.status_code == 200
        poll = client.get("/shc/medent/code?state=S-abc")
        assert poll.status_code == 200
        assert poll.get_json() == {"code": "C-123", "state": "S-abc"}
        # popped after pickup
        again = client.get("/shc/medent/code?state=S-abc")
        assert again.status_code == 202
        assert again.get_json()["pending"] is True

    def test_medent_error_and_missing_params(self, client):
        assert client.get("/shc/medent/callback?error=denied").status_code == 400
        assert client.get("/shc/medent/callback?code=x").status_code == 400
        assert client.get("/shc/medent/code").status_code == 400

    def test_hbo_round_trip_and_namespacing(self, client):
        cb = client.get("/shc/hbo/callback?code=H-456&state=S-hbo")
        assert cb.status_code == 200
        # HBO state is namespaced: the MEDENT poll route must NOT see it
        cross = client.get("/shc/medent/code?state=S-hbo")
        assert cross.status_code == 202
        poll = client.get("/shc/hbo/code?state=S-hbo")
        assert poll.status_code == 200
        assert poll.get_json()["code"] == "H-456"
