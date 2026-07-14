"""CareAgents unit tests — no network; the HealthClaw client and LLM are faked.

The live path is covered by scripts/careagents_smoke.py against the deployed
site; these tests pin the app's contracts: fail-closed config, one safety core
in every persona, the agent loop's event protocol, session/ownership guards,
and the rate limit.
"""

from __future__ import annotations

import pytest

from careagents.config import Config, ConfigError
from careagents.healthclaw import HealthClawError
from careagents.personas import PERSONAS, SAFETY_CORE, system_prompt


# --- config: fail-closed ------------------------------------------------------

def test_production_config_refuses_to_boot_half_configured():
    with pytest.raises(ConfigError):
        Config(env={"CARE_ENV": "production"})
    with pytest.raises(ConfigError):
        Config(env={"CARE_ENV": "production",
                    "CARE_SESSION_SECRET": "x" * 32})  # no mint secret
    with pytest.raises(ConfigError):
        Config(env={"CARE_ENV": "production",
                    "CARE_SESSION_SECRET": "x" * 32,
                    "HEALTHCLAW_MINT_SECRET": "m"})  # no LLM key
    cfg = Config(env={"CARE_ENV": "production",
                      "CARE_SESSION_SECRET": "x" * 32,
                      "HEALTHCLAW_MINT_SECRET": "m",
                      "OPENAI_API_KEY": "k"})
    assert cfg.provider == "openai"


def test_anthropic_preferred_when_key_present():
    cfg = Config(env={"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"})
    assert cfg.provider == "anthropic"


# --- personas: one safety core --------------------------------------------------

def test_every_persona_carries_the_same_safety_core():
    for key in PERSONAS:
        prompt = system_prompt("Juniper", key)
        assert SAFETY_CORE in prompt
        assert "911" in prompt
        assert "no known allergies" in prompt.lower()


# --- fakes -----------------------------------------------------------------------

class FakeClient:
    """Stands in for HealthClawClient; records calls, owns tenant 'ca-own'."""

    def __init__(self):
        self.seeded = []

    @staticmethod
    def new_tenant_id():
        return "ca-own"

    def seed(self, tenant):
        self.seeded.append(tenant)
        return 7

    def mint_token(self, tenant):
        return "tok"

    def search(self, tenant, rtype, params=None):
        return {"total": 1, "entry": [{"resource": {
            "resourceType": rtype, "status": "active",
            "code": {"text": f"sample {rtype}"}}}]}

    def interpret_labs(self, tenant):
        return {"summary": {}, "consumer": {"headline": "labs look ok"},
                "disclaimer": "not medical advice"}

    def care_gaps(self, tenant):
        return {"summary": {}, "consumer": {"due": ["flu shot"]}}

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

    def conformance_badge(self):
        return {"message": "A (7/7)"}


@pytest.fixture
def care_app():
    from careagents.app import create_app
    cfg = Config(env={"OPENAI_API_KEY": "k"})
    app = create_app(config=cfg, client=FakeClient())
    app.config["TESTING"] = True
    return app


@pytest.fixture
def care_client(care_app):
    return care_app.test_client()


# --- agent loop -------------------------------------------------------------------

def test_agent_loop_emits_chip_card_then_text(monkeypatch):
    from careagents import agent as agent_mod
    from careagents.llm import LLMTurn, ToolCall

    turns = iter([
        LLMTurn(tool_calls=[ToolCall(id="1", name="start_intake_form",
                                     arguments={})]),
        LLMTurn(text="Form started — review card is up."),
    ])
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: next(turns))

    history: list = []
    events = list(agent_mod.run_turn(
        Config(env={"OPENAI_API_KEY": "k"}), FakeClient(), "ca-own",
        "sys", history, "fill my form"))

    kinds = [(e["type"], e.get("kind")) for e in events]
    assert kinds == [("tool", None), ("card", "review"), ("text", None)]
    assert events[1]["review_url"] == "/review/act-1"
    # History carries the full tool exchange for the next turn.
    roles = [m["role"] for m in history]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_agent_loop_surfaces_llm_failure_as_error_event(monkeypatch):
    from careagents import agent as agent_mod
    from careagents.llm import LLMError

    def boom(*a, **k):
        raise LLMError("model call failed (HTTP 500)")
    monkeypatch.setattr(agent_mod.llm, "complete", boom)

    events = list(agent_mod.run_turn(
        Config(env={"OPENAI_API_KEY": "k"}), FakeClient(), "ca-own",
        "sys", [], "hi"))
    assert events == [{"type": "error",
                       "text": "model call failed (HTTP 500)"}]


