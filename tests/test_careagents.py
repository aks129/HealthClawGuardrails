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
                     "RESEND_API_KEY": "r",
                     "REDIS_URL": "redis://localhost:6379/1"})
    assert ok.provider == "openai" and ok.rp_id == "careagents.cloud"


def test_production_on_sqlite_warns_loudly(caplog):
    # SQLite in production is single-writer and host-local. It is not a hard
    # failure yet (the live deployment still runs on it, and refusing to boot
    # would take it down rather than migrate it) — but it must never be quiet.
    import logging
    with caplog.at_level(logging.WARNING, logger="careagents.config"):
        Config(env={"CARE_ENV": "production", "CARE_SESSION_SECRET": "x" * 32,
                    "HEALTHCLAW_MINT_SECRET": "m", "OPENAI_API_KEY": "k",
                    "RESEND_API_KEY": "r",
                    "REDIS_URL": "redis://localhost:6379/1",
                    "CARE_DATABASE_URL": "sqlite:///careagents.db"})
    assert any("SQLite" in r.message and "Postgres" in r.message
               for r in caplog.records)


def test_production_on_postgres_does_not_warn(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="careagents.config"):
        cfg = Config(env={"CARE_ENV": "production",
                          "CARE_SESSION_SECRET": "x" * 32,
                          "HEALTHCLAW_MINT_SECRET": "m", "OPENAI_API_KEY": "k",
                          "RESEND_API_KEY": "r",
                          "REDIS_URL": "redis://localhost:6379/1",
                          "CARE_DATABASE_URL":
                              "postgresql://u:p@db:5432/care"})
    assert cfg.database_url.startswith("postgresql")
    assert not any("SQLite" in r.message for r in caplog.records)


def test_auth_email_reports_failure_instead_of_claiming_it_sent(app, svc,
                                                                monkeypatch):
    # Login is the front door. This used to discard send_code's return value
    # and answer {"sent": true} even when Resend was down, leaving the person
    # watching an empty inbox with no idea whether to wait or retry.
    import careagents.mail as mailmod
    monkeypatch.setattr(mailmod, "send_code",
                        lambda cfg, e, code, purpose: False)
    r = app.test_client().post("/api/auth/email",
                               json={"email": "gene@example.com"})
    assert r.status_code == 502
    assert r.get_json()["sent"] is False


def test_a_failed_send_does_not_block_the_retry(app, svc, monkeypatch):
    # The code row is committed before the send, so an undelivered code left
    # live would make the resend cooldown swallow the retry and report success
    # without sending anything.
    import careagents.mail as mailmod
    monkeypatch.setattr(mailmod, "send_code",
                        lambda cfg, e, code, purpose: False)
    c = app.test_client()
    assert c.post("/api/auth/email",
                  json={"email": "retry@example.com"}).status_code == 502

    sent = {}
    monkeypatch.setattr(mailmod, "send_code",
                        lambda cfg, e, code, purpose: sent.setdefault("c", code)
                        is not None)
    r = c.post("/api/auth/email", json={"email": "retry@example.com"})
    assert r.status_code == 200 and sent.get("c"), "retry never sent a code"


def test_review_submit_reports_a_failed_confirmation(cfg, svc, monkeypatch):
    # The person approved. If the confirmation doesn't land, the action sits
    # unexecuted — telling them it succeeded is the worst possible answer.
    from careagents.app import create_app

    class _Fake(FakeClient):
        def submit_review(self, tenant, action_id, decisions):
            return 200, {"ok": True}

        def confirm_action(self, tenant, action_id):
            raise HealthClawError("upstream down", 500)

    fake = _Fake()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    created = c.post("/api/connections/sample").get_json()
    agent_id = c.post("/api/agents", json={
        "name": "A", "persona": "calm",
        "connection_id": created["id"]}).get_json()["id"]

    monkeypatch.setattr(fake, "action_status",
                        lambda t, a: {"status": "awaiting_confirmation"})
    r = c.post(f"/review/{agent_id}/act-1/submit", json={"approved": []})
    assert r.status_code == 502
    body = r.get_json()
    assert body["confirmed"] is False
    assert "not" in body["message"].lower() or "couldn't" in body["message"]


# --- chat persistence (#222) --------------------------------------------------

def _turn(client, agent_id, message, request_id=None, conversation_id=None):
    """Post a chat turn AND drain the SSE stream, as a browser does.

    The response body is a generator; without reading it the turn never
    actually runs to completion.
    """
    payload = {"agent_id": agent_id, "message": message}
    if request_id:
        payload["request_id"] = request_id
    if conversation_id:
        payload["conversation_id"] = conversation_id
    r = client.post("/api/chat", json=payload)
    r.get_data()
    return r


def _chat_app(cfg, svc, monkeypatch, reply="here you go"):
    """An app whose LLM answers immediately, plus a logged-in client."""
    from careagents import agent as agent_mod
    from careagents.app import create_app

    class _Turn:
        def __init__(self):
            self.text, self.tool_calls, self.raw_tool_calls = reply, [], []

    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: _Turn())
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    return app, c, fake, agent_id, fake.tenants[-1], conn["id"]


