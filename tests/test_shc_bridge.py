"""Contract tests for the SmartHealthConnect bridge + OAuth callback brokers.

These five live routes had zero tests (audit 2026-07-08) — the same
untested-live-path class as the Fasten webhook envelope bug. Pins:
/shc/ingest auth + bundle shape, and the MEDENT/HBO code/state round-trip.
"""

import json
import logging
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

    def test_ingest_error_logs_exception_class_not_phi(self, app, caplog):
        """A failing entry must log the exception CLASS name, never the
        exception object. str() on a SQLAlchemy DBAPIError serialises the
        failing statement and its bound parameters — i.e. the FHIR record
        being ingested — so `%s` on the exception leaks PHI into logs. Issue
        #306 (S-4); .github/REVIEW_STANDARDS.md rule 1; docs/2026-08-02-retro.md.

        MUTATION: reverting the fix at r6/shc/routes.py:257 to
        `logger.warning('SHC ingest error (job=%s): %s', job_id, exc)` (logging
        `exc` instead of `type(exc).__name__`) turns this test red.
        """
        from r6.shc import routes as shc_routes

        phi = "Rosa PHI-LEAK Kowalski dob=1971-03-02 ssn=123-45-6789"

        class PoisonedStatementError(Exception):
            pass

        def _boom(resource, tenant_id):
            # Mimic a DBAPIError whose str() echoes the bound parameters.
            raise PoisonedStatementError(
                "(psycopg2.errors.StringDataRightTruncation) INSERT INTO "
                "fhir_resource (...) VALUES (...) -- parameters: "
                f"{{'patient': '{phi}'}}"
            )

        entries = [{"resourceType": "Observation", "status": "final"}]
        with caplog.at_level(logging.WARNING, logger="r6.shc.routes"):
            with patch("r6.fasten.ingester._ingest_one", _boom):
                shc_routes._ingest_bundle(
                    app, entries, "shc-tenant", "flexpa", "job-306-test")

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert phi not in logged, f"PHI leaked into logs: {logged!r}"
        assert "PoisonedStatementError" in logged


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
