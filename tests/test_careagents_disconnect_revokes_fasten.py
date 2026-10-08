"""Disconnect tells HealthClaw first, and only then says "disconnected".

Disconnect used to flip only the CareAgents row, so the engine kept
accepting Fasten records into the tenant. Now `disconnect_connection` asks
HealthClaw to revoke the tenant first; if that cannot be confirmed, the row
is left as it was and the person is told it could not be confirmed. Not
that it is "still on": a lost answer can follow a revoke that ran.

The unit tests fake the HealthClaw client, which proves a call is MADE.
The last test drives the real client onto the real engine WSGI, which
proves the call is ACCEPTED and holds.

Synthetic accounts only (example.com addresses).
"""

from __future__ import annotations

from careagents.healthclaw import HealthClawError
from careagents.models import Connection
from tests.careagents_consent_helpers import consented
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _login, cfg, svc)

FAILED_MESSAGE = ("We couldn't confirm the disconnect. "
                  "Please try again in a minute.")


def _status(svc, conn_id):  # noqa: F811
    with svc.session() as s:
        return s.get(Connection, conn_id).status


def _fasten(c, fake):
    conn_id = c.post("/api/connections/fasten",
                     json=consented()).get_json()["id"]
    return conn_id, fake.tenants[-1]


def test_disconnect_revokes_the_tenant_at_healthclaw(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id, tenant = _fasten(c, fake)
    r = c.post(f"/api/connections/{conn_id}/disconnect")
    assert r.status_code == 200
    assert fake.fasten_revoked == [tenant]
    assert _status(svc, conn_id) == "revoked"


def test_engine_is_told_before_the_row_flips(cfg, svc, monkeypatch):  # noqa: F811
    """MUTATION: call svc.revoke_connection before hc.revoke_fasten -> the
    order recorded here is reversed."""
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id, tenant = _fasten(c, fake)
    order = []
    real_revoke = fake.revoke_fasten
    real_flip = svc.revoke_connection
    monkeypatch.setattr(fake, "revoke_fasten",
                        lambda t: (order.append("engine"), real_revoke(t))[1])
    monkeypatch.setattr(svc, "revoke_connection",
                        lambda a, cid: (order.append("row"),
                                        real_flip(a, cid))[1])
    assert c.post(f"/api/connections/{conn_id}/disconnect").status_code == 200
    assert order == ["engine", "row"]


def test_an_unconfirmed_revoke_leaves_the_connection_on(cfg, svc, monkeypatch):  # noqa: F811
    """Fail closed: never tell the person "disconnected" while the engine
    may still be importing.

    MUTATION: catch the HealthClawError and fall through to
    svc.revoke_connection -> 200 and a revoked row here."""
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id, _tenant = _fasten(c, fake)
    before = _status(svc, conn_id)
    fake.revoke_fasten_fails = True
    r = c.post(f"/api/connections/{conn_id}/disconnect")
    assert r.status_code == 503
    body = r.get_json()
    assert body["message"] == FAILED_MESSAGE
    assert body.get("status") != "revoked"
    assert _status(svc, conn_id) == before


def test_a_sample_disconnect_also_revokes(cfg, svc, monkeypatch):  # noqa: F811
    """Every connection kind: the engine call is harmless on a sample
    tenant, and one rule is easier to hold than a list of kinds."""
    app, c, fake, _agent, tenant, conn_id = _chat_app(cfg, svc, monkeypatch)
    assert c.post(f"/api/connections/{conn_id}/disconnect").status_code == 200
    assert fake.fasten_revoked == [tenant]
    assert _status(svc, conn_id) == "revoked"


def test_unknown_connection_is_404_and_revokes_nothing(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    r = c.post("/api/connections/no-such-connection/disconnect")
    assert r.status_code == 404
    assert fake.fasten_revoked == []


def test_another_accounts_connection_is_404_and_revokes_nothing(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id, _tenant = _fasten(c, fake)
    other = app.test_client()
    _login(other, svc, monkeypatch, email="someone-else@example.com")
    r = other.post(f"/api/connections/{conn_id}/disconnect")
    assert r.status_code == 404
    assert fake.fasten_revoked == []
    assert _status(svc, conn_id) != "revoked"


# --- the client method -------------------------------------------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": "application/json"}
        self.text = ""

    def json(self):
        return self._body


def test_revoke_fasten_posts_the_tenant_with_the_internal_secret():
    from careagents.healthclaw import HealthClawClient
    sent = {}

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None, **_):
            sent.update(url=url, json=json, headers=headers)
            return _Resp(200, {"tenant_id": "t-1", "revoked": True,
                               "already_revoked": False})

    client = HealthClawClient(base="http://engine", mint_secret="s3cret")
    client.http = _Session()
    out = client.revoke_fasten("t-1")
    assert out["revoked"] is True
    assert sent["url"].endswith("/internal/fasten-revoke")
    assert sent["json"] == {"tenant_id": "t-1"}
    assert sent["headers"]["X-Internal-Secret"] == "s3cret"


def test_revoke_fasten_raises_on_any_non_200():
    """MUTATION: drop the status check in revoke_fasten -> a 403 reads as a
    confirmed disconnect."""
    import pytest

    from careagents.healthclaw import HealthClawClient

    class _Session:
        def post(self, url, **_):
            return _Resp(403, {"error": "forbidden"})

    client = HealthClawClient(base="http://engine", mint_secret="wrong")
    client.http = _Session()
    with pytest.raises(HealthClawError):
        client.revoke_fasten("t-1")


# --- cross-layer: the real client on the real engine ------------------------

def test_cross_layer_disconnect_holds_at_the_engine(cfg, svc, monkeypatch):  # noqa: F811
    """A real HealthClawClient dispatching through the engine's Flask WSGI.
    After Disconnect the engine holds the tombstone, the old connect link
    shows no widget, and a late connection_success creates nothing."""
    import json
    from unittest.mock import patch

    import requests as _requests

    from careagents.app import create_app
    from careagents.healthclaw import HealthClawClient
    from main import create_app as engine_create_app
    from models import db

    secret = "cross-layer-internal-secret"
    tenant = "ca-xl-revoke"                    # not public
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", secret)
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public-xl-key")
    monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    engine_app = engine_create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "LEGACY_BOOT_ON_CREATE": False,
    })
    with engine_app.app_context():
        db.create_all()
    engine = engine_app.test_client()

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

    real = HealthClawClient(base="http://engine", mint_secret=secret)
    real.http = _RelaySession()
    real.new_tenant_id = lambda: tenant

    app = create_app(config=cfg, client=real, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = c.post("/api/connections/fasten",
                     json=consented()).get_json()["id"]
    assert "public-xl-key" in engine.get(f"/connect/{tenant}").get_data(
        as_text=True)

    r = c.post(f"/api/connections/{conn_id}/disconnect")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _status(svc, conn_id) == "revoked"

    with engine_app.app_context():
        from r6.fasten.models import FastenConnection, tenant_revoked
        assert tenant_revoked(tenant)
    page = engine.get(f"/connect/{tenant}").get_data(as_text=True)
    assert "public-xl-key" not in page

    payload = {"type": "patient.connection_success",
               "data": {"org_connection_id": "oc-xl-late",
                        "external_id": tenant}}
    with patch("r6.fasten.routes.verify_webhook", return_value=True), \
         patch("r6.fasten.routes.trigger_ehi_export") as trigger:
        engine.post("/fasten/webhook", data=json.dumps(payload),
                    content_type="application/json")
    assert not trigger.called
    with engine_app.app_context():
        assert db.session.get(FastenConnection, "oc-xl-late") is None


# --- the poll tells the truth about a disconnected connection ---------------

def test_poll_reports_a_disconnected_connection_as_revoked(
        cfg, svc, monkeypatch):  # noqa: F811
    """QA on #909: records had landed, so the poll answered "active" for a
    row that stayed revoked, and the hub read that as live.

    MUTATION: delete the revoked branch in poll_connection -> "active"."""
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id, tenant = _fasten(c, fake)
    assert c.post(f"/api/connections/{conn_id}/disconnect").status_code == 200
    assert fake.tenant_has_records(tenant)
    r = c.get(f"/api/connections/{tenant}/poll")
    assert r.status_code == 200
    assert r.get_json()["status"] == "revoked"
    assert _status(svc, conn_id) == "revoked"


def test_poll_on_a_live_connection_still_reports_active(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    _conn_id, tenant = _fasten(c, fake)
    assert c.get(f"/api/connections/{tenant}/poll").get_json()["status"] \
        == "active"


def test_the_pending_poller_stops_on_revoked():
    """A card left polling in another tab stops and reloads instead of
    spinning forever on a status it does not know."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent
          / "careagents" / "static" / "home.js").read_text()
    assert 'd.status === "revoked"' in js