def test_log_message_sends_explicit_agent_and_conversation_identity():
    """The wire contract preserves CareAgents identity across surfaces."""
    import careagents.healthclaw as hcmod

    sent = {}

    class _Resp:
        status_code = 201

        def json(self):
            return {"id": "message-1"}

    class _HTTP:
        def post(self, url, json=None, headers=None, timeout=None):
            sent.update(json or {})
            return _Resp()

    client = hcmod.HealthClawClient("http://local", "s")
    client.http = _HTTP()
    client.mint_token = lambda tenant: "tok"

    assert client.log_message(
        "t-1", "user", "hi", "ag_care_123",
        "careagents:ag_care_123", surface="web",
    ) is True
    assert sent["agent_id"] == "ag_care_123"
    assert sent["conversation_id"] == "careagents:ag_care_123"
    assert sent["surface"] == "web"
    assert sent["metadata"]["careagents_agent_id"] == "ag_care_123"
    assert sent["tenant_id"] == "t-1" and sent["text"] == "hi"


def test_log_message_reports_failure_on_a_non_201():
    import careagents.healthclaw as hcmod

    class _Resp:
        status_code = 400

    class _HTTP:
        def post(self, *a, **k):
            return _Resp()

    client = hcmod.HealthClawClient("http://local", "s")
    client.http = _HTTP()
    client.mint_token = lambda tenant: "tok"
    assert client.log_message("t-1", "user", "hi") is False


def test_confirm_action_mints_action_bound_credential_then_uses_it():
    import careagents.healthclaw as hcmod

    calls = []

    class _Resp:
        def __init__(self, body):
            self.status_code = 200
            self.ok = True
            self._body = body

        def json(self):
            return self._body

    class _HTTP:
        def post(self, url, json=None, headers=None, timeout=None):
            calls.append((url, json, headers))
            if url.endswith('/approval-token'):
                return _Resp({'token': 'action-bound-token'})
            return _Resp({'status': 'completed'})

    client = hcmod.HealthClawClient("http://local", "mint-secret")
    client.http = _HTTP()

    assert client.confirm_action("tenant-1", "action-1") == {
        'status': 'completed'}
    assert calls[0][0].endswith('/r6/actions/action-1/approval-token')
    assert calls[0][2] == {
        'X-Tenant-Id': 'tenant-1', 'X-Internal-Secret': 'mint-secret'}
    assert calls[1][0].endswith('/r6/actions/action-1/confirm')
    assert calls[1][1] == {'approved_via': 'review-page'}
    assert calls[1][2]['X-Step-Up-Token'] == 'action-bound-token'


def test_a_chat_turn_is_persisted_to_healthclaw_not_careagents(
        cfg, svc, monkeypatch):
    # The transcript is PHI-adjacent, so it belongs behind the guardrails in
    # the tenant — never in CareAgents' own tables.
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(cfg, svc, monkeypatch)
    _turn(c, agent_id, "hi there")

    stored = fake.logged[(tenant, fake.conversation_id(agent_id))]
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert stored[0]["content"] == "hi there"
    assert stored[1]["content"] == "here you go"


def test_a_returning_person_sees_the_conversation_they_had(cfg, svc,
                                                           monkeypatch):
    # The core of #222: process memory is a cache, not the record. Wiping it
    # (a deploy, a restart, idle eviction) must not make the agent forget.
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(cfg, svc, monkeypatch)
    _turn(c, agent_id, "am I due for a flu shot?")

    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert "am I due for a flu shot?" in page
    assert "Picking up where you left off" in page


def test_a_cold_process_rehydrates_instead_of_starting_over(cfg, svc,
                                                            monkeypatch):
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(cfg, svc, monkeypatch)
    _turn(c, agent_id, "first question")

    # Simulate the restart: everything in memory is gone, the store is not.
    seen = []
    from careagents import agent as agent_mod

    class _Turn:
        def __init__(self):
            self.text, self.tool_calls, self.raw_tool_calls = "ok", [], []

    def _capture(cfg_, system, history, tools):
        seen.append([m.get("content") for m in history])
        return _Turn()

    monkeypatch.setattr(agent_mod.llm, "complete", _capture)
    from careagents.app import create_app
    fresh = create_app(config=cfg, client=fake, accounts=svc)
    fresh.config["TESTING"] = True
    c2 = fresh.test_client()
    _login(c2, svc, monkeypatch)
    _turn(c2, agent_id, "second question")

    assert "first question" in seen[0], "the new process forgot the conversation"
    assert seen[0].count("second question") == 1


