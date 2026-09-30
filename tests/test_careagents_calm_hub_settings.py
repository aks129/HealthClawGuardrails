"""The settings page (calm hub spec section 6)."""

from __future__ import annotations

from careagents.models import Passkey
from tests.test_careagents import _login
from tests.test_careagents import app as _app_fixture
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

#: The CareAgents fixtures, re-exported under their own names.
cfg = _cfg_fixture
svc = _svc_fixture
app = _app_fixture


def _signed_in(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    with c.session_transaction() as s:
        return c, s["account_id"]


def test_settings_lists_passkeys_and_offers_to_add_one(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    with svc.session() as s:
        s.add(Passkey(account_id=aid, credential_id=b"cred-1",
                      public_key=b"pk", name="Kitchen iPad"))
    page = c.get("/settings").get_data(as_text=True)
    assert "Kitchen iPad" in page
    assert 'href="/auth?enroll=1"' in page


def test_settings_holds_surfaces_grants_sign_out_and_delete(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    tenant = svc.get_connection(aid, conn["id"])["tenant_id"]
    gid = svc.add_grant(aid, conn["id"], tenant, "cid-claude", "Claude",
                        "fhir.read", "consent_set")
    page = c.get("/settings").get_data(as_text=True)
    assert "Where you can reach your assistant" in page
    assert f'id="im-surface" data-agent="{conn["agent_id"]}"' in page
    assert "Apps you have shared records with" in page
    assert f'data-grant="{gid}"' in page
    assert 'action="/logout"' in page
    assert 'id="account-delete"' in page
    assert 'id="delete-modal"' in page
    assert "home.js" in page


def test_the_hub_no_longer_carries_what_moved(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    tenant = svc.get_connection(aid, conn["id"])["tenant_id"]
    svc.add_grant(aid, conn["id"], tenant, "cid-claude", "Claude",
                  "fhir.read", "consent_hub")
    hub = c.get("/home").get_data(as_text=True)
    for gone in ('id="account-delete"', 'id="im-surface"',
                 'class="hub-card grant-card"', 'action="/logout"',
                 'id="code-card"'):
        assert gone not in hub, gone
    assert 'href="/settings"' in hub
    assert 'href="/settings#grants-section"' in hub
    assert 'id="delete-modal"' in hub


def test_the_grants_link_is_absent_without_grants(app, svc, monkeypatch):
    c, _ = _signed_in(app, svc, monkeypatch)
    assert "#grants-section" not in c.get("/home").get_data(as_text=True)


def test_settings_needs_a_session(app):
    r = app.test_client().get("/settings")
    assert r.status_code == 302 and "/auth" in r.headers["Location"]
