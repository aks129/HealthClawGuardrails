"""Who can hold the 30-day patient connect read token, and for how long.

Security review of #940 (the CareAgents patient connect view, #937) found
two server-side gaps that predate it:

SEC940-F1: for a CareAgents tenant, the browser that registered a Fasten
connection held the enrollment proof, so one devtools fetch to
GET .../agent-access minted a 30-day read token. CareAgents reads its
tenants with server-minted tokens and never hands this one out, so
registration no longer establishes enrollment for a CareAgents tenant, and
agent-access falls back to the ordinary tenant read check.

SEC940-F2: a token minted before Disconnect (POST
/r6/fhir/internal/fasten-revoke) kept reading the tenant. A patient-connect
token on a closed tenant is now refused at the read gate. CareAgents' own
tokens keep reading: Disconnect keeps the records already collected (#847).

Synthetic tenant and connection ids only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from models import db
from r6.fasten.models import FastenConnection
from r6.stepup import READ_TOKEN_TTL_SECONDS, generate_step_up_token

SECRET = "token-scope-internal-secret"
CA_TENANT = "ca-00aa11bb22"          # the shape CareAgents generates
DEV_TENANT = "token-scope-dev"       # a developer's own connect page


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public_test_patient")
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_TENANTS", "desktop-demo")
    monkeypatch.delenv("REDIS_URL", raising=False)


def _register(client, tenant, org_id):
    return client.post("/fasten/connections", headers={"X-Tenant-Id": tenant},
                       json={"org_connection_id": org_id})


def _verify(org_id):
    """Fasten's signed connection_success has landed."""
    conn = db.session.get(FastenConnection, org_id)
    conn.webhook_verified_at = datetime.now(timezone.utc)
    db.session.commit()


def _revoke(client, tenant):
    return client.post("/r6/fhir/internal/fasten-revoke",
                       json={"tenant_id": tenant},
                       headers={"X-Internal-Secret": SECRET})


def _patient_connect_token(tenant):
    """A token exactly as agent-access mints it: one already in the wild."""
    return generate_step_up_token(
        tenant, agent_id="patient-connect",
        ttl_seconds=READ_TOKEN_TTL_SECONDS, scope="read")


def _read(app, tenant, token):
    return app.test_client().get(
        "/r6/fhir/Observation",
        headers={"X-Tenant-Id": tenant, "X-Step-Up-Token": token})


# --- SEC940-F1: no browser enrollment for a CareAgents tenant ----------------

def test_careagents_registration_browser_cannot_mint_a_read_token(client, env):
    """MUTATION: drop the CareAgents check in register_connection -> the
    proof cookie is set and agent-access answers 200 with a read token."""
    org_id = "tscope-oc-1"
    reg = _register(client, CA_TENANT, org_id)
    assert reg.status_code == 201
    assert reg.get_json()["agent_access_pending"] is False
    assert db.session.get(FastenConnection, org_id).enrollment_proof_hash is None
    _verify(org_id)

    for headers in ({}, {"X-Tenant-Id": CA_TENANT}):
        minted = client.get(f"/fasten/connections/{org_id}/agent-access",
                            headers=headers)
        assert minted.status_code == 401, headers
        assert "read_token" not in (minted.get_json() or {})
    assert db.session.get(FastenConnection, org_id).agent_token_issued_at is None


def test_careagents_tenant_falls_back_to_the_tenant_read_check(client, env):
    """A caller that can already read the tenant is not refused by F1."""
    org_id = "tscope-oc-2"
    assert _register(client, CA_TENANT, org_id).status_code == 201
    _verify(org_id)
    reader = generate_step_up_token(CA_TENANT, scope="read")
    minted = client.get(f"/fasten/connections/{org_id}/agent-access",
                        headers={"X-Tenant-Id": CA_TENANT,
                                 "X-Step-Up-Token": reader})
    assert minted.status_code == 200


def test_developer_tenant_still_enrolls(client, env):
    org_id = "tscope-oc-3"
    reg = _register(client, DEV_TENANT, org_id)
    assert reg.status_code == 201
    assert reg.get_json()["agent_access_pending"] is True
    _verify(org_id)
    assert client.get(
        f"/fasten/connections/{org_id}/agent-access").status_code == 200


# --- SEC940-F2: Disconnect ends the patient-connect token ---------------------

def test_disconnect_ends_an_existing_read_token(client, app, env):
    """MUTATION: drop the closed-tenant check in authorize_tenant_read ->
    the token keeps answering 200 after the revoke."""
    token = _patient_connect_token(CA_TENANT)
    assert _read(app, CA_TENANT, token).status_code == 200

    assert _revoke(client, CA_TENANT).status_code == 200
    assert _read(app, CA_TENANT, token).status_code == 401


def test_disconnect_ends_a_token_minted_through_enrollment(client, app, env):
    """Not gated on the CareAgents prefix: a developer tenant's own token
    stops reading once that tenant is disconnected."""
    org_id = "tscope-oc-4"
    assert _register(client, DEV_TENANT, org_id).status_code == 201
    _verify(org_id)
    token = client.get(
        f"/fasten/connections/{org_id}/agent-access").get_json()["read_token"]
    assert _read(app, DEV_TENANT, token).status_code == 200

    assert _revoke(client, DEV_TENANT).status_code == 200
    assert _read(app, DEV_TENANT, token).status_code == 401
    # The bearer alias is the same credential and is refused the same way.
    bearer = app.test_client().get(
        "/r6/fhir/Observation",
        headers={"X-Tenant-Id": DEV_TENANT,
                 "Authorization": f"Bearer {token}"})
    assert bearer.status_code == 401


