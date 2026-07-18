"""CareAgents account-layer unit tests — no network; HealthClaw + LLM + email
+ WebAuthn verification are faked. Live paths are covered by
scripts/careagents_smoke.py against the deployed site.

Pins: fail-closed config, one safety core per persona, email-code auth,
WebAuthn option issuance, account-scoped connections/agents/surfaces (foreign
ids 404), the chat gate, the review relay, and the Telegram bind handshake.
"""

from __future__ import annotations

import pytest

from careagents.config import Config, ConfigError
from careagents.healthclaw import HealthClawError
from careagents.personas import PERSONAS, SAFETY_CORE, system_prompt


# --- config: fail-closed ------------------------------------------------------

def test_production_config_requires_every_secret():
    base = {"CARE_ENV": "production"}
    for missing in ({}, {"CARE_SESSION_SECRET": "x" * 32},
                    {"CARE_SESSION_SECRET": "x" * 32, "HEALTHCLAW_MINT_SECRET": "m"},
                    {"CARE_SESSION_SECRET": "x" * 32, "HEALTHCLAW_MINT_SECRET": "m",
                     "OPENAI_API_KEY": "k"}):  # still missing RESEND
        with pytest.raises(ConfigError):
            Config(env={**base, **missing})
    ok = Config(env={**base, "CARE_SESSION_SECRET": "x" * 32,
                     "HEALTHCLAW_MINT_SECRET": "m", "OPENAI_API_KEY": "k",
                     "RESEND_API_KEY": "r"})
    assert ok.provider == "openai" and ok.rp_id == "careagents.cloud"


def test_every_persona_shares_the_safety_core():
    for key in PERSONAS:
        p = system_prompt("Juniper", key)
        assert SAFETY_CORE in p and "911" in p and "no known allergies" in p.lower()


# --- fakes -------------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.bound = []
        self.seeded = []

    def new_tenant_id(self):
        self.seeded.append(1)
        return f"ca-{len(self.seeded):010d}"

    def seed(self, tenant):
        return 7

    def search(self, tenant, rtype, params=None):
        return {"total": 1, "entry": [{"resource": {
            "resourceType": rtype, "status": "active",
            "code": {"text": f"sample {rtype}"}}}]}

    def interpret_labs(self, tenant):
        return {"summary": {}, "consumer": {"headline": "ok"}, "disclaimer": "d"}

    def care_gaps(self, tenant):
        return {"summary": {}, "consumer": {"due": []}}

    def start_form_action(self, tenant):
        return "act-1"

    def action_status(self, tenant, action_id):
        if action_id != "act-1":
            raise HealthClawError("not found", 404)
        return {"id": "act-1", "status": "completed",
                "outcome_summary": '{"delivery_link": "https://x/pdf"}'}

    def confirm_action(self, tenant, action_id):
        return {"status": "completed"}

    def fetch_review_page(self, tenant, action_id):
        return 200, f"<html>/r6/actions/{action_id}/review</html>"

    def submit_review(self, tenant, action_id, decisions):
        if "nka" not in decisions and "allergy-0" not in decisions:
            return 422, {"error": "attestation required"}
        return 200, {"status": "awaiting_confirmation"}

    def tenant_has_records(self, tenant):
        return True

    def bind_telegram(self, tenant, chat_id):
        self.bound.append((tenant, chat_id))
        return True

    base = "https://app.healthclaw.io"

    def fasten_connect_url(self, tenant):
        return f"{self.base}/connect/{tenant}"

    def wearables_connect_url(self, tenant, provider):
        return f"{self.base}/wearables/oauth/start?provider={provider}&tenant_id={tenant}"

    def conformance_badge(self):
        return {"message": "A (7/7)"}


@pytest.fixture
def cfg():
    return Config(env={"CARE_DATABASE_URL": "sqlite:///:memory:",
                       "CARE_RP_ID": "localhost",
                       "CARE_ORIGIN": "http://localhost",
                       "OPENAI_API_KEY": "k",
                       "HEALTHCLAW_MINT_SECRET": "mint-secret",
                       "FASTEN_PUBLIC_KEY": "pub123",
                       "CARE_TELEGRAM_BOT": "carebot",
                       "CARE_IMESSAGE_HANDLE": "+15550001111"})


