"""Disconnect holds, and consent names the version the person saw.

Three findings from the security review of #904 (tester terms):

F2  refresh on a disconnected connection handed back the Fasten connect
    URL for the same tenant, whose webhook ingests what arrives.
F3  a poll un-disconnected: activate_connection rewrote every row on the
    tenant to "active", a revoked one included.
F4  consent was stamped with the server's version at the instant of the
    request, not the version on the card the person read. A card rendered
    before a terms change and submitted after it recorded acceptance of
    wording never shown.

Synthetic accounts only (example.com addresses).
"""

from __future__ import annotations

import re

import pytest

from careagents import connectors, tester_terms
from careagents.models import Connection
from tests.careagents_consent_helpers import consented
from tests.careagents_stage1_helpers import approve_terms
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)


def _row(svc, conn_id):  # noqa: F811
    with svc.session() as s:
        c = s.get(Connection, conn_id)
        return {"status": c.status, "last_count": c.last_count,
                "consent_version": c.consent_version}


def _disconnected_fasten(c, fake):
    conn_id = c.post("/api/connections/fasten",
                     json=consented()).get_json()["id"]
    tenant = fake.tenants[-1]
    assert c.post(f"/api/connections/{conn_id}/disconnect").status_code == 200
    return conn_id, tenant


def _real_rows(svc):  # noqa: F811
    with svc.session() as s:
        return s.query(Connection).filter(Connection.kind != "sample").count()


# --- F2: refresh refuses a disconnected connection --------------------------

def test_refresh_refuses_a_disconnected_connection(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id, _tenant = _disconnected_fasten(c, fake)
    r = c.post(f"/api/connections/{conn_id}/refresh", json=consented())
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "connection_not_active"
    assert "reauth_url" not in body
    # Refused before anything was touched: no new sync baseline either.
    row = _row(svc, conn_id)
    assert row["status"] == "revoked"
    assert row["last_count"] is None


# --- F3: nothing returns a revoked row to active ----------------------------

def test_poll_does_not_reactivate_a_disconnected_connection(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id, tenant = _disconnected_fasten(c, fake)
    assert fake.tenant_has_records(tenant)
    c.get(f"/api/connections/{tenant}/poll")
    assert _row(svc, conn_id)["status"] == "revoked"


def test_status_writes_skip_revoked_rows(cfg, svc, monkeypatch):  # noqa: F811
    """The guard sits in set_connection_status, so the chat page's settle
    (app.py, pending -> active) and activate_connection both inherit it."""
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id, tenant = _disconnected_fasten(c, fake)
    svc.set_connection_status(tenant, "active")
    assert _row(svc, conn_id)["status"] == "revoked"
    svc.set_connection_status(tenant, "pending")
    assert _row(svc, conn_id)["status"] == "revoked"
    assert svc.activate_connection(tenant) == []
    assert _row(svc, conn_id)["status"] == "revoked"


def test_status_writes_still_move_live_rows(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id = c.post("/api/connections/fasten",
                     json=consented()).get_json()["id"]
    svc.set_connection_status(fake.tenants[-1], "active")
    assert _row(svc, conn_id)["status"] == "active"


# --- F4: consent names the version the person saw ---------------------------

@pytest.mark.parametrize("kind", ["fasten", "direct"])
def test_connect_without_a_version_is_refused(cfg, svc, monkeypatch, kind):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    r = c.post(f"/api/connections/{kind}", json={"consent": True})
    assert r.status_code == 428
    assert r.get_json()["consent_version"] == tester_terms.CONSENT_VERSION
    assert _real_rows(svc) == 0


@pytest.mark.parametrize("kind", ["fasten", "direct"])
def test_connect_with_an_out_of_date_card_is_refused(
        cfg, svc, monkeypatch, kind):  # noqa: F811
    """A card rendered before a terms change, submitted after it."""
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    seen = tester_terms.CONSENT_VERSION
    approve_terms(monkeypatch, "2026-10-01")
    r = c.post(f"/api/connections/{kind}",
               json={"consent": True, "consent_version": seen})
    assert r.status_code == 428, "an out-of-date acceptance was recorded"
    assert r.get_json()["consent_version"] == "2026-10-01"
    assert _real_rows(svc) == 0


@pytest.mark.parametrize("kind", ["fasten", "direct"])
def test_connect_records_the_version_echoed(cfg, svc, monkeypatch, kind):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    r = c.post(f"/api/connections/{kind}", json=consented())
    assert r.status_code == 200, r.get_json()
    assert _row(svc, r.get_json()["id"])["consent_version"] == "2026-10-01"


@pytest.mark.parametrize("body", [
    {"consent": True},
    {"consent": True, "consent_version": None},
    {"consent": True, "consent_version": 20261001},
])
def test_reconsent_needs_the_current_version(cfg, svc, monkeypatch, body):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id = c.post("/api/connections/direct",
                     json=consented()).get_json()["id"]
    before = _row(svc, conn_id)["consent_version"]
    approve_terms(monkeypatch, "2026-10-01")
    r = c.post(f"/api/connections/{conn_id}/consent", json=body)
    assert r.status_code == 428
    assert _row(svc, conn_id)["consent_version"] == before
    stale = {"consent": True, "consent_version": before}
    assert c.post(f"/api/connections/{conn_id}/consent",
                  json=stale).status_code == 428
    ok = c.post(f"/api/connections/{conn_id}/consent", json=consented())
    assert ok.status_code == 200
    assert _row(svc, conn_id)["consent_version"] == "2026-10-01"


def test_refresh_consent_needs_the_current_version(cfg, svc, monkeypatch):  # noqa: F811
    """No connector asks consent on refresh today; the gate must still
    hold the day one does."""
    app, c, fake, *_ = _chat_app(cfg, svc, monkeypatch)
    conn_id = c.post("/api/connections/fasten",
                     json=consented()).get_json()["id"]
    monkeypatch.setattr(connectors, "refresh", lambda *a, **k: {
        "reauth_url": "https://example.com/again", "requires_consent": True})
    seen = tester_terms.CONSENT_VERSION
    approve_terms(monkeypatch, "2026-10-01")
    for body in ({"consent": True},
                 {"consent": True, "consent_version": seen}):
        r = c.post(f"/api/connections/{conn_id}/refresh", json=body)
        assert r.status_code == 428, body
        assert "reauth_url" not in r.get_json()
    r = c.post(f"/api/connections/{conn_id}/refresh", json=consented())
    assert r.status_code == 200
    assert r.get_json()["reauth_url"] == "https://example.com/again"


def test_the_hub_renders_the_version_its_cards_show(cfg, svc, monkeypatch):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    html = c.get("/home").get_data(as_text=True)
    # Both cards: the first-connect card and the "terms changed" card.
    assert html.count('data-consent-version="2026-10-01"') == 2


def test_home_js_echoes_the_rendered_version_on_every_consent_post():
    """Each of the three consent posts (connect tile, terms card, refresh)
    sends the version the page was rendered with, and none copies the
    server's version out of a 428 into a retry."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent
          / "careagents" / "static" / "home.js").read_text()
    sends = re.findall(r"consent_version\s*[:=]\s*shownConsentVersion", js)
    assert len(sends) == 3
    assert "res.d.consent_version" not in js