# --- app routes ---------------------------------------------------------------------

def test_landing_and_setup_render(care_client):
    assert care_client.get("/").status_code == 200
    assert b"Create your agent" in care_client.get("/").data
    assert care_client.get("/start").status_code == 200


def test_setup_creates_seeded_session_and_chat_requires_one(care_app):
    c = care_app.test_client()
    assert c.get("/chat").status_code == 302  # no session → setup
    r = c.post("/start", data={"agent_name": "Ada", "persona": "direct"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/chat")
    page = c.get("/chat")
    assert page.status_code == 200 and b"Ada" in page.data


def test_setup_rejects_hostile_agent_name(care_app):
    c = care_app.test_client()
    c.post("/start", data={"agent_name": "<script>alert(1)</script>",
                           "persona": "calm"})
    assert b"<script>alert(1)</script>" not in c.get("/chat").data


def test_review_proxy_refuses_foreign_action(care_app):
    c = care_app.test_client()
    c.post("/start", data={"agent_name": "Ada", "persona": "calm"})
    assert c.get("/review/act-1").status_code == 200
    assert c.get("/review/not-mine").status_code == 404
    assert c.post("/review/not-mine/submit", json={}).status_code == 404


def test_review_submit_relays_attestation_gate(care_app):
    c = care_app.test_client()
    c.post("/start", data={"agent_name": "Ada", "persona": "calm"})
    r = c.post("/review/act-1/submit", json={"med-0": "yes"})
    assert r.status_code == 422  # gate verdict relayed verbatim
    r = c.post("/review/act-1/submit", json={"med-0": "yes", "nka": "true"})
    assert r.status_code == 200


def test_chat_api_requires_session_and_rate_limits(care_app):
    c = care_app.test_client()
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 401
    c.post("/start", data={"agent_name": "Ada", "persona": "calm"})
    assert c.post("/api/chat", json={"message": ""}).status_code == 400

    # Exhaust the window (fake the LLM so allowed turns stream cleanly).
    import careagents.agent as agent_mod
    from careagents.llm import LLMTurn
    orig = agent_mod.llm.complete
    agent_mod.llm.complete = lambda *a, **k: LLMTurn(text="ok")
    try:
        codes = [c.post("/api/chat", json={"message": f"m{i}"}).status_code
                 for i in range(21)]
    finally:
        agent_mod.llm.complete = orig
    assert codes.count(200) == 20 and codes[-1] == 429


def test_magic_link_roundtrip(care_app):
    c = care_app.test_client()
    c.post("/start", data={"agent_name": "Mo", "persona": "sunny"})
    link = None
    page = c.get("/chat").get_data(as_text=True)
    import re
    m = re.search(r'data-link="([^"]+)"', page)
    assert m, "chat page exposes the magic link"
    link = m.group(1)
    path = "/m/" + link.rsplit("/m/", 1)[1]

    fresh = care_app.test_client()
    r = fresh.get(path)
    assert r.status_code == 302 and r.headers["Location"].endswith("/chat")
    assert b"Mo" in fresh.get("/chat").data


def test_form_status_endpoint_scoped_to_session(care_app):
    c = care_app.test_client()
    assert c.get("/api/form/act-1").status_code == 401
    c.post("/start", data={"agent_name": "Ada", "persona": "calm"})
    r = c.get("/api/form/act-1")
    assert r.status_code == 200
    assert r.get_json()["delivery_link"] == "https://x/pdf"
    assert c.get("/api/form/other").status_code == 404


def test_healthz(care_client):
    r = care_client.get("/healthz")
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