@pytest.fixture
def svc(cfg):
    from careagents.accounts import AccountService
    return AccountService(cfg)


@pytest.fixture
def app(cfg, svc):
    from careagents.app import create_app
    a = create_app(config=cfg, client=FakeClient(), accounts=svc)
    a.config["TESTING"] = True
    return a


def _login(client, svc, monkeypatch, email="gene@example.com"):
    """Log a client in via the real email-code path (code captured from mail)."""
    captured = {}
    import careagents.mail as mailmod
    monkeypatch.setattr(mailmod, "send_code",
                        lambda cfg, e, code, purpose: captured.setdefault("c", code))
    r = client.post("/api/auth/email", json={"email": email})
    assert r.status_code == 200
    r = client.post("/api/auth/verify", json={"email": email, "code": captured["c"]})
    assert r.status_code == 200
    return r.get_json()


# --- auth --------------------------------------------------------------------

def test_email_code_creates_account_and_session(app, svc, monkeypatch):
    c = app.test_client()
    data = _login(c, svc, monkeypatch)
    assert data["ok"] is True and data["has_passkey"] is False
    # session now authorized for gated pages
    assert c.get("/home").status_code == 200


def test_fresh_home_gates_agent_modal_and_shows_onboarding(app, svc, monkeypatch):
    """A brand-new account (no connections) must not have a visible agent modal
    and should be guided to connect records first (regression: the modal used
    to render on load because `.modal{display:flex}` beat the hidden attr)."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    html = c.get("/home").data.decode()
    # the modal element is present but carries the `hidden` attribute
    assert 'id="agent-modal"' in html
    modal = html.split('id="agent-modal"')[1][:40]
    assert "hidden" in modal
    # first-run onboarding: Step 1 points at connections, not the agent
    assert "Step 1" in html and "connect" in html.lower()


def test_wrong_email_code_rejected(app, svc, monkeypatch):
    c = app.test_client()
    import careagents.mail as mailmod
    monkeypatch.setattr(mailmod, "send_code", lambda *a: None)
    c.post("/api/auth/email", json={"email": "x@y.com"})
    r = c.post("/api/auth/verify", json={"email": "x@y.com", "code": "000000"})
    assert r.status_code == 400


def test_email_code_burns_after_max_attempts(svc, monkeypatch):
    """Anti-brute-force: a login code is burned after MAX_CODE_ATTEMPTS wrong
    guesses — even the correct code no longer works afterwards."""
    from careagents.accounts import MAX_CODE_ATTEMPTS, AuthError
    import careagents.mail as mailmod
    cap = {}
    monkeypatch.setattr(mailmod, "send_code",
                        lambda cfg, e, code, purpose: cap.__setitem__("c", code))
    svc.start_email_code("brute@example.com")
    real = cap["c"]
    assert len(real) == 8  # higher entropy than 6 digits
    for _ in range(MAX_CODE_ATTEMPTS):
        with pytest.raises(AuthError):
            svc.verify_email_code("brute@example.com", "00000001")
    with pytest.raises(AuthError):  # correct code is now burned
        svc.verify_email_code("brute@example.com", real)


def test_email_resend_invalidates_prior_code(svc, monkeypatch):
    """One live code at a time: a fresh send retires the previous code so an
    attacker can't accumulate many simultaneously-valid guesses."""
    from careagents.accounts import AuthError
    import careagents.mail as mailmod
    codes = []
    monkeypatch.setattr(mailmod, "send_code",
                        lambda cfg, e, code, purpose: codes.append(code))
    monkeypatch.setattr("careagents.accounts.RESEND_COOLDOWN", 0)  # skip cooldown
    svc.start_email_code("rotate@example.com")
    first = codes[-1]
    svc.start_email_code("rotate@example.com")
    second = codes[-1]
    with pytest.raises(AuthError):  # the old code was invalidated
        svc.verify_email_code("rotate@example.com", first)
    acct = svc.verify_email_code("rotate@example.com", second)
    assert acct.email == "rotate@example.com"


