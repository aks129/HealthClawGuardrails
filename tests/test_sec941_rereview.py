"""Re-review of the #941 fixes for SEC941-F1 and SEC941-F2 (head 928af3a).

Synthetic tenant ids only; no real secrets.
"""

from __future__ import annotations

import pytest

from r6.stepup import READ_TOKEN_TTL_SECONDS, generate_step_up_token

SECRET = "sec941-rereview-secret"
CA = "ca-sec941rr001"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_TENANTS", "desktop-demo")
    monkeypatch.delenv("REDIS_URL", raising=False)


def _pc(tenant):
    return generate_step_up_token(tenant, agent_id="patient-connect",
                                  ttl_seconds=READ_TOKEN_TTL_SECONDS,
                                  scope="read")


@pytest.mark.parametrize("path", ["/command-center/api/system",
                                  "/command-center/api/openclaw/sessions"])
def test_both_infra_routes_refuse_a_dead_token_and_accept_a_live_one(
        client, app, env, monkeypatch, path):
    """SEC941-PRE1: a live tenant token never opened these once they became
    operator-only, so the "live" control is the internal secret. A token,
    live or dead, gets the same 403 either side of a Disconnect."""
    from r6.command_center import gateway, projector
    monkeypatch.setattr(gateway, "list_sessions", lambda force=False: [])
    monkeypatch.setattr(projector, "system_status", lambda: {"ok": True})
    c = app.test_client()
    live = c.get(path, headers={"X-Internal-Secret": SECRET})
    assert live.status_code == 200          # control: the operator reaches it
    before = c.get(f"{path}?tenant={CA}", headers={"X-Step-Up-Token": _pc(CA)})
    assert before.status_code == 403
    r = client.post("/r6/fhir/internal/fasten-revoke",
                    json={"tenant_id": CA},
                    headers={"X-Internal-Secret": SECRET})
    assert r.status_code == 200
    dead = c.get(f"{path}?tenant={CA}", headers={"X-Step-Up-Token": _pc(CA)})
    assert dead.status_code == 403
    # Header-only tenant source is refused the same way.
    dead = c.get(path, headers={"X-Step-Up-Token": _pc(CA), "X-Tenant-Id": CA})
    assert dead.status_code == 403
    assert c.get(path, headers={"X-Internal-Secret": SECRET}).status_code == 200


@pytest.mark.parametrize("path", ["/command-center/api/system",
                                  "/command-center/api/openclaw/sessions"])
def test_public_tenant_still_needs_a_credential_on_infra_routes(
        app, env, path):
    """A public tenant waves through read-auth with no credential; these
    routes are operator-only, so it gets the operator refusal."""
    r = app.test_client().get(f"{path}?tenant=desktop-demo")
    assert r.status_code == 403


@pytest.mark.parametrize("blank", [" ", "\t\n", " ", "  "])
def test_blank_secret_variants_keep_every_engine_gate_closed(
        client, monkeypatch, blank):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", blank)
    monkeypatch.setenv("PUBLIC_TENANTS", "desktop-demo")
    for hdr in ({}, {"X-Internal-Secret": ""}, {"X-Internal-Secret": "blank-secret:"}):
        assert client.post("/r6/fhir/internal/step-up-token",
                           json={"tenant_id": CA}, headers=hdr).status_code == 403
        assert client.post("/r6/fhir/internal/fasten-revoke",
                           json={"tenant_id": CA}, headers=hdr).status_code == 403


@pytest.mark.parametrize("blank", [" ", "\t"])
def test_engine_startup_refuses_unicode_blank(monkeypatch, blank):
    from r6.runtime_config import validate_runtime_environment
    with pytest.raises(RuntimeError, match="only whitespace"):
        validate_runtime_environment({"INTERNAL_TOKEN_MINT_SECRET": blank,
                                      "TESTING": "1"})
