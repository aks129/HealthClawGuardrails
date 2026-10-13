"""QA on #941: the cross-layer claims, on the real engine WSGI.

The CareAgents unit tests fake the HealthClaw client, so they prove a call
is MADE, not that the engine ACCEPTS it and holds. These drive the real
`HealthClawClient` onto the engine's Flask app with read auth on:

1. CareAgents' own Disconnect route ends a patient connect token that was
   issued earlier, on both token headers, while CareAgents' own
   server-minted token keeps reading the records Disconnect keeps (#847).
2. The mint secret is trimmed on BOTH sides, so a copy padded one way at
   CareAgents and another way at the engine still matches.

Synthetic accounts and tenants only.
"""

from __future__ import annotations

import pytest

from tests.careagents_consent_helpers import consented
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, cfg, svc)


def _engine(monkeypatch, secret_env):
    from main import create_app as engine_create_app
    from models import db

    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", secret_env)
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public-qa941-key")
    monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    app = engine_create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "LEGACY_BOOT_ON_CREATE": False,
    })
    with app.app_context():
        db.create_all()
    return app, app.test_client()


def _relay_client(engine, mint_secret):
    import requests as _requests

    from careagents.healthclaw import HealthClawClient

    def _to_requests(resp):
        r = _requests.Response()
        r.status_code = resp.status_code
        r._content = resp.get_data() or b""
        r.headers.update(resp.headers.to_wsgi_list())
        return r

    class _RelaySession:
        def post(self, url, json=None, headers=None, timeout=None, data=None):
            return _to_requests(engine.post(url.replace("http://engine", ""),
                                            json=json, data=data,
                                            headers=headers or {}))

        def get(self, url, params=None, headers=None, timeout=None):
            return _to_requests(engine.get(url.replace("http://engine", ""),
                                           query_string=params or {},
                                           headers=headers or {}))

    real = HealthClawClient(base="http://engine", mint_secret=mint_secret)
    real.http = _RelaySession()
    return real


def _read(engine, tenant, token, how):
    headers = {"X-Tenant-Id": tenant}
    if how == "stepup":
        headers["X-Step-Up-Token"] = token
    else:
        headers["Authorization"] = "Bearer " + token
    return engine.get("/r6/fhir/Observation", headers=headers).status_code


def test_careagents_disconnect_ends_an_earlier_patient_connect_token(
        cfg, svc, monkeypatch):  # noqa: F811
    """MUTATION: drop `and not disconnected_patient_connect_token(...)` in
    r6/read_auth.py authorize_tenant_read -> the patient token still reads
    (200) after CareAgents' Disconnect."""
    from careagents.app import create_app
    from r6.stepup import (PATIENT_CONNECT_SUBJECT, READ_TOKEN_TTL_SECONDS,
                           generate_step_up_token)

    secret = "qa941-cross-layer-internal-secret"
    tenant = "ca-qa941xl01"
    engine_app, engine = _engine(monkeypatch, secret)
    real = _relay_client(engine, secret)
    real.new_tenant_id = lambda: tenant

    app = create_app(config=cfg, client=real, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = c.post("/api/connections/fasten",
                     json=consented()).get_json()["id"]

    # Issued earlier: the token a browser could hold from before #941.
    patient = generate_step_up_token(
        tenant, agent_id=PATIENT_CONNECT_SUBJECT,
        ttl_seconds=READ_TOKEN_TTL_SECONDS, scope="read")
    for how in ("stepup", "bearer"):
        assert _read(engine, tenant, patient, how) == 200, how

    r = c.post(f"/api/connections/{conn_id}/disconnect")
    assert r.status_code == 200, r.get_data(as_text=True)

    for how in ("stepup", "bearer"):
        assert _read(engine, tenant, patient, how) == 401, how
    # CareAgents' own server-minted token is not a patient connect token.
    ours = real.mint_token(tenant)
    for how in ("stepup", "bearer"):
        assert _read(engine, tenant, ours, how) == 200, how
    # The control: with no credential this tenant is closed to reads anyway,
    # so the 401s above are the dead token refused, not a missing one.
    assert engine.get("/r6/fhir/Observation",
                      headers={"X-Tenant-Id": tenant}).status_code == 401


@pytest.mark.parametrize("engine_side, careagents_side", [
    (" qa941-shared-secret\n", "qa941-shared-secret"),
    ("qa941-shared-secret", "qa941-shared-secret \r\n"),
    ("\tqa941-shared-secret ", "\nqa941-shared-secret\t"),
])
def test_padded_secrets_match_across_the_boundary(
        monkeypatch, engine_side, careagents_side):
    """Before #941 the engine compared its raw value and CareAgents sent its
    raw value, so differently padded copies never matched and the mint
    answered 403. MUTATION: drop `.strip()` from either side -> 403."""
    from careagents.config import Config

    base = {"CARE_ENV": "development", "CARE_SESSION_SECRET": "q" * 40,
            "CARE_DATABASE_URL": "sqlite:///:memory:"}
    sent = Config({**base, "HEALTHCLAW_MINT_SECRET": careagents_side}).mint_secret
    _engine_app, engine = _engine(monkeypatch, engine_side)
    real = _relay_client(engine, sent)
    token = real.mint_token("ca-qa941pad01")
    assert token
    assert _read(engine, "ca-qa941pad01", token, "stepup") == 200


def test_a_wrong_secret_still_refuses_the_mint(monkeypatch):
    """The control for the test above: trimming must not loosen matching."""
    from careagents.healthclaw import HealthClawError

    _engine_app, engine = _engine(monkeypatch, " qa941-shared-secret\n")
    real = _relay_client(engine, "qa941-shared-secreT")
    with pytest.raises(HealthClawError):
        real.mint_token("ca-qa941pad02")