def test_email_resend_cooldown_suppresses_duplicate_send(svc, monkeypatch):
    """Within the cooldown a repeat request does not mint/send a new code."""
    import careagents.mail as mailmod
    codes = []
    monkeypatch.setattr(mailmod, "send_code",
                        lambda cfg, e, code, purpose: codes.append(code))
    svc.start_email_code("cool@example.com")
    svc.start_email_code("cool@example.com")  # within cooldown → suppressed
    assert len(codes) == 1


def test_gated_pages_redirect_or_401_without_session(app):
    c = app.test_client()
    assert c.get("/home").status_code == 302
    assert c.get("/chat?agent=x").status_code == 302
    assert c.post("/api/agents", json={}).status_code == 401
    assert c.post("/api/connections/sample").status_code == 401


def test_webauthn_options_are_issued_when_authed(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    reg = c.post("/webauthn/register/options")
    assert reg.status_code == 200 and "challenge" in reg.get_json()
    login = c.post("/webauthn/login/options")
    assert login.status_code == 200 and "challenge" in login.get_json()


def test_passkey_registration_and_login_via_faked_verification(app, svc, monkeypatch):
    """Exercise the route wiring with the WebAuthn crypto faked (no browser)."""
    c = app.test_client()
    acct = _login(c, svc, monkeypatch)  # noqa: F841
    # fake a successful registration
    class RegV:
        credential_id = b"cred-1"
        credential_public_key = b"pk-1"
        sign_count = 0
    monkeypatch.setattr("webauthn.verify_registration_response",
                        lambda **k: RegV())
    c.post("/webauthn/register/options")  # sets challenge in session
    r = c.post("/webauthn/register/verify", json={"id": "a", "rawId": "a",
                                                  "type": "public-key",
                                                  "response": {}})
    assert r.status_code == 200
    # a fresh client logs in with the passkey (verification faked)
    class AuthV:
        new_sign_count = 1
    monkeypatch.setattr("webauthn.verify_authentication_response",
                        lambda **k: AuthV())
    from webauthn.helpers import bytes_to_base64url
    c2 = app.test_client()
    c2.post("/webauthn/login/options")
    r = c2.post("/webauthn/login/verify",
                json={"rawId": bytes_to_base64url(b"cred-1"), "id": "x",
                      "type": "public-key", "response": {}})
    assert r.status_code == 200
    assert c2.get("/home").status_code == 200  # logged in as the same account


# --- connections / agents / scoping ------------------------------------------

def _make_agent(c):
    conn = c.post("/api/connections/sample").get_json()["id"]  # noqa: F841
    # need the connection_id from home
    return conn


def test_sample_connection_agent_and_chat_gate(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/sample")
    assert r.status_code == 200 and r.get_json()["status"] == "active"
    conn_id = r.get_json()["id"]
    r = c.post("/api/agents", json={"name": "Ada", "persona": "direct",
                                    "connection_id": conn_id})
    assert r.status_code == 200
    agent_id = r.get_json()["id"]
    assert b"Ada" in c.get(f"/chat?agent={agent_id}").data
    # unknown agent → redirect to hub
    assert c.get("/chat?agent=nope").status_code == 302
    # chat api rejects an agent that isn't the account's
    assert c.post("/api/chat", json={"message": "hi", "agent_id": "nope"}
                  ).status_code == 404


def test_connector_catalog_lists_apple_health(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    cat = c.get("/api/connections/catalog").get_json()["connectors"]
    by_id = {m["id"]: m for m in cat}
    assert by_id["sample"]["tier"] == "live"
    assert by_id["fasten"]["tier"] == "live"  # fasten key set in the fixture
    # wearable is "coming soon" until the sidecar is wired, but Apple Health is
    # visible as a provider so the demo shows it's supported.
    assert by_id["wearable"]["tier"] == "soon"
    labels = {p["label"] for p in by_id["wearable"]["providers"]}
    assert "Apple Health" in labels
    assert by_id["healthex"]["tier"] == "soon"


def test_wearable_connector_soon_by_default_live_when_enabled(svc, monkeypatch):
    from careagents import connectors
    from careagents.app import create_app
    from careagents.config import Config
    # default (no CARE_WEARABLES_ENABLED) → soon, no client call
    assert connectors.start("wearable", "apple", svc.cfg, FakeClient()) == {
        "soon": True}
    # enabled → live connect URL routed through HealthClaw wearables OAuth
    cfg2 = Config(env={"CARE_DATABASE_URL": "sqlite:///:memory:",
                       "OPENAI_API_KEY": "k", "HEALTHCLAW_MINT_SECRET": "m",
                       "CARE_WEARABLES_ENABLED": "1"})
    a = create_app(config=cfg2, client=FakeClient(), accounts=svc)
    a.config["TESTING"] = True
    c = a.test_client()
    _login(c, svc, monkeypatch, email="wear@example.com")
    r = c.post("/api/connections/wearable", json={"provider": "apple"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "pending"
    assert "/wearables/oauth/start?provider=apple" in d["connect_url"]


def test_coming_soon_connector_records_intent_not_error(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/healthex")
    assert r.status_code == 200 and r.get_json()["soon"] is True
    assert c.post("/api/connections/nonsense").status_code == 404


def test_agent_requires_own_connection(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/agents", json={"name": "X", "persona": "calm",
                                    "connection_id": "conn_foreign"})
    assert r.status_code == 400


def test_fasten_connection_returns_verified_provider_url(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/fasten")
    assert r.status_code == 200
    d = r.get_json()
    # routes through HealthClaw's own wired-up connect page, not a Fasten URL
    assert d["status"] == "pending" and "/connect/" in d["connect_url"]
    assert "app.healthclaw.io" in d["connect_url"]
    tenant = d["connect_url"].rsplit("/connect/", 1)[1]
    # the pending connection polls to active once records land
    assert c.get(f"/api/connections/{tenant}/poll").get_json()["status"] == "active"
    assert c.get("/api/connections/not-mine/poll").status_code == 404


# --- review relay ------------------------------------------------------------

def test_review_relay_is_agent_scoped_and_holds_the_gate(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    agent = c.post("/api/agents", json={"name": "A", "persona": "calm",
                                        "connection_id": conn}
                   ).get_json()["id"]
    assert c.get(f"/review/{agent}/act-1").status_code == 200
    assert c.get(f"/review/{agent}/not-mine").status_code == 404
    # gate relayed verbatim
    assert c.post(f"/review/{agent}/act-1/submit",
                  json={"med-0": "yes"}).status_code == 422
    assert c.post(f"/review/{agent}/act-1/submit",
                  json={"med-0": "yes", "nka": "true"}).status_code == 200
    # a stranger can't drive another account's review
    other = app.test_client()
    _login(other, svc, monkeypatch, email="mallory@example.com")
    assert other.get(f"/review/{agent}/act-1").status_code == 404


# --- telegram surface --------------------------------------------------------

def test_telegram_connect_and_bind_handshake(app, svc, monkeypatch, cfg):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    agent = c.post("/api/agents", json={"name": "A", "persona": "calm",
                                        "connection_id": conn}
                   ).get_json()["id"]
    r = c.post("/api/surfaces/telegram", json={"agent_id": agent})
    assert r.status_code == 200
    code = r.get_json()["code"]
    assert "carebot" in r.get_json()["deep_link"]
    # the bot calls the bind endpoint (mint-secret gated) with code + chat_id
    bind = app.test_client()
    assert bind.post("/api/surfaces/telegram/bind",
                     json={"code": f"care_{code}", "chat_id": 4242}).status_code == 403
    ok = bind.post("/api/surfaces/telegram/bind",
                   json={"code": f"care_{code}", "chat_id": 4242},
                   headers={"X-Internal-Secret": cfg.mint_secret})
    assert ok.status_code == 200
    assert bind.post("/api/surfaces/telegram/bind",
                     json={"code": "care_bogus", "chat_id": 1},
                     headers={"X-Internal-Secret": cfg.mint_secret}).status_code == 404


def test_imessage_connect_bind_inbound_flow(app, svc, monkeypatch, cfg):
    """iMessage runs the message loop in careagents itself: connect (get a
    code + handle) → relay binds the sender handle → relay forwards an inbound
    message and gets the agent's reply. Both server-to-server hops are
    mint-secret gated; the agent turn is faked (no LLM/network)."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    agent = c.post("/api/agents", json={"name": "Iris", "persona": "calm",
                                        "connection_id": conn}).get_json()["id"]

    r = c.post("/api/surfaces/imessage", json={"agent_id": agent})
    assert r.status_code == 200
    body = r.get_json()
    code = body["code"]
    assert body["handle"] == "+15550001111" and code in body["instructions"]

    relay = app.test_client()
    hdrs = {"X-Internal-Secret": cfg.mint_secret}
    # bind requires the mint secret
    assert relay.post("/api/surfaces/imessage/bind",
                      json={"code": code, "handle": "+15559998888"}
                      ).status_code == 403
    assert relay.post("/api/surfaces/imessage/bind", headers=hdrs,
                      json={"code": code, "handle": "+15559998888"}
                      ).status_code == 200
    assert relay.post("/api/surfaces/imessage/bind", headers=hdrs,
                      json={"code": "bogus", "handle": "+1"}
                      ).status_code == 404

    # inbound: fake the agent turn, assert the reply is relayed back
    monkeypatch.setattr("careagents.app.run_turn_to_message",
                        lambda *a, **k: "Your last A1c was 6.1% — in range.")
    assert relay.post("/api/surfaces/imessage/inbound",
                      json={"handle": "+15559998888", "text": "how's my a1c?"}
                      ).status_code == 403  # needs mint secret
    ok = relay.post("/api/surfaces/imessage/inbound", headers=hdrs,
                    json={"handle": "+15559998888", "text": "how's my a1c?"})
    assert ok.status_code == 200
    assert "6.1%" in ok.get_json()["reply"]
    # an unbound handle is not routed (don't answer strangers)
    assert relay.post("/api/surfaces/imessage/inbound", headers=hdrs,
                      json={"handle": "+1000", "text": "hi"}
                      ).status_code == 404


def test_imessage_reply_collapses_review_card_to_link(monkeypatch, cfg):
    """run_turn_to_message turns a review card into a link back to the web app
    (the human approval gate never happens inline in the thread)."""
    from careagents import agent as agent_mod
    from careagents.llm import LLMTurn, ToolCall
    seq = iter([LLMTurn(tool_calls=[ToolCall("1", "start_intake_form", {})]),
                LLMTurn(text="I've drafted your intake form.")])
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: next(seq))
    reply = agent_mod.run_turn_to_message(
        cfg, FakeClient(), "ca-x", "sys", [], "fill my intake form",
        origin="https://careagents.cloud", agent_id="agent_1")
    assert "drafted your intake form" in reply
    assert "https://careagents.cloud/review/agent_1/act-1" in reply


# --- agent loop (unchanged contract) -----------------------------------------

def test_agent_loop_emits_chip_card_then_text(monkeypatch, cfg):
    from careagents import agent as agent_mod
    from careagents.llm import LLMTurn, ToolCall
    seq = iter([LLMTurn(tool_calls=[ToolCall("1", "start_intake_form", {})]),
                LLMTurn(text="Review card is up.")])
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: next(seq))
    events = list(agent_mod.run_turn(cfg, FakeClient(), "ca-x", "sys", [], "fill it"))
    assert [(e["type"], e.get("kind")) for e in events] == [
        ("tool", None), ("card", "review"), ("text", None)]


def test_landing_and_auth_render(app):
    c = app.test_client()
    assert c.get("/").status_code == 200
    assert b"Get started" in c.get("/").data
    a = c.get("/auth")
    assert a.status_code == 200 and b"passkey" in a.data.lower()


def test_healthz_and_manifest(app):
    c = app.test_client()
    assert c.get("/healthz").get_json()["accounts"] is True
    m = c.get("/manifest.webmanifest").get_json()
    assert m["name"] == "CareAgents" and m["start_url"] == "/home"
