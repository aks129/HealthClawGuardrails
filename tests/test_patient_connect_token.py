"""Patient connect token — long-lived READ-scoped tokens for own-data agents.

The non-technical-user flow: a patient completes an identity-verified Fasten
connection, and the registration response hands them a read-scoped token their
AI agent uses to read their own tenant. Scope rules keep H4 intact:

- scope='read' tokens authenticate READS (authenticate_tenant_read) only
- write paths reject scope='read' tokens (default require_scope='write')
- legacy tokens (no scope claim) keep full write behavior — back-compat
"""

import json

from r6.stepup import generate_step_up_token, validate_step_up_token


TENANT = "connect-token-tenant"  # not in PUBLIC_TENANTS -> read auth required


def _mint(scope=None, ttl=3600):
    return generate_step_up_token(TENANT, ttl_seconds=ttl, scope=scope)


class TestScopeClaim:
    def test_read_scope_rejected_for_writes(self):
        tok = _mint(scope="read")
        valid, err = validate_step_up_token(tok, TENANT)  # default: write
        assert valid is False
        assert "read" in (err or "").lower()

    def test_read_scope_accepted_when_scope_not_required(self):
        tok = _mint(scope="read")
        valid, err = validate_step_up_token(tok, TENANT, require_scope=None)
        assert valid is True, err

    def test_legacy_token_still_authorizes_writes(self):
        tok = _mint()  # no scope claim — the historical token shape
        valid, err = validate_step_up_token(tok, TENANT)
        assert valid is True, err

    def test_write_scope_token_authorizes_writes(self):
        tok = _mint(scope="write")
        valid, err = validate_step_up_token(tok, TENANT)
        assert valid is True, err

    def test_read_scope_token_still_tenant_bound(self):
        tok = _mint(scope="read")
        valid, _ = validate_step_up_token(tok, "other-tenant", require_scope=None)
        assert valid is False


class TestReadPathAcceptsReadScope:
    def test_read_token_reads_nonpublic_tenant(self, client, sample_patient):
        # Seed via a write (full-scope token), then read back with a
        # read-scoped token — the own-data agent pattern.
        write_tok = _mint()
        created = client.post(
            "/r6/fhir/Patient",
            headers={"X-Tenant-Id": TENANT, "X-Step-Up-Token": write_tok,
                     "X-Human-Confirmed": "true",
                     "Content-Type": "application/fhir+json"},
            data=json.dumps(sample_patient))
        assert created.status_code == 201, created.get_data(as_text=True)
        pid = created.get_json()["id"]

        read_tok = _mint(scope="read")
        got = client.get(
            f"/r6/fhir/Patient/{pid}",
            headers={"X-Tenant-Id": TENANT, "X-Step-Up-Token": read_tok})
        assert got.status_code == 200

    def test_read_token_cannot_write(self, client, sample_patient):
        read_tok = _mint(scope="read")
        resp = client.post(
            "/r6/fhir/Patient",
            headers={"X-Tenant-Id": TENANT, "X-Step-Up-Token": read_tok,
                     "X-Human-Confirmed": "true",
                     "Content-Type": "application/fhir+json"},
            data=json.dumps(sample_patient))
        assert resp.status_code == 401


class TestWebhookGatedAgentAccess:
    """Token issuance is gated on the HMAC-verified connection_success webhook.

    Pre-claim protection: a fabricated org_connection_id never gets webhook
    verification, so registering it yields no token — ever."""

    def _register(self, client, tenant, org_id):
        return client.post(
            "/fasten/connections",
            headers={"X-Tenant-Id": tenant, "Content-Type": "application/json"},
            data=json.dumps({"org_connection_id": org_id,
                             "platform_type": "epic"}))

    def _webhook_verify(self, client, tenant, org_id):
        from unittest.mock import patch
        payload = {"type": "patient.connection_success",
                   "data": {"org_connection_id": org_id,
                            "external_id": tenant}}
        with patch("r6.fasten.routes.verify_webhook", return_value=True):
            return client.post("/fasten/webhook", data=json.dumps(payload),
                               content_type="application/json")

    def _access(self, client, tenant, org_id):
        return client.get(f"/fasten/connections/{org_id}/agent-access",
                          headers={"X-Tenant-Id": tenant})

    def test_registration_alone_yields_no_token(self, client):
        resp = self._register(client, "preclaim-tenant", "oc-preclaim-1")
        assert resp.status_code == 201
        assert "agent_access" not in (resp.get_json() or {})
        # unverified connection: poll says pending, forever
        poll = self._access(client, "preclaim-tenant", "oc-preclaim-1")
        assert poll.status_code == 202
        assert poll.get_json().get("pending") is True

    def test_webhook_verification_unlocks_one_time_mint(self, client):
        t, oc = "verified-tenant", "oc-verified-1"
        assert self._register(client, t, oc).status_code == 201
        assert self._webhook_verify(client, t, oc).status_code == 200
        first = self._access(client, t, oc)
        assert first.status_code == 200, first.get_data(as_text=True)
        body = first.get_json()
        assert body["tenant_id"] == t and body["scope"] == "read"
        valid, err = validate_step_up_token(body["read_token"], t,
                                            require_scope=None)
        assert valid is True, err
        # read token still rejected on writes
        valid, _ = validate_step_up_token(body["read_token"], t)
        assert valid is False
        # mint-once
        again = self._access(client, t, oc)
        assert again.status_code == 410

    def test_wrong_tenant_cannot_poll(self, client):
        t, oc = "owner-tenant", "oc-owner-1"
        self._register(client, t, oc)
        self._webhook_verify(client, t, oc)
        resp = self._access(client, "attacker-tenant", oc)
        assert resp.status_code == 404

    def test_second_connection_same_tenant_gets_no_token(self, client):
        t = "second-conn-tenant"
        self._register(client, t, "oc-2nd-a")
        self._webhook_verify(client, t, "oc-2nd-a")
        assert self._access(client, t, "oc-2nd-a").status_code == 200
        self._register(client, t, "oc-2nd-b")
        self._webhook_verify(client, t, "oc-2nd-b")
        assert self._access(client, t, "oc-2nd-b").status_code == 409

    def test_webhook_created_connection_is_verified_and_mintable(self, client):
        # connection_success arriving before the page registers: webhook
        # creates the row already verified; the page poll can mint.
        t, oc = "webhook-first-tenant", "oc-whfirst-1"
        assert self._webhook_verify(client, t, oc).status_code == 200
        resp = self._access(client, t, oc)
        assert resp.status_code == 200