def test_two_agents_on_one_connection_have_isolated_transcripts(
        cfg, svc, monkeypatch):
    _app, c, fake, first_agent, tenant, conn_id = _chat_app(
        cfg, svc, monkeypatch)
    second_agent = c.post("/api/agents", json={
        "name": "Cedar",
        "persona": "calm",
        "connection_id": conn_id,
    }).get_json()["id"]

    _turn(c, first_agent, "only Juniper should see this")
    _turn(c, second_agent, "only Cedar should see this")

    first = fake.logged[(tenant, fake.conversation_id(first_agent))]
    second = fake.logged[(tenant, fake.conversation_id(second_agent))]
    assert "Cedar" not in " ".join(m["content"] for m in first)
    assert "Juniper" not in " ".join(m["content"] for m in second)
    assert first[0]["content"] == "only Juniper should see this"
    assert second[0]["content"] == "only Cedar should see this"


def test_duplicate_inbound_request_runs_the_model_once(cfg, svc, monkeypatch):
    _app, c, fake, agent_id, tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)
    from careagents import agent as agent_mod

    calls = {"count": 0}

    class _Turn:
        text, tool_calls, raw_tool_calls = "once", [], []

    def _complete(*args, **kwargs):
        calls["count"] += 1
        return _Turn()

    monkeypatch.setattr(agent_mod.llm, "complete", _complete)
    first = _turn(c, agent_id, "one delivery", request_id="delivery-1")
    replay = _turn(c, agent_id, "one delivery", request_id="delivery-1")

    assert calls["count"] == 1
    assert '"type": "duplicate"' in replay.get_data(as_text=True)
    stored = fake.logged[(tenant, fake.conversation_id(agent_id))]
    assert [message["role"] for message in stored] == ["user", "assistant"]
    assert first.status_code == replay.status_code == 200


def test_web_and_imessage_resume_the_same_explicit_conversation(
        cfg, svc, monkeypatch):
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)
    _turn(c, agent_id, "remember this from web", request_id="web-1")

    surface = c.post(
        "/api/surfaces/imessage", json={"agent_id": agent_id}).get_json()
    relay = app.test_client()
    headers = {"X-Internal-Secret": cfg.mint_secret}
    assert relay.post(
        "/api/surfaces/imessage/bind",
        headers=headers,
        json={"code": surface["code"], "handle": "+15551234567"},
    ).status_code == 200

    seen = []

    def reply(_cfg, _hc, _tenant, _system, history, text, **kwargs):
        seen.extend(message["content"] for message in history)
        return "continued on iMessage"

    monkeypatch.setattr("careagents.app.run_turn_to_message", reply)
    response = relay.post(
        "/api/surfaces/imessage/inbound",
        headers=headers,
        json={
            "handle": "+15551234567",
            "text": "continue here",
            "request_id": "imessage-1",
            "conversation_id": fake.conversation_id(agent_id),
        },
    )

    assert response.status_code == 200
    assert "remember this from web" in seen
    stored = fake.logged[(tenant, fake.conversation_id(agent_id))]
    assert {message["surface"] for message in stored} == {"web", "imessage"}


def test_conversation_lock_serializes_concurrent_turns():
    import threading
    import time

    from careagents.conversation_locks import ConversationTurnLocks

    locks = ConversationTurnLocks()
    state = {"active": 0, "maximum": 0}
    guard = threading.Lock()

    def work():
        with locks.hold("tenant:careagents:agent"):
            with guard:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.02)
            with guard:
                state["active"] -= 1

    threads = [threading.Thread(target=work) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state["maximum"] == 1
    assert locks._local == {}, "idle conversation locks leaked forever"


def test_a_storage_outage_does_not_break_the_chat(cfg, svc, monkeypatch):
    # A user turn must be durably claimed before inference. Otherwise a retry
    # can execute the same health action twice.
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(fake, "claim_inbound_message",
                        lambda *a, **k: (None, None))
    r = _turn(c, agent_id, "still works?")
    assert r.status_code == 200
    assert "could not save this message safely" in r.get_data(as_text=True)


def test_history_is_trimmed_but_never_orphans_a_tool_call():
    # Unbounded history meant cost grew every turn until the request blew past
    # the context window, after which that person's chat failed permanently.
    # The trim must not cut between an assistant tool_call and its tool result
    # — the provider APIs reject that outright.
    from careagents.agent import MAX_HISTORY_MESSAGES, _trim_history
    history = []
    for i in range(60):
        history.append({"role": "user", "content": f"q{i}"})
        history.append({"role": "assistant", "content": "",
                        "tool_calls": [{"id": f"c{i}", "name": "get_labs",
                                        "arguments": {}}]})
        history.append({"role": "tool", "tool_call_id": f"c{i}",
                        "content": "{}"})
        history.append({"role": "assistant", "content": f"a{i}"})

    _trim_history(history)

    assert len(history) <= MAX_HISTORY_MESSAGES
    assert history[0]["role"] == "user"           # cut at a safe boundary
    # every surviving tool result still has its call in view
    call_ids = {c["id"] for m in history for c in m.get("tool_calls", [])}
    for msg in history:
        if msg.get("role") == "tool":
            assert msg["tool_call_id"] in call_ids


def test_history_shorter_than_the_cap_is_left_alone():
    from careagents.agent import _trim_history
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "hello"}]
    _trim_history(history)
    assert len(history) == 2


