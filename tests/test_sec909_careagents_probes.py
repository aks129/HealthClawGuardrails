"""Security review of #909 on the CareAgents side: consent-version shapes,
cross-account ids on the disconnect routes, and the engine never hearing
about a disconnect. Synthetic example.com accounts only."""

from __future__ import annotations

import pytest

from careagents import tester_terms
from careagents.models import Connection
from tests.careagents_consent_helpers import consented
from tests.careagents_stage1_helpers import approve_terms
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _login, cfg, svc)


def _status(svc, conn_id):  # noqa: F811
    with svc.session() as s:
        return s.get(Connection, conn_id).status


def _real_rows(svc):  # noqa: F811
    with svc.session() as s:
        return s.query(Connection).filter(Connection.kind != "sample").count()


# --- consent version: shapes that must never pass ---------------------------

def _bad_bodies():
    v = tester_terms.CONSENT_VERSION
    return [
        {"consent": True, "consent_version": [v]},
        {"consent": True, "consent_version": {"v": v}},
        {"consent": True, "consent_version": v.upper() + "X"},
        {"consent": True, "consent_version": " " + v},
        {"consent": True, "consent_version": v + " "},
        {"consent": True, "consent_version": v + "​"},
        {"consent": True, "consent_version": True},
        {"consent": True, "consent_version": ""},
        {"consent": "true", "consent_version": v},
        {"consent": 1, "consent_version": v},
        {"consent": [True], "consent_version": v},
        {"consent_version": v},
    ]


def _non_object_bodies():
    v = tester_terms.CONSENT_VERSION
    return [[{"consent": True, "consent_version": v}], "consent=true", 1]


@pytest.mark.parametrize("kind", ["fasten", "direct"])
def test_consent_version_shapes_refused_on_connect(cfg, svc, monkeypatch, kind):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    for body in _bad_bodies():
        r = c.post(f"/api/connections/{kind}", json=body)
        assert r.status_code == 428, (body, r.status_code)
    assert _real_rows(svc) == 0


def test_consent_version_shapes_refused_on_reconsent(cfg, svc, monkeypatch):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id = c.post("/api/connections/direct",
                     json=consented()).get_json()["id"]
    approve_terms(monkeypatch, "2026-10-01")
    for body in _bad_bodies():
        r = c.post(f"/api/connections/{conn_id}/consent", json=body)
        assert r.status_code == 428, (body, r.status_code)


@pytest.mark.parametrize("path", ["/api/connections/direct",
                                  "/api/connections/fasten"])
def test_non_object_consent_body_creates_nothing(cfg, svc, monkeypatch, path):  # noqa: F811
    """A JSON array or string body reaches `body.get` and raises (a 500,
    pre-existing on main). It fails closed: nothing is created."""
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    approve_terms(monkeypatch, "2026-10-01")
    for body in _non_object_bodies():
        r = c.post(path, json=body)
        assert r.status_code >= 400, (body, r.status_code)
    assert _real_rows(svc) == 0


def test_form_encoded_consent_is_refused(cfg, svc, monkeypatch):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    r = c.post("/api/connections/direct",
               data={"consent": "true",
                     "consent_version": tester_terms.CONSENT_VERSION})
    assert r.status_code == 428
    assert _real_rows(svc) == 0


# --- V6: another account's connection id on every disconnect-path route ----

def test_other_accounts_connection_is_refused_everywhere(cfg, svc, monkeypatch):  # noqa: F811
    app, a, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    victim = a.post("/api/connections/direct",
                    json=consented()).get_json()["id"]
    victim_tenant = fake.tenants[-1]
    b = app.test_client()
    _login(b, svc, monkeypatch, email="mallory@example.com")
    assert b.post(f"/api/connections/{victim}/disconnect").status_code == 404
    assert b.post(f"/api/connections/{victim}/refresh",
                  json=consented()).status_code == 404
    assert b.post(f"/api/connections/{victim}/consent",
                  json=consented()).status_code == 404
    assert b.get(f"/api/connections/{victim_tenant}/poll").status_code == 404
    r = b.post(f"/api/connections/{victim}/upload",
               data=b'{"resourceType":"Bundle","type":"collection","entry":[]}',
               content_type="application/fhir+json")
    assert r.status_code == 404
    assert _status(svc, victim) != "revoked"


# --- disconnect holds on the CareAgents side --------------------------------

def test_upload_after_disconnect_is_refused(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id = c.post("/api/connections/direct",
                     json=consented()).get_json()["id"]
    assert c.post(f"/api/connections/{conn_id}/disconnect").status_code == 200
    calls = []
    monkeypatch.setattr(fake, "ingest_bundle",
                        lambda *a, **k: calls.append(a) or {"ingested": 1})
    r = c.post(f"/api/connections/{conn_id}/upload",
               data=b'{"resourceType":"Bundle","type":"collection","entry":[]}',
               content_type="application/fhir+json")
    assert r.status_code == 409 and calls == []
    assert _status(svc, conn_id) == "revoked"


def test_reconnect_after_disconnect_mints_a_new_tenant(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    first = c.post("/api/connections/fasten", json=consented()).get_json()
    old_tenant = fake.tenants[-1]
    c.post(f"/api/connections/{first['id']}/disconnect")
    again = c.post("/api/connections/fasten", json=consented()).get_json()
    assert again["id"] != first["id"]
    assert not again.get("existing")
    assert old_tenant not in again["connect_url"]
    assert _status(svc, first["id"]) == "revoked"


# --- the known gap: the engine is never told about a disconnect -------------

@pytest.mark.xfail(strict=True, reason="known gap (CTO design pending): "
                   "CareAgents disconnect makes no engine call, so the "
                   "engine's FastenConnection and /connect/<tenant> page "
                   "stay live for the tenant")
def test_disconnect_tells_the_engine(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id = c.post("/api/connections/fasten",
                     json=consented()).get_json()["id"]
    seen = []

    # Every public client method, counted for the length of the request.
    for name in [n for n in dir(fake) if not n.startswith("_")]:
        attr = getattr(fake, name)
        if callable(attr):
            monkeypatch.setattr(
                fake, name,
                (lambda n, f: lambda *a, **k: (seen.append(n), f(*a, **k))[1])(
                    name, attr))
    assert c.post(f"/api/connections/{conn_id}/disconnect").status_code == 200
    assert seen, "disconnect made no engine call at all"
