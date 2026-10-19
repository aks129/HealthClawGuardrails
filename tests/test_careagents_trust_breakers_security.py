"""Security review of #998: the done-form card, the visit-brief link and the
code-first sign-in.

The PR's own tests fake the HealthClaw client. The isolation tests here run
the real client on the real engine's WSGI, two accounts on two tenants, and
always assert the positive control first so a refused read cannot pass the
negative one vacuously. Synthetic data only.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from tests.test_beta_acceptance_rows import _MINT, BASE, _Browser, _to_requests
from tests.test_careagents import FakeClient, _login
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture
from tests.test_careagents_what_happened import OutcomeClient, _app, _signed_in

cfg = _cfg_fixture
svc = _svc_fixture

TENANT_A = "ca-sec998-a"
TENANT_B = "ca-sec998-b"


class TwoWorlds:
    """One engine, one CareAgents app, two accounts on two tenants, each
    with a sample connection and an assistant."""

    def __init__(self, cfg, svc, monkeypatch):
        from careagents.app import create_app
        from careagents.healthclaw import HealthClawClient
        from main import create_app as engine_create_app
        from models import db

        monkeypatch.setenv("PUBLIC_TENANTS",
                           f"test-tenant,{TENANT_A},{TENANT_B}")
        monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
        monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", _MINT)
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://engine")
        self.engine_app = engine_create_app({
            "TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "LEGACY_BOOT_ON_CREATE": False})
        with self.engine_app.app_context():
            db.create_all()
            from r6.actions.rails import register_all
            from r6.actions.registry import _clear
            from r6.rate_limit import _rate_limits
            _clear()
            register_all()
            _rate_limits.clear()
        engine = self.engine_app.test_client()

        class _Relay:
            def post(self, url, json=None, headers=None, timeout=None, data=None):
                return _to_requests(engine.post(url.replace("http://engine", ""),
                                                json=json, data=data,
                                                headers=headers or {}))

            def get(self, url, params=None, headers=None, timeout=None):
                extra = {"query_string": params} if params else {}
                return _to_requests(engine.get(url.replace("http://engine", ""),
                                               headers=headers or {}, **extra))

        self.hc = HealthClawClient(base="http://engine", mint_secret=_MINT)
        self.hc.http = _Relay()
        self.app = create_app(config=cfg, client=self.hc, accounts=svc)
        self.app.config["TESTING"] = True
        self.svc, self.mp = svc, monkeypatch
        self.a, self.agent_a = self._world("sec998-a@example.com", TENANT_A)
        self.b, self.agent_b = self._world("sec998-b@example.com", TENANT_B)

    def _world(self, email, tenant):
        c = self.app.test_client()
        _login(c, self.svc, self.mp, email=email)
        s = _Browser(c, BASE)
        self.hc.new_tenant_id = lambda: tenant
        r = s.post(f"{BASE}/api/connections/sample")
        assert r.status_code == 200, r.text
        conn = r.json()["id"]
        r = s.post(f"{BASE}/api/agents", json={
            "name": "Sec", "persona": "calm", "connection_id": conn})
        assert r.status_code == 200, r.text
        return c, r.json()["id"]

    def completed_form(self, tenant):
        """A form proposed through the real client, then moved past the
        gate in the engine's own table: the filter is what is under test,
        not the approval."""
        from models import db
        from r6.actions.models import ProposedAction
        action_id = self.hc.start_form_action(tenant)
        with self.engine_app.app_context():
            row = db.session.get(ProposedAction, action_id)
            assert row is not None and row.tenant_id == tenant
            row.status = "completed"
            row.updated_at = datetime.now(timezone.utc)
            db.session.commit()
        return action_id


def _done(html):
    m = re.search(r'data-done-form="([^"]*)"', html)
    return m.group(1) if m else None


def test_a_done_form_never_crosses_accounts_on_the_real_engine(
        cfg, svc, monkeypatch):
    w = TwoWorlds(cfg, svc, monkeypatch)
    form_a = w.completed_form(TENANT_A)
    form_b = w.completed_form(TENANT_B)
    page_a = w.a.get(f"/chat?agent={w.agent_a}").get_data(as_text=True)
    page_b = w.b.get(f"/chat?agent={w.agent_b}").get_data(as_text=True)
    # Positive controls: each sees its own, so the reads were accepted.
    assert _done(page_a) == form_a
    assert _done(page_b) == form_b
    # V6: neither page carries the other's id anywhere.
    assert form_b not in page_a
    assert form_a not in page_b


def test_another_accounts_agent_id_gets_no_chat_review_or_brief(
        cfg, svc, monkeypatch):
    w = TwoWorlds(cfg, svc, monkeypatch)
    form_a = w.completed_form(TENANT_A)
    assert w.a.get(f"/review/{w.agent_a}/{form_a}").status_code == 200
    # B swaps in A's agent id on each surface the PR links.
    r = w.b.get(f"/chat?agent={w.agent_a}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/home")
    assert form_a not in r.get_data(as_text=True)
    r = w.b.get(f"/brief?agent={w.agent_a}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/home")
    # And A's action id, under B's own agent and under A's.
    assert w.b.get(f"/review/{w.agent_b}/{form_a}").status_code == 404
    assert w.b.get(f"/review/{w.agent_a}/{form_a}").status_code == 404


def test_a_revoked_connection_draws_no_done_form_on_the_real_engine(
        cfg, svc, monkeypatch):
    w = TwoWorlds(cfg, svc, monkeypatch)
    form_a = w.completed_form(TENANT_A)
    assert _done(w.a.get(f"/chat?agent={w.agent_a}").get_data(
        as_text=True)) == form_a
    svc.set_connection_status(TENANT_A, "revoked")
    ctx = svc.get_worker_agent_context(w.agent_a)
    assert ctx["connection"]["status"] == "revoked"
    page = w.a.get(f"/chat?agent={w.agent_a}").get_data(as_text=True)
    assert "data-done-form" not in page
    assert form_a not in page


# --- injection: the id is engine-supplied, the page must not trust it --------

@pytest.mark.parametrize("hostile", [
    '"><script>alert(1)</script>',
    "x' onmouseover='alert(1)",
    "javascript:alert(1)",
])
def test_a_hostile_action_id_is_escaped_in_the_attribute(
        cfg, svc, monkeypatch, hostile):
    fake = OutcomeClient(pending=[], recent=[
        {"id": hostile, "kind": "form-fill", "status": "completed",
         "updated_at": "2026-10-18T00:00:00Z"}])
    c, agent, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    html = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    tag = re.search(r'<div class="chat-log" id="log"[^>]*>', html).group(0)
    assert "<script>alert" not in html
    assert "onmouseover=" not in tag.replace("&#39; onmouseover=&#39;", "")
    # The raw value round-trips only as escaped text inside the one attribute.
    assert re.search(r'data-done-form="[^"<>]*"', tag)


def test_the_done_card_href_is_rooted_on_review():
    """chat.js builds the link by concatenation, so the scheme is always
    same-origin /review/. (A '../' in an engine id would still move within
    the origin: recorded as low in the review, ids are server uuid4.)"""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "careagents" / "static"
          / "chat.js").read_text()
    card = js[js.index("function addFormDoneCard"):]
    card = card[:card.index("\n  }\n")]
    assert re.search(r'a\.href = "/review/" \+ AGENT \+ "/" \+ '
                     r'encodeURIComponent\(actionId\)', card)
    assert "innerHTML" not in card


def test_updated_at_of_mixed_types_does_not_500_the_chat(cfg, svc, monkeypatch):
    """The sort key is `updated_at or ""`. An engine that sent a number for
    one row must not take the chat page down (only HealthClawError is
    caught)."""
    fake = OutcomeClient(pending=[], recent=[
        {"id": "a1", "kind": "form-fill", "status": "completed",
         "updated_at": "2026-10-18T00:00:00Z"},
        {"id": "a2", "kind": "form-fill", "status": "completed",
         "updated_at": 5}])
    c, agent, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    assert c.get(f"/chat?agent={agent}").status_code == 200


# --- auth page: presentation only ------------------------------------------

def test_auth_page_variants_keep_one_email_button_and_no_hidden_passkey_path(
        cfg, svc):
    c = _app(cfg, svc, FakeClient()).test_client()
    page = c.get("/auth").get_data(as_text=True)
    assert page.count('id="email-btn"') == 1
    assert page.count('id="passkey-btn"') == 1
    # The enroll step still exists for an account adding one.
    assert 'id="add-passkey-btn"' in page


def test_admin_funnel_stays_owner_only(cfg, svc, monkeypatch):
    c, _agent, _ = _signed_in(_app(cfg, svc, FakeClient()), svc, monkeypatch)
    assert c.get("/admin/funnel").status_code == 404


def test_a_failed_pending_lookup_draws_no_done_form(cfg, svc, monkeypatch):
    from careagents.healthclaw import HealthClawError

    class PendingDown(OutcomeClient):
        def pending_actions(self, tenant):
            raise HealthClawError("pending unavailable", 503)

    fake = PendingDown(recent=[
        {"id": "act-old", "kind": "form-fill", "status": "completed",
         "updated_at": "2026-10-18T00:00:00Z"}])
    c, agent, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    html = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert "data-done-form" not in html