def test_tool_loop_stops_at_the_budget_instead_of_spending_forever(
        cfg, monkeypatch):
    # The budget nudge used to be appended inside `while True` with no exit, so
    # a model that kept calling tools was never stopped — it just collected
    # another nudge each round.
    from careagents import agent as agent_mod

    calls = {"n": 0, "toolless": 0}

    class _Call:
        def __init__(self, i):
            self.id, self.name, self.arguments = f"c{i}", "get_labs", {}

    class _Turn:
        def __init__(self, tool_calls):
            self.text = "working" if tool_calls else "final answer"
            self.tool_calls = tool_calls
            self.raw_tool_calls = []

    def fake_complete(cfg_, system, history, tools):
        calls["n"] += 1
        if not tools:                      # the forced final call
            calls["toolless"] += 1
            return _Turn([])
        return _Turn([_Call(calls["n"])])  # never stops asking for tools

    class _HC:
        def interpret_labs(self, tenant):
            return {"consumer": "ok", "disclaimer": "d"}

    monkeypatch.setattr(agent_mod.llm, "complete", fake_complete)
    events = list(agent_mod.run_turn(cfg, _HC(), "t-1", "sys", [], "hi"))

    assert calls["toolless"] == 1, "must make one final tools-free call"
    assert calls["n"] <= agent_mod.MAX_TOOL_ROUNDS + 1
    assert events[-1] == {"type": "text", "text": "final answer"}


def test_healthz_reports_ok_when_the_account_store_answers(app):
    r = app.test_client().get("/healthz")
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "ok" and d["accounts"] is True


def test_healthz_reports_503_when_the_account_store_is_unreachable(app, svc,
                                                                   monkeypatch):
    # The point of a readiness check: a container that cannot reach its
    # database must NOT advertise itself as healthy, or a load balancer will
    # route real sign-ins straight into failure. This used to be hard-coded
    # true, which would have made the Postgres cutover silently dangerous.
    monkeypatch.setattr(svc, "ping", lambda: False)
    r = app.test_client().get("/healthz")
    assert r.status_code == 503
    assert r.get_json()["accounts"] is False


def test_ping_returns_false_instead_of_raising(svc, monkeypatch):
    def boom():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(svc, "session", boom)
    assert svc.ping() is False


def test_anthropic_oauth_token_selects_anthropic_provider():
    # An OAuth token (Claude subscription / OpenClaw) is a valid LLM credential
    # and selects the Anthropic provider without an API key.
    dev = Config(env={"CARE_DATABASE_URL": "sqlite:///:memory:",
                      "HEALTHCLAW_MINT_SECRET": "m",
                      "ANTHROPIC_OAUTH_TOKEN": "oat-abc"})
    assert dev.provider == "anthropic" and dev.anthropic_oauth_token == "oat-abc"
    prod = Config(env={"CARE_ENV": "production",
                       "CARE_SESSION_SECRET": "x" * 32,
                       "HEALTHCLAW_MINT_SECRET": "m", "RESEND_API_KEY": "r",
                       "REDIS_URL": "redis://localhost:6379/1",
                       "ANTHROPIC_OAUTH_TOKEN": "oat-abc"})
    assert prod.provider == "anthropic"  # satisfies the prod LLM-cred gate


def test_unreadable_record_is_never_summarized_as_nothing():
    # #207: when a record had no readable label the summarizer dropped the
    # name key, the model saw an empty item, and answered "no, you do not
    # have that". An unnamed record must still be visible AND flagged.
    from careagents.agent import _summarize_bundle
    bundle = {"entry": [{"resource": {
        "resourceType": "Condition",
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-9-cm",
                             "code": "250.00"}]},   # no display
    }}]}
    items = _summarize_bundle(bundle)
    assert len(items) == 1
    assert items[0]["unreadable"] is True
    assert "250.00" in items[0]["name"]


def test_safety_core_forbids_answering_absence_with_a_bare_no():
    # The model must not convert "I couldn't read it" into "you don't have it".
    assert "bare \"no\"" in SAFETY_CORE or "bare 'no'" in SAFETY_CORE
    assert "unreadable" in SAFETY_CORE.lower()


def test_every_persona_shares_the_safety_core():
    for key in PERSONAS:
        p = system_prompt("Juniper", key)
        assert SAFETY_CORE in p and "911" in p and "no known allergies" in p.lower()


