"""Security review of #913, CareAgents side: is Disconnect fail-closed when
the engine times out, drops the connection or answers 200 with something
that is not a confirmation?

The PR's own tests fake the HealthClaw client. Here the app's fake keeps
everything except `revoke_fasten`, which is the REAL client method over a
stub transport, so the transport-to-HealthClawError conversion is exercised.
Synthetic accounts only (example.com).
"""

from __future__ import annotations

import pytest
import requests

from careagents.healthclaw import HealthClawClient
from careagents.models import Connection
from tests.careagents_consent_helpers import consented
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _login, cfg, svc)


def _status(svc, conn_id):  # noqa: F811
    with svc.session() as s:
        return s.get(Connection, conn_id).status


class _Resp:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body, self._text = body, text

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._body


def _real_revoke(behaviour):
    class _Session:
        def post(self, url, **_):
            return behaviour()

    real = HealthClawClient(base="http://engine", mint_secret="s")
    real.http = _Session()
    return real.revoke_fasten


def _raise(exc):
    def go():
        raise exc
    return go


@pytest.mark.parametrize("behaviour", [
    _raise(requests.Timeout("read timed out")),
    _raise(requests.ConnectionError("refused")),
    lambda: _Resp(500, {"error": "revoke failed", "revoked": False}),
    lambda: _Resp(502, text="<html>bad gateway</html>"),
    lambda: _Resp(200, text="<html>proxy interstitial</html>"),
    lambda: _Resp(200, ["not", "an", "object"]),
], ids=["timeout", "conn-refused", "engine-500", "proxy-502", "200-html",
        "200-list"])
def test_disconnect_stays_on_when_the_engine_does_not_confirm(
        cfg, svc, monkeypatch, behaviour):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id = c.post("/api/connections/fasten",
                     json=consented()).get_json()["id"]
    before = _status(svc, conn_id)
    monkeypatch.setattr(fake, "revoke_fasten", _real_revoke(behaviour))
    r = c.post(f"/api/connections/{conn_id}/disconnect")
    assert r.status_code == 503
    assert r.get_json()["error"] == "disconnect_failed"
    assert "Traceback" not in r.get_data(as_text=True)
    assert _status(svc, conn_id) == before


@pytest.mark.parametrize("body", [{"revoked": False}, {}])
def test_a_200_that_does_not_say_revoked_is_not_a_disconnect(
        cfg, svc, monkeypatch, body):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id = c.post("/api/connections/fasten",
                     json=consented()).get_json()["id"]
    monkeypatch.setattr(fake, "revoke_fasten",
                        _real_revoke(lambda: _Resp(200, body)))
    r = c.post(f"/api/connections/{conn_id}/disconnect")
    assert r.status_code == 503
    assert _status(svc, conn_id) != "revoked"


def test_another_account_cannot_disconnect_mine(cfg, svc, monkeypatch):  # noqa: F811
    """Account B posts A's connection id: 404, no engine call, A unchanged.
    No session at all: refused, no engine call."""
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id = c.post("/api/connections/fasten",
                     json=consented()).get_json()["id"]
    calls = []
    monkeypatch.setattr(fake, "revoke_fasten", lambda t: calls.append(t))
    other = app.test_client()
    _login(other, svc, monkeypatch, email="mallory@example.com")
    r = other.post(f"/api/connections/{conn_id}/disconnect")
    assert r.status_code == 404
    assert calls == []
    assert _status(svc, conn_id) != "revoked"
    anon = app.test_client()
    assert anon.post(f"/api/connections/{conn_id}/disconnect"
                     ).status_code in (401, 403)
    assert calls == []
