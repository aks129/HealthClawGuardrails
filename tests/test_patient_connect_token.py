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


class TestFastenRegistrationIssuesAgentAccess:
    def _register(self, client, tenant, org_id):
        return client.post(
            "/fasten/connections",
            headers={"X-Tenant-Id": tenant, "Content-Type": "application/json"},
            data=json.dumps({"org_connection_id": org_id,
                             "platform_type": "epic"}))

    def test_new_connection_on_fresh_tenant_returns_agent_access(self, client):
        resp = self._register(client, "fresh-fasten-tenant", "org-conn-abc-1")
        assert resp.status_code == 201
        body = resp.get_json()
        access = body.get("agent_access")
        assert access, body
        assert access["tenant_id"] == "fresh-fasten-tenant"
        assert access["read_token"]
        assert access["expires_at"]
        # the issued token reads but does not write
        valid, err = validate_step_up_token(
            access["read_token"], "fresh-fasten-tenant", require_scope=None)
        assert valid is True, err
        valid, _ = validate_step_up_token(
            access["read_token"], "fresh-fasten-tenant")
        assert valid is False

    def test_duplicate_registration_does_not_reissue(self, client):
        first = self._register(client, "dup-fasten-tenant", "org-conn-dup-1")
        assert first.status_code == 201
        again = self._register(client, "dup-fasten-tenant", "org-conn-dup-1")
        assert again.status_code == 200
        assert "agent_access" not in (again.get_json() or {})

    def test_tenant_with_existing_data_gets_no_token(self, client,
                                                     sample_patient):
        # Pre-claim protection: registering a connection against a tenant
        # that already holds data must NOT hand out a read token.
        tenant = "occupied-fasten-tenant"
        write_tok = generate_step_up_token(tenant)
        seeded = client.post(
            "/r6/fhir/Patient",
            headers={"X-Tenant-Id": tenant, "X-Step-Up-Token": write_tok,
                     "X-Human-Confirmed": "true",
                     "Content-Type": "application/fhir+json"},
            data=json.dumps(sample_patient))
        assert seeded.status_code == 201

        resp = self._register(client, tenant, "org-conn-occupied-1")
        assert resp.status_code == 201
        assert "agent_access" not in (resp.get_json() or {})

    def test_second_connection_same_tenant_does_not_reissue(self, client):
        tenant = "two-conn-tenant"
        first = self._register(client, tenant, "org-conn-two-1")
        assert first.status_code == 201
        assert first.get_json().get("agent_access")
        second = self._register(client, tenant, "org-conn-two-2")
        assert second.status_code == 201
        assert "agent_access" not in (second.get_json() or {})