def test_no_new_read_token_after_disconnect(client, env):
    org_id = "tscope-oc-5"
    assert _register(client, DEV_TENANT, org_id).status_code == 201
    _verify(org_id)
    assert _revoke(client, DEV_TENANT).status_code == 200
    minted = client.get(f"/fasten/connections/{org_id}/agent-access")
    assert minted.status_code == 403
    assert "read_token" not in (minted.get_json() or {})


def test_careagents_own_token_still_reads_after_disconnect(client, app, env):
    """Disconnect keeps the records already collected (#847): CareAgents'
    server-minted token keeps reading them."""
    minted = client.post("/r6/fhir/internal/step-up-token",
                         json={"tenant_id": CA_TENANT},
                         headers={"X-Internal-Secret": SECRET})
    token = minted.get_json()["token"]
    assert _revoke(client, CA_TENANT).status_code == 200
    assert _read(app, CA_TENANT, token).status_code == 200


def test_open_tenant_patient_connect_token_still_reads(app, env):
    assert _read(app, DEV_TENANT,
                 _patient_connect_token(DEV_TENANT)).status_code == 200


# --- whitespace around the shared internal secret ----------------------------

def test_engine_trims_the_internal_secret_and_warns(monkeypatch, caplog):
    from r6.runtime_config import (internal_mint_secret,
                                   warn_on_padded_secrets)

    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f"  {SECRET}\n")
    assert internal_mint_secret() == SECRET
    with caplog.at_level(logging.WARNING, logger="r6.runtime_config"):
        warn_on_padded_secrets()
    assert "INTERNAL_TOKEN_MINT_SECRET" in caplog.text
    assert SECRET not in caplog.text

    caplog.clear()
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    with caplog.at_level(logging.WARNING, logger="r6.runtime_config"):
        warn_on_padded_secrets()
    assert caplog.text == ""


def test_padded_engine_secret_still_matches_the_trimmed_header(
        client, env, monkeypatch):
    """CareAgents sends the trimmed value; the engine gates compare against
    the trimmed value too."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f"{SECRET}\n")
    minted = client.post("/r6/fhir/internal/step-up-token",
                         json={"tenant_id": CA_TENANT},
                         headers={"X-Internal-Secret": SECRET})
    assert minted.status_code == 200


def test_careagents_trims_the_mint_secret_and_warns(caplog):
    from careagents.config import Config

    with caplog.at_level(logging.WARNING, logger="careagents.config"):
        cfg = Config({"HEALTHCLAW_MINT_SECRET": f"{SECRET} \n"})
    assert cfg.mint_secret == SECRET
    assert "HEALTHCLAW_MINT_SECRET" in caplog.text
    assert SECRET not in caplog.text


def test_both_sides_derive_the_same_consent_handoff_key(monkeypatch):
    from careagents import consent
    from careagents.config import Config
    from r6.oauth import handoff_tag

    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f"\t{SECRET}")
    cfg = Config({"HEALTHCLAW_MINT_SECRET": f"{SECRET}\n"})
    assert consent.tag(cfg.mint_secret, "req.123") == handoff_tag("req.123")


def test_worker_probe_sends_the_trimmed_secret(monkeypatch):
    from careagents import healthcheck

    sent = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, timeout):
        sent["secret"] = request.get_header("X-internal-secret")
        return _Response()

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", urlopen)
    assert healthcheck.healthy({
        "CARE_ROLE": "worker", "HEALTHCLAW_BASE": "http://healthclaw",
        "HEALTHCLAW_MINT_SECRET": f" {SECRET}\n"}) is True
    assert sent["secret"] == SECRET


# --- SEC941-F2: a whitespace-only secret is never "unset" ---------------------

def test_engine_refuses_to_start_on_a_whitespace_only_secret():
    from r6.runtime_config import validate_runtime_environment

    with pytest.raises(RuntimeError, match="INTERNAL_TOKEN_MINT_SECRET"):
        validate_runtime_environment(
            {"APP_ENV": "development", "INTERNAL_TOKEN_MINT_SECRET": " \n"})


def test_whitespace_only_secret_keeps_every_internal_gate_closed(
        client, monkeypatch):
    """The internal secret gate refuses too, not only the mint."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "\t ")
    assert client.post("/r6/fhir/internal/fasten-revoke",
                       json={"tenant_id": CA_TENANT}).status_code == 403
    assert client.post("/r6/fhir/internal/fasten-revoke",
                       json={"tenant_id": CA_TENANT},
                       headers={"X-Internal-Secret": ""}).status_code == 403


def test_careagents_refuses_a_whitespace_only_mint_secret():
    from careagents.config import Config, ConfigError

    with pytest.raises(ConfigError, match="HEALTHCLAW_MINT_SECRET"):
        Config({"HEALTHCLAW_MINT_SECRET": "  \n"})


# --- other senders of the shared secret ---------------------------------------

def test_relay_trims_the_careagents_secret(monkeypatch):
    import importlib.util
    from pathlib import Path

    monkeypatch.setenv("CAREAGENTS_MINT_SECRET", f"{SECRET}\n")
    path = (Path(__file__).resolve().parents[1]
            / "deploy" / "careagents" / "imessage_relay.py")
    spec = importlib.util.spec_from_file_location("imessage_relay_scope", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.SECRET == SECRET


def test_bot_sends_the_trimmed_secret(monkeypatch):
    import sys
    from pathlib import Path
    from unittest.mock import MagicMock

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-for-token-scope")
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "openclaw"))
    bot = sys.modules.get("bot")
    if bot is None:
        import bot
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", f" {SECRET}\n")
    resp = MagicMock()
    resp.json.return_value = {"token": "t"}
    post = MagicMock(return_value=resp)
    monkeypatch.setattr(bot.requests, "post", post)
    assert bot._get_step_up_token() == "t"
    assert post.call_args.kwargs["headers"]["X-Internal-Secret"] == SECRET