# --- fakes -------------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.bound = []
        self.seeded = []
        # Tenants this client minted, so a test can name the one it just made.
        self.tenants: list[str] = []
        # Conversation store, keyed by tenant + thread, matching HealthClaw.
        self.logged: dict[tuple[str, str], list] = {}
        self.claimed: dict[tuple[str, str, str], str] = {}

    @staticmethod
    def conversation_id(agent_id):
        return f"careagents:{agent_id}"

    def claim_inbound_message(self, tenant, text, agent_id, conversation_id,
                              surface, request_id):
        claim = (tenant, conversation_id, request_id)
        if claim in self.claimed:
            return False, self.claimed[claim]
        message_id = f"message-{len(self.claimed) + 1}"
        self.claimed[claim] = message_id
        self.logged.setdefault((tenant, conversation_id), []).append({
            "id": message_id,
            "role": "user",
            "content": text,
            "agent_id": agent_id,
            "surface": surface,
            "request_id": request_id,
        })
        return True, message_id

    def log_message(self, tenant, role, text, agent_id=None,
                    conversation_id=None, surface="web", reply_to=None):
        conversation_id = conversation_id or self.conversation_id(agent_id)
        self.logged.setdefault((tenant, conversation_id), []).append(
            {"role": role, "content": text, "agent_id": agent_id,
             "surface": surface, "reply_to": reply_to})
        return True

    def recent_messages(self, tenant, limit=20, conversation_id=None,
                        agent_id=None):
        conversation_id = conversation_id or self.conversation_id(agent_id)
        return list(self.logged.get((tenant, conversation_id), []))[-limit:]

    def new_tenant_id(self):
        self.seeded.append(1)
        tenant = f"ca-{len(self.seeded):010d}"
        self.tenants.append(tenant)
        return tenant

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

    # Settable so a test can simulate a refresh pulling additional records.
    counted = 100

    def record_count(self, tenant):
        return self.counted

    purge_fails = False
    purged = []

    def purge_tenant(self, tenant):
        if self.purge_fails:
            raise HealthClawError("purge failed", 500)
        self.purged.append(tenant)
        return {"tenant_id": tenant, "deleted": True, "rows_deleted": 42}

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
    """App config for tests.

    Defaults to in-memory SQLite. CI's Postgres lane sets
    CARE_TEST_DATABASE_URL so this same suite runs against real Postgres —
    careagents keeps its own engine and metadata, so SQLite-only coverage
    would never catch a Postgres-specific schema or type problem (exactly
    the class of bug the main app's Postgres lane exists to catch).
    """
    import os
    url = os.environ.get("CARE_TEST_DATABASE_URL", "sqlite:///:memory:")
    if not url.startswith("sqlite"):
        # A shared server-side database persists between tests; start each one
        # from a clean schema so ordering can't make the suite flaky.
        from careagents.models import Base, make_engine
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    return Config(env={"CARE_DATABASE_URL": url,
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


def _make_account(svc, monkeypatch, email):
    """Create a real account row for service-level tests.

    Connections/agents/surfaces carry a foreign key to ca_accounts. SQLite
    doesn't enforce it by default, so a made-up account id passes locally and
    fails on Postgres — use this instead of inventing ids.
    """
    import careagents.mail as mailmod
    captured = {}
    monkeypatch.setattr(mailmod, "send_code",
                        lambda cfg, e, code, purpose: captured.setdefault("c", code))
    svc.start_email_code(email)
    return svc.verify_email_code(email, captured["c"])


def _sink_code(sink):
    """Stand-in for mail.send_code that records the code and reports success.

    Returning True is load-bearing: a falsy return now means "the send failed"
    and raises MailError (#220), so a fake that returns None — as bare
    list.append and dict.__setitem__ do — reads as an outage.
    """
    def _send(cfg, email, code, purpose):
        sink.append(code)
        return True
    return _send


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
    monkeypatch.setattr(mailmod, "send_code", lambda *a: True)
    c.post("/api/auth/email", json={"email": "x@y.com"})
    r = c.post("/api/auth/verify", json={"email": "x@y.com", "code": "000000"})
    assert r.status_code == 400


def test_email_code_burns_after_max_attempts(svc, monkeypatch):
    """Anti-brute-force: a login code is burned after MAX_CODE_ATTEMPTS wrong
    guesses — even the correct code no longer works afterwards."""
    from careagents.accounts import MAX_CODE_ATTEMPTS, AuthError
    import careagents.mail as mailmod
    cap = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(cap))
    svc.start_email_code("brute@example.com")
    real = cap[0]
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
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
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
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
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


def test_no_connector_tile_advertises_a_flow_that_does_not_exist(cfg):
    # The house rule (#166, #170): ship the mechanism, then the copy. A tile
    # that isn't wired must present as coming-soon rather than instructing an
    # action that silently does nothing (#225).
    from careagents import connectors
    for item in connectors.catalog(cfg):
        plan = connectors.start(item["id"], None, cfg, FakeClient())
        if plan.get("soon"):
            assert item["tier"] == "soon", (
                f"{item['id']} is advertised as '{item['tier']}' but its "
                f"start() is not implemented")


def test_the_consent_card_points_at_the_delete_button_it_ships_with(
        app, svc, monkeypatch):
    # It told people to email support to leave, while self-serve Disconnect
    # and Delete sat on the same page — understating our strongest privacy
    # control in the one place people read carefully.
    c = app.test_client()
    _login(c, svc, monkeypatch)
    body = c.get("/home").get_data(as_text=True)
    assert "Disconnect" in body and "Delete" in body
    leaving = body[body.index("<b>Leaving:</b>"):][:600]
    assert "yourself" in leaving


def test_a_signed_in_person_can_still_add_a_passkey(app, svc, monkeypatch):
    # #223: /auth bounced every logged-in visitor to /home, so the only chance
    # to enrol was the one screen after first email verification. Skip it once
    # and "sign in with your face" silently became email codes forever.
    c = app.test_client()
    _login(c, svc, monkeypatch)

    r = c.get("/auth?enroll=1")
    assert r.status_code == 200, "enrolment is unreachable once signed in"
    body = r.get_data(as_text=True)

    def _tag(div_id):
        start = body.index(f'<div id="{div_id}"')
        return body[start:body.index(">", start)]

    # The enrolment step must be the visible one, and the sign-in step hidden —
    # landing on a hidden panel would look like a blank page.
    assert "hidden" not in _tag("step-passkey")
    assert "hidden" in _tag("step-start")


def test_plain_auth_still_sends_a_signed_in_person_home(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.get("/auth")
    assert r.status_code == 302 and "/home" in r.headers["Location"]


def test_the_hub_links_to_a_reachable_enrolment_page(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert "/auth?enroll=1" in c.get("/home").get_data(as_text=True)


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
    r = c.post("/api/connections/wearable",
               json={"provider": "apple", "consent": True})
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
    r = c.post("/api/connections/fasten", json={"consent": True})
    assert r.status_code == 200
    d = r.get_json()
    # routes through HealthClaw's own wired-up connect page, not a Fasten URL
    assert d["status"] == "pending" and "/connect/" in d["connect_url"]
    assert "app.healthclaw.io" in d["connect_url"]
    tenant = d["connect_url"].rsplit("/connect/", 1)[1]
    # the pending connection polls to active once records land
    assert c.get(f"/api/connections/{tenant}/poll").get_json()["status"] == "active"
    assert c.get("/api/connections/not-mine/poll").status_code == 404


# --- refresh an existing connection ------------------------------------------

def test_refresh_sample_connection_is_honestly_unsupported(app, svc, monkeypatch):
    # Synthetic data is generated, not fetched — say so instead of pretending
    # to sync (and instead of re-seeding the same fixture).
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    r = c.post(f"/api/connections/{conn}/refresh")
    assert r.status_code == 200
    d = r.get_json()
    assert d["unsupported"] is True
    assert "synthetic" in d["reason"].lower()


def test_refresh_real_connection_returns_reauth_url(app, svc, monkeypatch):
    # No stored provider credentials by design: refresh hands the patient back
    # to the same connect page rather than replaying a long-lived token.
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/fasten",
                  json={"consent": True}).get_json()["id"]
    r = c.post(f"/api/connections/{conn}/refresh")
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "reauth"
    assert "/connect/" in d["reauth_url"]


def test_refresh_rejects_a_connection_you_do_not_own(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert c.post("/api/connections/conn_someone_else/refresh").status_code == 404


def test_refresh_requires_a_session(app):
    assert app.test_client().post(
        "/api/connections/conn_x/refresh").status_code == 401


def test_poll_reports_new_records_added_since_the_refresh(cfg, svc, monkeypatch):
    # The whole point of refresh: tell the patient what it actually pulled.
    from careagents.app import create_app
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    created = c.post("/api/connections/fasten",
                     json={"consent": True}).get_json()
    conn = created["id"]
    tenant = created["connect_url"].rsplit("/connect/", 1)[1]

    # Refresh baselines the count at the current 100 ...
    assert c.post(f"/api/connections/{conn}/refresh").status_code == 200
    # ... then the provider delivers 12 more.
    fake.counted = 112

    d = c.get(f"/api/connections/{tenant}/poll").get_json()
    assert d["status"] == "active"
    assert d["record_count"] == 112
    assert d["new_records"] == 12


def test_first_sync_reports_no_phantom_new_records(svc, monkeypatch):
    # With no prior baseline every record would look "new" — report 0 rather
    # than a misleading number.
    # Uses a REAL account: ca_connections.account_id is a foreign key, which
    # SQLite ignores by default but Postgres enforces (caught by the new
    # careagents-on-Postgres CI lane).
    acct = _make_account(svc, monkeypatch, "firstsync@example.com")
    cid = svc.add_connection(acct.id, "fasten", "t-1", "My provider",
                             status="pending", consent_version="v1")
    assert svc.mark_synced(cid, 40) == {"new": 0, "total": 40}
    assert svc.mark_synced(cid, 52) == {"new": 12, "total": 52}
    # A shrinking count (provider removed records) never reports negative.
    assert svc.mark_synced(cid, 50)["new"] == 0


# --- disconnect + delete (self-serve) ----------------------------------------

def test_disconnect_marks_revoked_but_keeps_the_records(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    r = c.post(f"/api/connections/{conn}/disconnect")
    assert r.status_code == 200 and r.get_json()["status"] == "revoked"
    # still listed — disconnect stops new data, it doesn't erase what's here
    home = c.get("/home").get_data(as_text=True)
    assert "revoked" in home


def test_delete_purges_records_then_removes_the_connection(cfg, svc, monkeypatch):
    from careagents.app import create_app
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    created = c.post("/api/connections/sample").get_json()
    conn = created["id"]

    r = c.delete(f"/api/connections/{conn}")
    assert r.status_code == 200
    d = r.get_json()
    assert d["deleted"] is True and d["rows_deleted"] == 42
    assert d["audit_retained"] is True          # says so to the patient
    assert len(fake.purged) == 1                # records purged, not just unlinked
    # connection is gone from the hub
    assert c.post(f"/api/connections/{conn}/disconnect").status_code == 404


def test_delete_does_not_unlink_when_the_purge_fails(cfg, svc, monkeypatch):
    # Never leave a clean-looking hub while the data still sits in the engine,
    # and never tell the patient it's deleted when it isn't.
    from careagents.app import create_app
    fake = FakeClient()
    fake.purge_fails = True
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]

    r = c.delete(f"/api/connections/{conn}")
    assert r.status_code == 502
    assert r.get_json()["deleted"] is False
    # connection survives, so the patient can retry
    assert c.post(f"/api/connections/{conn}/disconnect").status_code == 200


def test_delete_and_disconnect_reject_other_peoples_connections(app, svc,
                                                                monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert c.delete("/api/connections/conn_not_mine").status_code == 404
    assert c.post(
        "/api/connections/conn_not_mine/disconnect").status_code == 404


def test_delete_requires_a_session(app):
    assert app.test_client().delete("/api/connections/x").status_code == 401


# --- daily usage cap (inference spend control) -------------------------------

def test_daily_turn_cap_counts_and_then_refuses(svc, monkeypatch):
    acct = _make_account(svc, monkeypatch, "cap@example.com")
    used = [svc.claim_daily_turn(acct.id, cap=3) for _ in range(3)]
    assert used == [(True, 1), (True, 2), (True, 3)]
    # Fourth is refused, and the count does not keep climbing past the cap.
    assert svc.claim_daily_turn(acct.id, cap=3) == (False, 3)
    assert svc.claim_daily_turn(acct.id, cap=3) == (False, 3)


def test_daily_cap_is_per_account(svc, monkeypatch):
    a = _make_account(svc, monkeypatch, "capa@example.com")
    b = _make_account(svc, monkeypatch, "capb@example.com")
    svc.claim_daily_turn(a.id, cap=1)
    assert svc.claim_daily_turn(a.id, cap=1)[0] is False
    # A different account is unaffected by the first one's spend.
    assert svc.claim_daily_turn(b.id, cap=1)[0] is True


def test_daily_cap_survives_a_service_restart(tmp_path, monkeypatch):
    # The whole point of moving this out of process memory: a restart must not
    # hand the account a fresh allowance. Needs a real file DB — an in-memory
    # SQLite would be a brand-new database per service instance and would pass
    # this test for the wrong reason.
    from careagents.accounts import AccountService
    db_cfg = Config(env={"CARE_DATABASE_URL": f"sqlite:///{tmp_path}/care.db",
                         "CARE_RP_ID": "localhost",
                         "CARE_ORIGIN": "http://localhost",
                         "OPENAI_API_KEY": "k",
                         "HEALTHCLAW_MINT_SECRET": "mint-secret"})
    first = AccountService(db_cfg)
    acct = _make_account(first, monkeypatch, "restart@example.com")
    assert first.claim_daily_turn(acct.id, cap=2)[0] is True
    assert first.claim_daily_turn(acct.id, cap=2)[0] is True

    reborn = AccountService(db_cfg)       # same DB file, new service instance
    assert reborn.claim_daily_turn(acct.id, cap=2) == (False, 2)


def test_chat_refuses_once_the_daily_limit_is_reached(cfg, svc, monkeypatch):
    from careagents.app import create_app
    monkeypatch.setattr(cfg, "chat_turns_per_day", 1)
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    agent = c.post("/api/agents", json={"name": "A", "persona": "calm",
                                        "connection_id": conn}).get_json()["id"]

    monkeypatch.setattr("careagents.app.run_turn",
                        lambda *a, **k: iter(["ok"]))
    first = c.post("/api/chat", json={"agent_id": agent, "message": "hi"})
    assert first.status_code == 200

    second = c.post("/api/chat", json={"agent_id": agent, "message": "again"})
    assert second.status_code == 429
    d = second.get_json()
    assert d["error"] == "daily_limit_reached" and d["limit"] == 1
    assert "resets" in d["message"].lower()   # tells the user when, not just no


# --- consent gate (real records only) ----------------------------------------

def test_real_record_connect_refused_without_consent(app, svc, monkeypatch):
    """The consent gate is server-side: skipping the card is refused."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    for payload in (None, {}, {"consent": False}, {"consent": "yes"},
                    {"consent": 1}):
        r = c.post("/api/connections/fasten", json=payload)
        assert r.status_code == 428, payload
        d = r.get_json()
        assert d["error"] == "consent_required"
        assert d["consent_version"]
    # nothing was persisted by the refused attempts
    with svc.session() as s:
        from careagents.models import Connection
        assert s.query(Connection).filter_by(kind="fasten").count() == 0


def test_sample_connect_needs_no_consent(app, svc, monkeypatch):
    """Synthetic records stay friction-free — no consent card, no record."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/sample")
    assert r.status_code == 200
    with svc.session() as s:
        from careagents.models import Connection
        conn = s.query(Connection).filter_by(kind="sample").first()
        assert conn.consented_at is None and conn.consent_version is None


def test_consent_is_recorded_with_version(app, svc, monkeypatch):
    """An agreed consent persists timestamp + the version agreed to."""
    from careagents.app import CONSENT_VERSION
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/fasten", json={"consent": True})
    assert r.status_code == 200
    with svc.session() as s:
        from careagents.models import Connection
        conn = s.query(Connection).filter_by(kind="fasten").first()
        assert conn.consent_version == CONSENT_VERSION
        assert conn.consented_at is not None


def test_catalog_marks_real_record_sources_for_consent(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    by_id = {x["id"]: x for x in
             c.get("/api/connections/catalog").get_json()["connectors"]}
    assert by_id["fasten"].get("requires_consent") is True
    assert by_id["wearable"].get("requires_consent") is True
    assert "requires_consent" not in by_id["sample"]


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


def test_auth_code_input_fits_minted_codes(app):
    # The server mints zero-padded codes of len(str(CODE_MAX - 1)) digits; the
    # input's maxlength must match or the UI truncates the code and every
    # sign-in fails (shipped broken as 6 vs 8 once — keep them locked).
    from careagents.accounts import CODE_MAX
    digits = len(str(CODE_MAX - 1))
    body = app.test_client().get("/auth").get_data(as_text=True)
    assert f'maxlength="{digits}"' in body
    assert f"{digits}-digit code" in body


def test_healthz_and_manifest(app):
    c = app.test_client()
    assert c.get("/healthz").get_json()["accounts"] is True
    m = c.get("/manifest.webmanifest").get_json()
    assert m["name"] == "CareAgents" and m["start_url"] == "/home"

# --- advisors (ported from SmartHealthConnect skills) -------------------------

def test_advisor_catalog_shape_and_honesty():
    """Available advisors have guidance; deferred ones state their blocker."""
    from careagents import advisors
    cat = {a["id"]: a for a in advisors.catalog()}
    # 4 live specialties + general; research-monitor + kids-health deferred
    for key in ("general", "healthy-habits", "care-completion",
                "medication-refills", "diet-exercise"):
        assert cat[key]["available"] is True, key
    for key in ("research-monitor", "kids-health"):
        assert cat[key]["available"] is False, key
        assert cat[key]["note"]  # honest reason, never a silent dead tile
    # guidance may only reference tools the agent loop actually has
    from careagents.agent import TOOLS
    real = {t["name"] for t in TOOLS}
    import re
    for key, a in advisors.ADVISORS.items():
        for tool in re.findall(r"\b(get_[a-z_]+|search_records|start_intake_form|check_form_status|log_[a-z_]+|request_[a-z_]+|check_[a-z_]+|track_[a-z_]+|monitor_[a-z_]+)\b",
                               a.get("guidance", "")):
            assert tool in real, f"{key} guidance references nonexistent tool {tool}"


def test_agent_with_advisor_gets_specialized_prompt(app, svc, monkeypatch):
    from careagents.personas import system_prompt
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    r = c.post("/api/agents", json={"name": "Meds", "persona": "direct",
                                    "advisor": "medication-refills",
                                    "connection_id": conn})
    assert r.status_code == 200
    aid = r.get_json()["id"]
    with svc.session() as s:
        from careagents.models import Account
        acct_id = s.query(Account).filter_by(
            email="gene@example.com").one().id
    ctx = svc.get_agent_context(acct_id, aid)
    assert ctx["agent"]["advisor"] == "medication-refills"
    sp = system_prompt(ctx["agent"]["name"], ctx["agent"]["persona"],
                       ctx["agent"]["advisor"])
    assert "medication picture" in sp            # advisor guidance present
    assert "cannot SUBMIT refill requests" in sp  # rail honesty preserved
    # persona (voice) is orthogonal: direct persona text still present
    base = system_prompt(ctx["agent"]["name"], ctx["agent"]["persona"])
    assert base in sp  # advisor only APPENDS; never rewrites the voice/safety


def test_unavailable_advisor_refused_not_downgraded(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    for adv in ("kids-health", "research-monitor"):
        r = c.post("/api/agents", json={"name": "X", "persona": "calm",
                                        "advisor": adv,
                                        "connection_id": conn})
        assert r.status_code == 400, adv
        assert r.get_json()["error"] == "advisor_not_available"
        assert r.get_json()["note"]
    r = c.post("/api/agents", json={"name": "X", "persona": "calm",
                                    "advisor": "nonsense",
                                    "connection_id": conn})
    assert r.status_code == 400
    # no advisor at all still works (general agent)
    r = c.post("/api/agents", json={"name": "Plain", "persona": "calm",
                                    "connection_id": conn})
    assert r.status_code == 200
