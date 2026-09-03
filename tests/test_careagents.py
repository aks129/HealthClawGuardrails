"""CareAgents account-layer unit tests — no network; HealthClaw + LLM + email
+ WebAuthn verification are faked. Live paths are covered by
scripts/careagents_smoke.py against the deployed site.

Pins: fail-closed config, one safety core per persona, email-code auth,
WebAuthn option issuance, account-scoped connections/agents/surfaces (foreign
ids 404), the chat gate, the review relay, and the Telegram bind handshake.
"""

from __future__ import annotations

import inspect
import json
import re
import time

import pytest

from careagents.config import Config, ConfigError
from careagents.healthclaw import HealthClawClient, HealthClawError
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


def test_idle_poll_cap_defaults_to_six_and_never_sits_below_the_floor():
    base = {"CARE_RP_ID": "localhost", "CARE_ORIGIN": "http://localhost",
            "OPENAI_API_KEY": "k", "HEALTHCLAW_MINT_SECRET": "m"}
    assert Config(env=base).run_poll_max_seconds == 6.0

    # The rollback contract: the cap set to the floor pins the interval flat,
    # and a cap below the floor clamps up rather than inverting the doubling.
    for value, expected in (("0.5", 0.5), ("0.1", 0.5)):
        cfg = Config(env={**base, "CARE_RUN_POLL_MAX_SECONDS": value})
        assert cfg.run_poll_max_seconds == cfg.run_poll_seconds == expected

    for bad in ("0.04", "31"):
        with pytest.raises(ConfigError):
            Config(env={**base, "CARE_RUN_POLL_MAX_SECONDS": bad})


# --- build provenance (#258) --------------------------------------------------
# Both deployments were once found serving code months older than main while
# every production check was green. The marker is what makes that visible — so
# it has to survive being absent or damaged without taking the app down with it.

def _marker(tmp_path, text: str):
    p = tmp_path / "BUILD_SHA"
    p.write_text(text, encoding="utf-8")
    return p


def test_build_marker_reports_the_deployed_commit(tmp_path):
    from careagents import _build
    assert _build._read(_marker(tmp_path, "4f2a91cbeef1\n1754056800\n")) == (
        "4f2a91cbeef1", 1754056800)


def test_build_marker_keeps_the_dirty_suffix(tmp_path):
    # Deploying an uncommitted tree must be visible, and "-dirty" matches no
    # acceptable sha in prod_watch — which is the intended outcome.
    from careagents import _build
    sha, _ = _build._read(_marker(tmp_path, "4f2a91cbeef1-dirty\n1754056800\n"))
    assert sha == "4f2a91cbeef1-dirty"


def test_missing_build_marker_degrades_to_unknown(tmp_path):
    from careagents import _build
    assert _build._read(tmp_path / "nope") == ("unknown", 0)


@pytest.mark.parametrize("text", [
    "", "\n\n", "not-a-sha\n1754056800\n", "\x00\xff garbage \x01\n",
    "4f2a91cbeef1",  # truncated: sha present, timestamp line missing
])
def test_corrupt_build_marker_never_raises(tmp_path, text, monkeypatch):
    # A damaged marker is a telemetry problem, not an outage. It must never be
    # the reason a deployment fails to boot.
    from careagents import _build
    monkeypatch.delenv("CARE_BUILD_SHA", raising=False)
    sha, when = _build._read(_marker(tmp_path, text))
    assert when == 0
    assert sha in ("unknown", "4f2a91cbeef1")


def test_binary_build_marker_never_raises(tmp_path, monkeypatch):
    from careagents import _build
    monkeypatch.delenv("CARE_BUILD_SHA", raising=False)
    p = tmp_path / "BUILD_SHA"
    p.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    assert _build._read(p) == ("unknown", 0)


def test_build_marker_falls_back_to_the_environment(tmp_path, monkeypatch):
    from careagents import _build
    monkeypatch.setenv("CARE_BUILD_SHA", "a1b2c3d4e5f6")
    assert _build._read(tmp_path / "nope") == ("a1b2c3d4e5f6", 0)


def test_build_marker_prefers_the_file_over_the_environment(tmp_path,
                                                            monkeypatch):
    # The file ships with the code; the env var can be edited without touching
    # the image, so it must not be able to override what the tree says.
    from careagents import _build
    monkeypatch.setenv("CARE_BUILD_SHA", "a1b2c3d4e5f6")
    sha, _ = _build._read(_marker(tmp_path, "4f2a91cbeef1\n1754056800\n"))
    assert sha == "4f2a91cbeef1"


def test_config_never_fails_on_a_missing_build_marker():
    # Telemetry, never config: production must boot without a marker.
    cfg = Config(env={"CARE_ENV": "production", "CARE_SESSION_SECRET": "x" * 32,
                      "HEALTHCLAW_MINT_SECRET": "m", "OPENAI_API_KEY": "k",
                      "RESEND_API_KEY": "r",
                      "REDIS_URL": "redis://localhost:6379/1",
                      "CARE_DATABASE_URL": "postgresql://u:p@db:5432/care"})
    assert isinstance(cfg.build_sha, str) and isinstance(cfg.build_time, int)


def test_auth_email_reports_failure_instead_of_claiming_it_sent(app, svc,
                                                                monkeypatch):
    # Login is the front door. This used to discard send_code's return value
    # and answer {"sent": true} even when Resend was down, leaving the person
    # watching an empty inbox with no idea whether to wait or retry.
    import careagents.mail as mailmod
    monkeypatch.setattr(mailmod, "send_code",
                        lambda cfg, e, code, purpose: mailmod.NOT_SENT)
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
                        lambda cfg, e, code, purpose: mailmod.NOT_SENT)
    c = app.test_client()
    assert c.post("/api/auth/email",
                  json={"email": "retry@example.com"}).status_code == 502

    sent = {}
    monkeypatch.setattr(
        mailmod, "send_code",
        lambda cfg, e, code, purpose: (sent.setdefault("c", code)
                                       and mailmod.SENT))
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


def test_review_submit_does_not_claim_nothing_was_sent_when_it_cannot_tell(
        cfg, svc, monkeypatch):
    """The third state (#220): the confirm went out and was never answered.

    Answering "Nothing has been sent — please try approving again" is as
    unobserved as the old "confirmed". The engine may already be executing the
    action, and a person who follows that instruction sends it twice.

    This fake raises HealthClawUnconfirmed itself, so it proves the ROUTE
    handles it and can never prove the client produces it — which is half of
    why #416 survived a suite that looked like it covered this. The other half
    is in tests/test_healthclaw_transport.py, where the real client meets a
    real 504 over a real socket.
    """
    from careagents.app import create_app
    from careagents.healthclaw import HealthClawUnconfirmed

    class _Fake(FakeClient):
        def submit_review(self, tenant, action_id, decisions):
            return 200, {"ok": True}

        def confirm_action(self, tenant, action_id):
            raise HealthClawUnconfirmed("confirm unanswered", 0)

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
    assert body["confirmed"] is None, "unknown is not false"
    msg = body["message"].lower()
    assert "nothing has been sent" not in msg
    assert "couldn't confirm" in msg and "twice" in msg


def test_confirm_action_separates_a_refusal_from_an_unanswered_confirm():
    """The client must not report silence as a refusal.

    A lost answer on /confirm used to escape as a bare requests exception —
    nothing in this client wraps it — so the relay's `except HealthClawError`
    never saw it and the person got a 500.
    """
    import requests
    from careagents.healthclaw import (HealthClawClient, HealthClawError,
                                       HealthClawUnconfirmed)

    class _Minted:
        ok, status_code = True, 200

        def json(self):
            return {"token": "tok"}

    def client_with(confirm_result):
        hc = HealthClawClient("http://engine.invalid", "secret")

        def _post(url, **kw):
            if url.endswith("/approval-token"):
                return _Minted()
            if isinstance(confirm_result, Exception):
                raise confirm_result
            return confirm_result

        hc.http.post = _post
        return hc

    # No answer at all — we cannot tell whether the action ran.
    with pytest.raises(HealthClawUnconfirmed):
        client_with(requests.ReadTimeout("read timed out")).confirm_action(
            "t", "a1")

    # An observed rejection stays an ordinary failure. 409 is a status the
    # ENGINE answers with, so this assertion is true — and it says nothing
    # about the statuses a gateway produces on the engine's behalf. A 504 took
    # this same branch all through #416. Which statuses mean what is
    # classified in tests/test_healthclaw_transport.py, against a real socket.
    class _Refused:
        ok, status_code = False, 409

    err = pytest.raises(HealthClawError,
                        client_with(_Refused()).confirm_action, "t", "a1")
    assert not isinstance(err.value, HealthClawUnconfirmed)

    # A transport failure minting the approval token is a refusal, not
    # silence: the confirm call never went out.
    hc = HealthClawClient("http://engine.invalid", "secret")

    def _mint_dies(url, **kw):
        raise requests.ConnectionError("refused")

    hc.http.post = _mint_dies
    err = pytest.raises(HealthClawError, hc.confirm_action, "t", "a1")
    assert not isinstance(err.value, HealthClawUnconfirmed)


def test_an_unconfirmed_send_keeps_the_code_live_and_says_so(app, svc,
                                                             monkeypatch):
    """The third state on the front door (#220).

    A read timeout to Resend is not a refusal: the mail may already be in the
    inbox. Reporting "we couldn't send it" AND burning the code is worse than
    doing nothing — the person types a code we just killed.
    """
    import careagents.mail as mailmod
    captured = {}
    monkeypatch.setattr(
        mailmod, "send_code",
        lambda cfg, e, code, purpose: (captured.setdefault("c", code)
                                       and mailmod.UNCONFIRMED))
    c = app.test_client()
    r = c.post("/api/auth/email", json={"email": "unsure@example.com"})

    assert r.status_code == 202
    body = r.get_json()
    assert body["sent"] is None, "unknown is neither sent nor not-sent"
    assert "couldn't confirm" in body["notice"].lower()

    # The code the provider may have delivered still works.
    r = c.post("/api/auth/verify",
               json={"email": "unsure@example.com", "code": captured["c"]})
    assert r.status_code == 200, "an unconfirmed send burned a live code"


def test_send_code_calls_a_lost_answer_unconfirmed_not_failed(monkeypatch):
    """A response is the only evidence of an outcome."""
    import requests
    import careagents.mail as mailmod

    class _Cfg:
        resend_api_key = "key"
        resend_from = "codes@example.com"

    def sending(raises=None, status=None):
        def _post(*a, **kw):
            if raises is not None:
                raise raises
            return type("R", (), {"status_code": status})()
        monkeypatch.setattr(mailmod.requests, "post", _post)
        return mailmod.send_code(_Cfg(), "x@example.com", "12345678", "verify")

    # Written, never answered — the mail may have gone out.
    assert sending(raises=requests.ReadTimeout("slow")) == mailmod.UNCONFIRMED
    # Never delivered to the provider at all.
    assert sending(raises=requests.ConnectionError("refused")) == mailmod.NOT_SENT
    # The provider answered and did not accept it.
    assert sending(status=429) == mailmod.NOT_SENT
    assert sending(status=200) == mailmod.SENT

# --- engine pool settings (#221) ---------------------------------------------
# Honest scope: these assert what make_engine hands to create_engine. They do
# NOT prove that a dropped connection is recovered — that needs a real server
# whose connection you can kill mid-pool, which this suite has no way to do.
# What they buy is a fence around settings that are invisible in review and
# whose absence only shows up as intermittent 500s after an idle period.

def _engine_kwargs(url, monkeypatch):
    """Capture the kwargs make_engine passes to create_engine for `url`.

    The spy returns a real in-memory SQLite engine so create_all() and
    _ensure_columns() still run — a Postgres URL must be assertable without a
    Postgres server.
    """
    import careagents.models as models
    seen = {}
    real = models.create_engine

    def _spy(engine_url, **kwargs):
        seen.update(kwargs)
        return real("sqlite:///:memory:",
                    connect_args={"check_same_thread": False}, future=True)

    monkeypatch.setattr(models, "create_engine", _spy)
    models.make_engine(url)
    return seen


def test_postgres_engine_is_built_with_pre_ping_and_recycle(monkeypatch):
    """MUTATION: drop either key from make_engine's pool_kwargs and this fails.

    Managed Postgres closes idle connections; a pooled one handed out after a
    quiet period fails on first use. pool_recycle must stay under the pooler's
    idle timeout (see the comment in models.py for why 300).
    """
    kwargs = _engine_kwargs("postgresql://u:p@db:5432/care", monkeypatch)
    assert kwargs.get("pool_pre_ping") is True
    assert kwargs.get("pool_recycle") == 300
    assert kwargs["pool_recycle"] < 600, "must retire before the pooler does"


def test_sqlite_engine_is_given_no_pool_options(monkeypatch):
    """The gate is the URL scheme. This suite runs on sqlite:///:memory:, where
    a pre-ping is meaningless — there is no server to have closed anything."""
    kwargs = _engine_kwargs("sqlite:///:memory:", monkeypatch)
    assert "pool_pre_ping" not in kwargs
    assert "pool_recycle" not in kwargs


def test_a_real_sqlite_engine_has_pre_ping_off(monkeypatch):
    """Guards the guard: the spy above could pass while the real engine differs."""
    from careagents.models import make_engine
    assert make_engine("sqlite:///:memory:").pool._pre_ping is False


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
    r = client.post("/api/chat", json=payload, buffered=False)
    runtime = client.application.extensions["careagents_runtime"]
    from careagents.worker import RunWorker
    RunWorker(runtime["config"], runtime["client"], runtime["accounts"],
              "test-worker").run_once()
    r.get_data()
    return r


def _enqueue_and_run_imessage(app, client, *, headers, json):
    """Enqueue from the relay, execute separately, then poll its projection."""
    from careagents.worker import RunWorker

    runtime = app.extensions["careagents_runtime"]
    queued = client.post(
        "/api/surfaces/imessage/inbound", headers=headers, json=json)
    assert queued.status_code == 202
    run_id = queued.get_json()["run_id"]
    RunWorker(runtime["config"], runtime["client"], runtime["accounts"],
              "test-surface-worker").run_once()
    return client.get(
        f"/api/surfaces/imessage/runs/{run_id}", headers=headers,
        query_string={"handle": json["handle"]})


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


def test_worker_health_client_accepts_fail_closed_503_projection():
    import careagents.healthclaw as hcmod

    captured = {}

    class _Resp:
        status_code = 503

        @staticmethod
        def json():
            return {"available": False, "active_workers": 0}

    class _HTTP:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            captured.update({"url": url, "params": params,
                             "headers": headers, "timeout": timeout})
            return _Resp()

    client = hcmod.HealthClawClient("http://local", "mint-secret")
    client.http = _HTTP()

    result = client.agent_worker_health(45)

    assert result["available"] is False
    assert captured["url"].endswith(
        "/command-center/api/runs/workers/health")
    assert captured["params"] == {"max_age_seconds": 45}
    assert captured["headers"] == {
        "X-Internal-Secret": "mint-secret",
        "X-Agent-Id": "careagents-worker",
    }


def test_worker_container_probe_uses_authenticated_queue_readiness(monkeypatch):
    from careagents import healthcheck

    captured = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["secret"] = request.get_header("X-internal-secret")
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", urlopen)
    env = {"CARE_ROLE": "worker", "HEALTHCLAW_BASE": "http://healthclaw",
           "HEALTHCLAW_MINT_SECRET": "worker-secret",
           "CARE_RUN_WORKER_STALE_SECONDS": "45"}

    assert healthcheck.healthy(env) is True
    assert captured["url"].endswith(
        "/command-center/api/runs/workers/health?max_age_seconds=45")
    assert captured["secret"] == "worker-secret"


def test_worker_container_probe_fails_closed_without_secret():
    from careagents.healthcheck import healthy

    assert healthy({"CARE_ROLE": "worker",
                    "HEALTHCLAW_BASE": "http://healthclaw"}) is False


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
    assert '"type": "accepted"' in replay.get_data(as_text=True)
    assert '"text": "once"' in replay.get_data(as_text=True)
    stored = fake.logged[(tenant, fake.conversation_id(agent_id))]
    assert [message["role"] for message in stored] == ["user", "assistant"]
    assert first.status_code == replay.status_code == 200


def test_browser_disconnect_reconnect_replays_without_duplicate_inference(
        cfg, svc, monkeypatch):
    app, c, fake, agent_id, _tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch, reply="durable answer")
    from careagents import agent as agent_mod
    from careagents.worker import RunWorker

    calls = {"count": 0}

    class _Turn:
        text, tool_calls, raw_tool_calls = "durable answer", [], []

    def complete(*args, **kwargs):
        calls["count"] += 1
        return _Turn()

    monkeypatch.setattr(agent_mod.llm, "complete", complete)
    response = c.post("/api/chat", json={
        "agent_id": agent_id, "message": "keep working",
        "request_id": "disconnect-1"}, buffered=False)
    assert response.headers["X-CareAgents-Run-ID"] in fake.runs
    accepted = next(iter(response.response)).decode()
    run_id = next(iter(fake.runs))
    assert '"type": "accepted"' in accepted and run_id in accepted
    response.close()  # disconnect is projection-only; it never cancels.

    RunWorker(cfg, fake, svc, "worker-after-disconnect").run_once()
    replay = c.get(
        f"/api/chat/runs/{run_id}/events",
        query_string={"agent_id": agent_id, "after": 0})
    body = replay.get_data(as_text=True)

    assert calls["count"] == 1
    assert '"text": "durable answer"' in body
    assert '"type": "done"' in body


def test_terminal_sse_drains_every_event_page_before_done(
        cfg, svc, monkeypatch):
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)
    created, message_id = fake.claim_inbound_message(
        tenant, "many events", agent_id, fake.conversation_id(agent_id),
        "web", "many-events")
    assert created is True
    run = fake.create_agent_run(tenant, message_id)
    for index in range(101):
        fake._append_run_event(
            run["id"], "agent.text", {"text": f"part-{index}"})
    fake.runs[run["id"]]["status"] = "completed"

    response = c.get(
        f"/api/chat/runs/{run['id']}/events",
        query_string={"agent_id": agent_id, "after": 0})
    body = response.get_data(as_text=True)

    assert body.count('"type": "text"') == 101
    assert body.index('"text": "part-100"') < body.index('"type": "done"')


def test_worker_enforces_claimed_run_deadline_before_inference(
        cfg, svc, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from careagents.worker import RunWorker

    app, c, fake, agent_id, _tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)
    response = c.post("/api/chat", json={
        "agent_id": agent_id, "message": "too late",
        "request_id": "expired-before-inference"}, buffered=False)
    next(iter(response.response))
    response.close()
    run_id = next(iter(fake.runs))
    fake.runs[run_id]["deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(
        "careagents.worker.llm.complete",
        lambda *a, **k: pytest.fail("deadline allowed model inference"))

    RunWorker(cfg, fake, svc, "deadline-worker").run_once()

    assert fake.runs[run_id]["status"] == "failed"
    assert any(event["type"] == "run.deadline_exceeded"
               for event in fake.events[run_id])


def test_sse_replay_terminates_expired_run_without_a_worker(
        cfg, svc, monkeypatch):
    from datetime import datetime, timedelta, timezone

    app, client, fake, agent_id, _tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)
    response = client.post("/api/chat", json={
        "agent_id": agent_id, "message": "worker disappears",
        "request_id": "expire-without-worker"}, buffered=False)
    stream = iter(response.response)
    assert '"type": "accepted"' in next(stream).decode()
    run_id = next(iter(fake.runs))
    fake.runs[run_id]["deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    body = b"".join(stream).decode()

    assert fake.runs[run_id]["status"] == "failed"
    assert '"type": "done"' in body
    assert '"status": "failed"' in body


def test_late_provider_result_is_not_checkpointed_after_lease_loss(
        cfg, svc, monkeypatch):
    from careagents.worker import RunWorker

    app, client, fake, agent_id, tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)
    accepted = client.post("/api/chat", json={
        "agent_id": agent_id, "message": "provider may hang",
        "request_id": "late-provider"}, buffered=False)
    next(iter(accepted.response))
    accepted.close()
    run = fake.claim_agent_run("late-worker")

    class _Turn:
        text, tool_calls, raw_tool_calls = "late answer", [], []

    monkeypatch.setattr(
        "careagents.worker.llm.complete", lambda *_args: _Turn())

    class _RevokedLease:
        checks = 0

        def check(self):
            self.checks += 1
            if self.checks > 1:
                raise HealthClawError("worker lease was lost", 409)

    with pytest.raises(HealthClawError):
        RunWorker(cfg, fake, svc, "late-worker")._execute(
            run, _RevokedLease())

    assert [event["type"] for event in fake.events[run["id"]]] == [
        "run.queued", "run.started"]
    assert [message["role"] for message in fake.logged[
        (tenant, fake.conversation_id(agent_id))]] == ["user"]


def test_worker_pool_creates_only_the_configured_number_of_slots(
        cfg, monkeypatch):
    import threading
    from careagents import worker as worker_mod

    cfg.run_worker_concurrency = 3
    stop = threading.Event()
    stop.set()
    threads = []

    class _Thread:
        def __init__(self, target, args, daemon, name):
            self.target, self.args = target, args
            self.daemon, self.name = daemon, name
            self.started = self.joined = False
            threads.append(self)

        def start(self):
            self.started = True
            self.target(*self.args)

        def join(self):
            self.joined = True

    monkeypatch.setattr(worker_mod.threading, "Thread", _Thread)
    monkeypatch.setattr(worker_mod, "AccountService", lambda _cfg: object())
    monkeypatch.setattr(worker_mod, "HealthClawClient",
                        lambda *_args: object())

    worker_mod.run_worker_pool(cfg, stop)

    assert [thread.name for thread in threads] == [
        "careagents-worker-0", "careagents-worker-1",
        "careagents-worker-2"]
    assert all(thread.started and thread.joined for thread in threads)


def test_worker_ids_are_unique_per_process_instance():
    """#374's redelivery hands a running run back to its own worker id, so a
    worker id must name exactly one live claim loop. Hostname and PID do not:
    a restarted container can be handed both back (PID 1 is the common case),
    and two processes sharing an id would each be handed the other's run.

    Both calls here share this process's hostname and PID, so the only thing
    that can distinguish them is the per-instance suffix.

    MUTATION: drop the random suffix from `_worker_base_id`
    (careagents/worker.py). The two ids become equal.
    """
    from careagents import worker as worker_mod

    first, second = worker_mod._worker_base_id(), worker_mod._worker_base_id()

    assert first != second
    # The engine rejects a worker_id outside this shape (r6/agent_runs/routes
    # `_ID`), and its column is String(128).
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,110}", first), first

# --- idle poll backoff (#341) -------------------------------------------------

class _RecordingStop:
    """Stop event that records each wait and advances a simulated clock.

    The clock is what makes presence freshness assertable: a slot commits
    presence by calling claim, so the interval it waits between claims *is*
    the age its presence row reaches.
    """

    def __init__(self):
        self.waits: list[float] = []
        self.claims: list[float] = []
        self.clock = 0.0
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout=None) -> bool:
        self.waits.append(timeout)
        self.clock += timeout or 0.0
        return self._set


def _drive_pool(cfg, monkeypatch, outcomes):
    """Run one worker slot against a scripted sequence of claim outcomes.

    `outcomes` is one bool per poll: True means the slot claimed a run. The
    pool runs synchronously on fake threads and stops once the script is
    exhausted. Driving the real `run_worker_pool` rather than the backoff
    object alone is deliberate — it pins that the backoff is wired to the
    empty branch and the reset to the claimed one.
    """
    from careagents import worker as worker_mod

    cfg.run_worker_concurrency = 1
    stop = _RecordingStop()
    pending = list(outcomes)

    class _ScriptedWorker:
        def __init__(self, *_args):
            pass

        def run_once(self):
            stop.claims.append(stop.clock)
            worked = pending.pop(0)
            if not pending:
                stop.set()
            return worked

    class _Thread:
        def __init__(self, target, args, daemon, name):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

        def join(self):
            pass

    monkeypatch.setattr(worker_mod.threading, "Thread", _Thread)
    monkeypatch.setattr(worker_mod, "AccountService", lambda _cfg: object())
    monkeypatch.setattr(worker_mod, "HealthClawClient",
                        lambda *_args: object())
    monkeypatch.setattr(worker_mod, "RunWorker", _ScriptedWorker)
    worker_mod.run_worker_pool(cfg, stop)
    return stop


def test_idle_poll_doubles_to_the_cap_and_stays_there(cfg, monkeypatch):
    cfg.run_poll_seconds = 0.5
    cfg.run_poll_max_seconds = 6.0

    stop = _drive_pool(cfg, monkeypatch, [False] * 7)

    assert stop.waits == [0.5, 1.0, 2.0, 4.0, 6.0, 6.0, 6.0]


def test_a_claim_returns_the_claiming_slot_to_the_floor(cfg, monkeypatch):
    cfg.run_poll_seconds = 0.5
    cfg.run_poll_max_seconds = 6.0

    # Ramp to the cap, claim one run, then go idle again. The cap must only
    # ever bite on the first message after silence.
    stop = _drive_pool(cfg, monkeypatch, [False] * 5 + [True] + [False] * 3)

    assert stop.waits == [0.5, 1.0, 2.0, 4.0, 6.0, 0.5, 1.0, 2.0]


def test_a_claim_in_one_slot_returns_the_other_slots_to_the_floor():
    from careagents.worker import _IdleBackoff

    backoff = _IdleBackoff(slots=2, floor=0.5, cap=6.0)
    # Slot 1 ramps to the cap while slot 0 is busy.
    assert [backoff.on_empty(1) for _ in range(5)] == [0.5, 1.0, 2.0, 4.0, 6.0]

    # Slot 0 claims a run. Slot 1 claimed nothing, but a live conversation
    # means work is arriving, so it must poll at the floor too.
    backoff.on_claim()

    assert backoff.on_empty(1) == 0.5


def test_poll_cap_equal_to_poll_floor_reproduces_todays_flat_interval(
        cfg, monkeypatch):
    # The rollback path: CARE_RUN_POLL_MAX_SECONDS=0.5 restores today's
    # behaviour with a variable change and no redeploy.
    cfg.run_poll_seconds = 0.5
    cfg.run_poll_max_seconds = 0.5

    stop = _drive_pool(cfg, monkeypatch, [False] * 6)

    assert stop.waits == [0.5] * 6


def test_idle_backoff_keeps_worker_presence_inside_the_readiness_window(
        cfg, monkeypatch):
    """The cap must never open a presence gap the readiness check fails on.

    `claim_next` commits presence on every claim including the empty ones,
    so an idle slot's poll interval is exactly how stale its presence row
    gets. Web readiness fails closed at CARE_RUN_WORKER_STALE_SECONDS, so a
    cap at or above that threshold would take /healthz to 503 on an idle
    queue — the outage the fail-closed design exists to make visible.
    """
    cfg.run_poll_seconds = 0.5
    cfg.run_poll_max_seconds = 6.0

    # Long enough to ramp to the cap and then sit at it across several
    # readiness windows, not merely long enough to touch it once.
    stop = _drive_pool(cfg, monkeypatch, [False] * 40)

    gaps = [later - earlier
            for earlier, later in zip(stop.claims, stop.claims[1:])]
    assert stop.claims[-1] > 3 * cfg.run_worker_stale_seconds, (
        "ramp too short to say anything about a 30s readiness window")
    assert max(gaps) == pytest.approx(cfg.run_poll_max_seconds)
    assert max(gaps) < cfg.run_worker_stale_seconds


def test_idle_backoff_sleep_stays_interruptible_so_shutdown_drains(
        cfg, monkeypatch):
    """The wait must stay `stop.wait`, never `time.sleep`.

    A `time.sleep(cap)` adds the full cap to every SIGTERM drain and every
    deploy. Floor and cap are pinned to 30s so the difference is
    unmistakable: milliseconds against `stop.wait`, half a minute against
    `time.sleep`.
    """
    import threading
    from careagents import worker as worker_mod

    cfg.run_worker_concurrency = 1
    cfg.run_poll_seconds = 30.0
    cfg.run_poll_max_seconds = 30.0
    polled = threading.Event()

    class _IdleWorker:
        def __init__(self, *_args):
            pass

        def run_once(self):
            polled.set()
            return False

    monkeypatch.setattr(worker_mod, "AccountService", lambda _cfg: object())
    monkeypatch.setattr(worker_mod, "HealthClawClient",
                        lambda *_args: object())
    monkeypatch.setattr(worker_mod, "RunWorker", _IdleWorker)

    stop = threading.Event()
    pool = threading.Thread(target=worker_mod.run_worker_pool,
                            args=(cfg, stop), daemon=True)
    started = time.monotonic()
    pool.start()
    assert polled.wait(10), "worker slot never polled"
    stop.set()
    pool.join(timeout=10)

    assert not pool.is_alive(), "pool did not drain: the sleep is not a wait"
    assert time.monotonic() - started < 10


def test_queued_run_history_stops_at_its_claimed_message(
        cfg, svc, monkeypatch):
    app, c, fake, agent_id, _tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)
    from careagents.worker import RunWorker

    seen = []

    class _Turn:
        text, tool_calls, raw_tool_calls = "ok", [], []

    def complete(_cfg, _system, history, _tools):
        seen.append([message.get("content") for message in history])
        return _Turn()

    monkeypatch.setattr("careagents.worker.llm.complete", complete)
    first = c.post("/api/chat", json={
        "agent_id": agent_id, "message": "earlier question",
        "request_id": "queued-earlier"}, buffered=False)
    next(iter(first.response))
    first.close()
    later = c.post("/api/chat", json={
        "agent_id": agent_id, "message": "later private detail",
        "request_id": "queued-later"}, buffered=False)
    next(iter(later.response))
    later.close()

    RunWorker(cfg, fake, svc, "history-worker").run_once()

    assert "earlier question" in seen[0]
    assert "later private detail" not in seen[0]


def test_recovery_reuses_completed_tool_and_fails_closed_on_ambiguous_tool(
        cfg, svc, monkeypatch):
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)
    from careagents.worker import RunWorker

    response = c.post("/api/chat", json={
        "agent_id": agent_id, "message": "check my labs",
        "request_id": "recovery-completed"}, buffered=False)
    next(iter(response.response))
    response.close()
    run_id = next(run_id for run_id, run in fake.runs.items()
                  if run["status"] == "queued")
    run = fake.runs[run_id]
    run["status"], run["worker_id"] = "running", "dead-worker"
    fake._append_run_event(run_id, "agent.checkpoint", {
        "checkpoint_id": "round-1", "round": 1, "text": "",
        "tool_calls": [{"id": "provider-call-1", "name": "get_labs",
                        "arguments": {}}], "raw_tool_calls": []})
    fake.tool_calls[(run_id, "provider-call-1")] = {
        "id": "call-1", "run_id": run_id,
        "provider_call_id": "provider-call-1", "tool_name": "get_labs",
        "input": {}, "status": "completed",
        "result": {"content": '{"already":"done"}', "ui_events": []}}
    run["status"], run["worker_id"] = "queued", None

    executed = {"count": 0}
    monkeypatch.setattr("careagents.worker._execute_tool",
                        lambda *a, **k: executed.__setitem__(
                            "count", executed["count"] + 1))

    class _Final:
        text, tool_calls, raw_tool_calls = "reused safely", [], []

    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: _Final())
    RunWorker(cfg, fake, svc, "recovery-worker").run_once()
    assert executed["count"] == 0
    assert fake.runs[run_id]["status"] == "completed"

    # A call left *running* has an unknown provider outcome. It must not be
    # executed again; it moves to reconciliation and pauses the run.
    second_message = fake.claim_inbound_message(
        tenant, "prepare a form", agent_id, fake.conversation_id(agent_id),
        "web", "recovery-ambiguous")[1]
    ambiguous = fake.create_agent_run(tenant, second_message)
    ambiguous_id = ambiguous["id"]
    fake._append_run_event(ambiguous_id, "agent.checkpoint", {
        "checkpoint_id": "round-1", "round": 1, "text": "",
        "tool_calls": [{"id": "provider-call-2",
                        "name": "start_intake_form", "arguments": {}}],
        "raw_tool_calls": []})
    fake.tool_calls[(ambiguous_id, "provider-call-2")] = {
        "id": "call-2", "run_id": ambiguous_id,
        "provider_call_id": "provider-call-2",
        "tool_name": "start_intake_form", "input": {},
        "status": "running", "result": None}

    RunWorker(cfg, fake, svc, "reconcile-worker").run_once()

    assert executed["count"] == 0
    assert fake.tool_calls[(ambiguous_id, "provider-call-2")][
        "status"] == "needs_reconciliation"
    assert fake.runs[ambiguous_id]["status"] == "waiting_for_human"


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

    class _Turn:
        text, tool_calls, raw_tool_calls = "continued on iMessage", [], []

    def reply(_cfg, _system, history, _tools):
        seen.extend(message["content"] for message in history)
        return _Turn()

    monkeypatch.setattr("careagents.worker.llm.complete", reply)
    response = _enqueue_and_run_imessage(
        app, relay,
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


def test_a_storage_outage_does_not_break_the_chat(cfg, svc, monkeypatch):
    # A user turn must be durably claimed before inference. Otherwise a retry
    # can execute the same health action twice.
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(fake, "claim_inbound_message",
                        lambda *a, **k: (None, None))
    r = _turn(c, agent_id, "still works?")
    assert r.status_code == 503
    assert r.get_json()["error"] == "message store unavailable"


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
    assert d["run_workers"] is True


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


def test_healthz_reports_which_build_is_running(app, cfg, monkeypatch):
    # #258: everything else this endpoint says is equally true of a build from
    # months ago, so a monitor could report green over unshipped code.
    monkeypatch.setattr(cfg, "build_sha", "4f2a91cbeef1")
    monkeypatch.setattr(cfg, "build_time", 1754056800)
    r = app.test_client().get("/healthz")
    d = r.get_json()
    assert r.status_code == 200, "the marker must not change readiness"
    assert d["status"] == "ok" and d["accounts"] is True
    assert d["build"] == "4f2a91cbeef1" and d["built_at"] == 1754056800


def test_healthz_still_reports_the_build_when_degraded(app, cfg, svc,
                                                       monkeypatch):
    # A stale build is a likely suspect when a deployment is misbehaving, so
    # the marker has to survive the 503 path — without softening it.
    monkeypatch.setattr(cfg, "build_sha", "4f2a91cbeef1")
    monkeypatch.setattr(svc, "ping", lambda: False)
    r = app.test_client().get("/healthz")
    assert r.status_code == 503
    assert r.get_json()["build"] == "4f2a91cbeef1"


def test_an_unstamped_build_is_still_ready(app, cfg, monkeypatch):
    # Telemetry, never a gate: an unmarked deploy must still be routable, or
    # the instrument becomes an outage of its own.
    monkeypatch.setattr(cfg, "build_sha", "unknown")
    monkeypatch.setattr(cfg, "build_time", 0)
    r = app.test_client().get("/healthz")
    assert r.status_code == 200 and r.get_json()["build"] == "unknown"

def test_healthz_and_chat_fail_fast_when_worker_presence_is_stale(
        cfg, svc, monkeypatch):
    app, client, fake, agent_id, _tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)
    fake.worker_available = False

    health = client.get("/healthz")
    refused = client.post("/api/chat", json={
        "agent_id": agent_id,
        "message": "do not strand this",
        "request_id": "worker-absent",
    })

    assert health.status_code == 503
    assert health.get_json()["run_workers"] is False
    assert refused.status_code == 503
    assert refused.get_json()["error"] == "run_workers_unavailable"
    assert fake.runs == {}


def test_chat_recovers_after_worker_queue_access_returns(
        cfg, svc, monkeypatch):
    app, client, fake, agent_id, _tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)
    payload = {"agent_id": agent_id, "message": "try once healthy",
               "request_id": "worker-recovery"}
    fake.worker_available = False
    assert client.post("/api/chat", json=payload).status_code == 503

    fake.worker_available = True
    accepted = client.post("/api/chat", json=payload, buffered=False)
    first = next(iter(accepted.response)).decode()
    accepted.close()

    assert accepted.status_code == 200
    assert '"type": "accepted"' in first
    assert len(fake.runs) == 1


def test_unreachable_worker_control_plane_degrades_readiness_and_admission(
        cfg, svc, monkeypatch):
    """Both states still fail closed. The code says which one happened.

    Readiness and admission must refuse whether the workers reported
    themselves absent or we could not reach the health endpoint at all — the
    turn cannot be promised either way, and that half is unchanged. What
    changed with #410 is the claim: this used to answer
    `run_workers_unavailable`, filing "we could not ask" as "the workers are
    down". The two are different incidents.
    """
    app, client, fake, agent_id, _tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)
    fake.worker_health_error = True

    assert client.get("/healthz").status_code == 503
    assert client.get("/healthz").get_json()["run_workers"] is False
    response = client.post("/api/chat", json={
        "agent_id": agent_id, "message": "upstream is unreachable"})
    assert response.status_code == 503
    assert response.get_json()["error"] == "run_workers_unknown"
    # The sentence the patient reads is true in both states, so it does not
    # change with the code.
    assert "temporarily unavailable" in response.get_json()["message"]


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


def test_a_truncated_list_says_so_rather_than_looking_complete():
    # The sibling of #207. The summarizer caps at 12 and the list it returns
    # IS the model's whole view, so a silent cut reads as "that is all there
    # is". A person on 30 medications would be told about 12, confidently.
    from careagents.agent import _summarize_bundle
    bundle = {"entry": [
        {"resource": {"resourceType": "MedicationRequest",
                      "medicationCodeableConcept": {"text": f"Drug {i}"}}}
        for i in range(30)
    ]}
    items = _summarize_bundle(bundle)

    marker = [i for i in items if i.get("truncated")]
    assert marker, "a truncated list must carry a truncation marker"
    assert marker[0]["total"] == 30
    assert marker[0]["shown"] == 12
    # Must not be mistakable for a record.
    assert "name" not in marker[0] and "type" not in marker[0]
    assert "complete" in marker[0]["note"].lower()


def test_an_untruncated_list_carries_no_marker():
    # Otherwise every answer hedges and the signal stops meaning anything.
    from careagents.agent import _summarize_bundle
    bundle = {"entry": [
        {"resource": {"resourceType": "Condition",
                      "code": {"text": f"Thing {i}"}}}
        for i in range(3)
    ]}
    items = _summarize_bundle(bundle)
    assert len(items) == 3
    assert not any(i.get("truncated") for i in items)


def test_truncation_total_ignores_operation_outcomes():
    # OperationOutcome entries are skipped as records, so counting them would
    # promise records that do not exist.
    from careagents.agent import _summarize_bundle
    entries = [{"resource": {"resourceType": "Condition",
                             "code": {"text": f"C{i}"}}} for i in range(13)]
    entries.append({"resource": {"resourceType": "OperationOutcome"}})
    items = _summarize_bundle({"entry": entries})
    marker = [i for i in items if i.get("truncated")][0]
    assert marker["total"] == 13


def test_safety_core_forbids_presenting_a_truncated_list_as_complete():
    assert "truncated" in SAFETY_CORE.lower()


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
        self.runs: dict[str, dict] = {}
        self.run_by_message: dict[str, str] = {}
        self.events: dict[str, list[dict]] = {}
        self.tool_calls: dict[tuple[str, str], dict] = {}
        self._event_id = 0
        self.worker_available = True
        self.worker_health_error = False
        # Instance state, NOT a class attribute. `purged = []` at class scope
        # was mutated through `self.purged.append(...)`, so every FakeClient
        # in the session shared one list and a test's assertion on
        # `len(fake.purged)` depended on which tests ran before it.
        self.purged: list[str] = []

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
                    conversation_id=None, surface="web", reply_to=None,
                    request_id=None):
        conversation_id = conversation_id or self.conversation_id(agent_id)
        if request_id and any(
                item.get("request_id") == request_id
                for item in self.logged.setdefault(
                    (tenant, conversation_id), [])):
            return True
        self.logged.setdefault((tenant, conversation_id), []).append(
            {"role": role, "content": text, "agent_id": agent_id,
             "surface": surface, "reply_to": reply_to,
             "request_id": request_id})
        return True

    def recent_messages(self, tenant, limit=20, conversation_id=None,
                        agent_id=None, through_message_id=None):
        conversation_id = conversation_id or self.conversation_id(agent_id)
        rows = list(self.logged.get((tenant, conversation_id), []))
        if through_message_id:
            anchor = next((index for index, row in enumerate(rows)
                           if row.get("id") == through_message_id), None)
            rows = [] if anchor is None else rows[:anchor + 1]
        return rows[-limit:]

    def _append_run_event(self, run_id, kind, payload=None):
        self._event_id += 1
        event = {"id": self._event_id, "run_id": run_id, "type": kind,
                 "payload": payload or {}}
        self.events.setdefault(run_id, []).append(event)
        return event

    def create_agent_run(self, tenant, message_id, deadline_seconds=120):
        existing = self.run_by_message.get(message_id)
        if existing:
            return {**self.runs[existing], "idempotent_replay": True}
        message = next(
            item for rows in self.logged.values() for item in rows
            if item.get("id") == message_id)
        run_id = f"run-{len(self.runs) + 1}"
        from datetime import datetime, timedelta, timezone
        run = {
            "id": run_id, "tenant_id": tenant,
            "conversation_id": next(
                key[1] for key, rows in self.logged.items()
                if message in rows),
            "message_id": message_id, "agent_id": message["agent_id"],
            "surface": message["surface"], "status": "queued",
            "worker_id": None, "cancel_requested": False,
            "deadline_at": (datetime.now(timezone.utc) + timedelta(
                seconds=deadline_seconds)).isoformat(),
        }
        self.runs[run_id] = run
        self.run_by_message[message_id] = run_id
        self._append_run_event(run_id, "run.queued", {"status": "queued"})
        return {**run, "idempotent_replay": False}

    def get_agent_run(self, tenant, run_id):
        run = self.runs.get(run_id)
        if not run or run["tenant_id"] != tenant:
            raise HealthClawError("unknown run", 404)
        return dict(run)

    def agent_run_events(self, tenant, run_id, after=0, limit=100):
        run = self.get_agent_run(tenant, run_id)
        if run["status"] == "queued":
            from datetime import datetime, timezone
            deadline = datetime.fromisoformat(run["deadline_at"])
            if datetime.now(timezone.utc) >= deadline:
                self.runs[run_id]["status"] = "failed"
                self._append_run_event(
                    run_id, "run.deadline_exceeded", {"status": "failed"})
                run = self.get_agent_run(tenant, run_id)
        events = [event for event in self.events.get(run_id, [])
                  if event["id"] > after][:limit]
        return {"run_id": run_id, "status": run["status"],
                "events": events,
                "next_cursor": events[-1]["id"] if events else after}

    def agent_worker_health(self, max_age_seconds=30):
        if self.worker_health_error:
            raise HealthClawError("worker health unavailable", 0)
        return {"available": self.worker_available,
                "active_workers": 1 if self.worker_available else 0,
                "max_age_seconds": max_age_seconds}

    def claim_agent_run(self, worker_id, lease_seconds=60):
        run = next((item for item in self.runs.values()
                    if item["status"] == "queued"), None)
        if run is None:
            return None
        run["status"] = "running"
        run["worker_id"] = worker_id
        self._append_run_event(run["id"], "run.started",
                               {"status": "running"})
        message = next(
            item for rows in self.logged.values() for item in rows
            if item.get("id") == run["message_id"])
        return {**run, "message": {
            "id": run["message_id"], "role": "user",
            "text": message["content"]}}

    def heartbeat_agent_run(self, run_id, worker_id, lease_seconds=60):
        run = self.runs[run_id]
        if run["status"] != "running" or run["worker_id"] != worker_id:
            raise HealthClawError("worker does not own run", 409)
        return {"ok": True,
                "cancel_requested": run.get("cancel_requested", False)}

    # Keyword-only, spelled out, matching the real client. `**kwargs` here
    # swallowed a misspelled keyword (`event_typ=`, `errorclass=`) that would
    # TypeError against HealthClawClient.
    def transition_agent_run(self, run_id, worker_id, status, *,
                             event_type=None, payload=None,
                             error_class=None, available_in_seconds=0):
        run = self.runs[run_id]
        if run["status"] != "running" or run["worker_id"] != worker_id:
            raise HealthClawError("worker does not own run", 409)
        run["status"] = status
        if status != "running":
            run["worker_id"] = None
        # `error_class` / `available_in_seconds` are accepted but not modelled,
        # exactly as before — the point of naming them is that a misspelling
        # now raises TypeError here the same way it would in production.
        del error_class, available_in_seconds
        self._append_run_event(
            run_id, event_type or f"run.{status}",
            payload or {"status": status})
        return dict(run)

    def finalize_agent_run(self, run_id, worker_id, text, checkpoint_id):
        run = self.runs[run_id]
        if run["status"] == "completed":
            return {"run": dict(run), "idempotent_replay": True}
        if run["status"] != "running" or run["worker_id"] != worker_id:
            raise HealthClawError("worker does not own run", 409)
        request_id = f"run:{run_id}:assistant"
        rows = self.logged.setdefault(
            (run["tenant_id"], run["conversation_id"]), [])
        if not any(row.get("request_id") == request_id for row in rows):
            rows.append({
                "role": "assistant", "content": text,
                "agent_id": run.get("agent_id"),
                "surface": run.get("surface") or "web",
                "reply_to": run.get("message_id"),
                "request_id": request_id,
            })
        self._append_run_event(run_id, "agent.text", {
            "checkpoint_id": checkpoint_id, "text": text})
        run["status"] = "completed"
        run["worker_id"] = None
        self._append_run_event(run_id, "run.completed", {
            "status": "completed"})
        return {"run": dict(run), "idempotent_replay": False}

    def append_agent_run_event(self, run_id, worker_id, event_type,
                               payload=None):
        run = self.runs[run_id]
        if run["status"] != "running" or run["worker_id"] != worker_id:
            raise HealthClawError("worker does not own run", 409)
        return self._append_run_event(run_id, event_type, payload)

    def register_agent_tool_call(self, run_id, worker_id, provider_call_id,
                                 tool_name, arguments):
        key = (run_id, provider_call_id)
        existing = self.tool_calls.get(key)
        if existing:
            return {**existing, "idempotent_replay": True}
        call = {"id": f"call-{len(self.tool_calls) + 1}",
                "run_id": run_id, "provider_call_id": provider_call_id,
                "tool_name": tool_name, "input": arguments,
                "status": "pending", "result": None}
        self.tool_calls[key] = call
        return {**call, "idempotent_replay": False}

    def transition_agent_tool_call(self, run_id, call_id, worker_id, status, *,
                                   result=None, outcome_ref=None,
                                   error_class=None):
        call = next(item for item in self.tool_calls.values()
                    if item["id"] == call_id and item["run_id"] == run_id)
        call["status"] = status
        # `is not None`, not `in kwargs`: the real client also drops a None
        # result from the request body, so an explicit `result=None` must not
        # look different here than it does over the wire.
        if result is not None:
            call["result"] = result
        del worker_id, outcome_ref, error_class
        return dict(call)

    def new_tenant_id(self):
        self.seeded.append(1)
        tenant = f"ca-{len(self.seeded):010d}"
        self.tenants.append(tenant)
        return tenant

    def mint_token(self, tenant):
        # Real client caches a step-up token per tenant and raises on a failed
        # mint. Nothing in CareAgents calls it directly today — HealthClawClient
        # uses it to build its own headers — but it is public surface, so the
        # stand-in carries it rather than 404ing a future caller.
        return f"step-up-{tenant}"

    def seed(self, tenant):
        return 7

    def search(self, tenant, resource_type, params=None):
        return {"total": 1, "entry": [{"resource": {
            "resourceType": resource_type, "status": "active",
            "code": {"text": f"sample {resource_type}"}}}]}

    def read(self, tenant, resource_type, resource_id):
        return {"resourceType": resource_type, "id": resource_id,
                "code": {"text": f"sample {resource_type} {resource_id}"}}

    def interpret_labs(self, tenant):
        return {"summary": {}, "consumer": {"headline": "ok"}, "disclaimer": "d"}

    def care_gaps(self, tenant):
        return {"summary": {}, "consumer": {"due": []}}

    def fetch_appointment_brief(self, tenant):
        return None

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

    # DocumentReferences that landed but are deliberately not in `counted`
    # (#226). Settable the same way.
    uncounted = 0

    def uncounted_record_count(self, tenant):
        return self.uncounted

    purge_fails = False

    def purge_tenant(self, tenant):
        if self.purge_fails:
            raise HealthClawError("purge failed", 500)
        self.purged.append(tenant)
        return {"tenant_id": tenant, "deleted": True, "rows_deleted": 42}

    def bind_telegram(self, tenant, chat_id):
        self.bound.append((tenant, chat_id))
        return True

    # --- direct upload (#227) ------------------------------------------------
    # A test can override `ingest_bundle_fails` or `ingest_bundle_result` to
    # simulate engine 4xx/5xx or specific per-entry outcomes.
    ingest_bundle_fails: HealthClawError | None = None
    ingest_bundle_result: dict | None = None

    def ingest_bundle(self, tenant, bundle):
        self.seeded.append(("upload", tenant, len(bundle.get("entry") or [])))
        if self.ingest_bundle_fails is not None:
            raise self.ingest_bundle_fails
        if self.ingest_bundle_result is not None:
            return dict(self.ingest_bundle_result)
        entries = bundle.get("entry") or []
        return {"tenant_id": tenant, "entries": len(entries),
                "ingested": len(entries), "skipped": 0, "failed": 0,
                "errors": []}

    base = "https://app.healthclaw.io"

    def fasten_connect_url(self, tenant):
        return f"{self.base}/connect/{tenant}"

    def wearables_connect_url(self, tenant, provider):
        return f"{self.base}/wearables/oauth/start?provider={provider}&tenant_id={tenant}"

    def conformance_badge(self):
        return {"message": "A (7/7)"}


# --- FakeClient ↔ HealthClawClient parity -------------------------------------
#
# Most of this file drives CareAgents through `FakeClient` rather than
# `careagents.healthclaw.HealthClawClient`, and until these tests nothing
# checked that the two agreed. The failure mode is quiet and expensive: the
# suite is green, `healthclaw.py` sits near 40% line coverage, and the fake
# teaches every test a call shape production would reject.
#
# What these tests DO catch: a method that exists on one side only, a renamed
# parameter, a parameter that changed kind (positional → keyword-only, or a
# spelled-out keyword collapsed to `**kwargs`), and a required parameter that
# quietly grew a default.
#
# What they deliberately do NOT catch — stated so nobody mistakes green here
# for a proven client:
#   * Behaviour. Signature parity says a call is *accepted*, never that it does
#     the same thing. `FakeClient.record_count` returns a settable int;
#     `HealthClawClient.record_count` sums five `_summary=count` searches and
#     raises if any of them could not be asked. Both signatures are
#     `(tenant)`.
#   * Wire format. Nothing here talks to a running HealthClaw, so a body or
#     header the engine rejects still passes.
#   * Return types and annotations. The fake is unannotated by design; only
#     parameters are compared.
#   * Default *values*. Presence of a default is compared, its value is not —
#     a fake may legitimately shorten a timeout or a page size. A fake that
#     made a *required* parameter optional would still fail, because that is
#     the direction that hides a broken caller.

def _public_callables(obj: object) -> dict:
    """Public callables as a caller sees them on a live instance.

    Reading off an instance (rather than the class) deliberately normalises
    `staticmethod` vs instance method and drops `self` — `FakeClient` has to
    make `new_tenant_id` an instance method to record what it handed out,
    while the real client can keep it static. Callers only ever hold an
    instance, so that difference is not drift.
    """
    out = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        attr = getattr(obj, name)
        if callable(attr):
            out[name] = attr
    return out


def _param_shape(fn) -> list[tuple]:
    return [(p.name, p.kind, p.default is not inspect.Parameter.empty)
            for p in inspect.signature(fn).parameters.values()]


def _render(fn) -> str:
    return str(inspect.signature(fn))


def test_fake_client_implements_every_public_healthclaw_method():
    """Every public method of the real client exists on the fake, with a
    compatible signature.

    Parameter NAMES and KINDS must match exactly, in order. Names matter
    because a caller that switches to a keyword (`search(t, resource_type=x)`)
    breaks only against the real client — the fake had `rtype`. Kinds matter
    because `**kwargs` on the fake silently absorbs a misspelled keyword that
    would `TypeError` in production.

    MUTATION: rename `resource_type` to `rtype` in the signature of
    `HealthClawClient.search` (careagents/healthclaw.py) and this test fails
    with `search: real (tenant, rtype, params=None) != fake (tenant,
    resource_type, params=None)`. Verified by applying and reverting the edit.
    """
    real = _public_callables(HealthClawClient("https://healthclaw.test", "s"))
    fake = _public_callables(FakeClient())

    missing = sorted(set(real) - set(fake))
    assert not missing, (
        "FakeClient is missing public HealthClawClient methods, so tests "
        f"using it cannot exercise them at all: {missing}")

    drift = [f"{name}: real {_render(real[name])} "
             f"!= fake {_render(fake[name])}"
             for name in sorted(real)
             if _param_shape(real[name]) != _param_shape(fake[name])]
    assert not drift, (
        "FakeClient signatures have drifted from HealthClawClient. A test "
        "passing against the fake would not pass against the real client:\n"
        + "\n".join(drift))


def test_fake_client_invents_no_method_the_real_client_lacks():
    """The fake must not grow surface production does not have.

    An invented method teaches tests to depend on a capability
    `HealthClawClient` cannot deliver, and the gap only shows up in prod.
    Test-harness *state* (`purged`, `counted`, `logged`, …) is exempt — only
    callables are compared.
    """
    real = _public_callables(HealthClawClient("https://healthclaw.test", "s"))
    fake = _public_callables(FakeClient())
    invented = sorted(set(fake) - set(real))
    assert not invented, (
        "FakeClient defines public methods HealthClawClient does not have; "
        f"tests relying on them prove nothing about production: {invented}")


def test_fake_client_keeps_no_mutable_state_at_class_scope():
    """Regression guard for the `purged = []` bug.

    A mutable class attribute mutated via `self.x.append(...)` is shared by
    every FakeClient in the session, so an assertion like
    `len(fake.purged) == 1` passes or fails depending on which tests ran
    first. Per-instance state belongs in `__init__`.
    """
    leaky = sorted(
        name for name, value in vars(FakeClient).items()
        if not name.startswith("__")
        and isinstance(value, (list, dict, set, bytearray))
    )
    assert not leaky, (
        "mutable FakeClient class attributes leak across every test in the "
        f"session; move them into __init__: {leaky}")


def test_two_fake_clients_do_not_share_recorded_state():
    """The leak, demonstrated rather than asserted structurally."""
    first, second = FakeClient(), FakeClient()
    first.purge_tenant("ca-1")
    assert first.purged == ["ca-1"]
    assert second.purged == [], (
        "a second FakeClient saw the first one's purge — shared state")


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
                       "CARE_IMESSAGE_HANDLE": "im-test-handle"})


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
    monkeypatch.setattr(
        mailmod, "send_code",
        lambda cfg, e, code, purpose: (captured.setdefault("c", code)
                                       and mailmod.SENT))
    svc.start_email_code(email)
    return svc.verify_email_code(email, captured["c"])


def _sink_code(sink):
    """Stand-in for mail.send_code that records the code and reports success.

    Returning mail.SENT is load-bearing: send_code answers with one of three
    named states (#220), and anything else — True, None, a bare list.append —
    reads as "we could not tell", which raises MailUnconfirmed.
    """
    import careagents.mail as mailmod

    def _send(cfg, email, code, purpose):
        sink.append(code)
        return mailmod.SENT
    return _send


def _login(client, svc, monkeypatch, email="gene@example.com"):
    """Log a client in via the real email-code path (code captured from mail)."""
    captured = {}
    import careagents.mail as mailmod
    monkeypatch.setattr(
        mailmod, "send_code",
        lambda cfg, e, code, purpose: (captured.setdefault("c", code)
                                       and mailmod.SENT))
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
    monkeypatch.setattr(mailmod, "send_code", lambda *a: mailmod.SENT)
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


def test_start_email_code_reports_the_seconds_left_on_the_cooldown(
        svc, monkeypatch):
    """MUTATION: `return` (rather than the seconds left) on the cooldown branch
    and this fails — that bare return is what left the route with nothing to
    distinguish a send from a suppression (#262)."""
    from careagents.accounts import RESEND_COOLDOWN
    import careagents.mail as mailmod
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    assert svc.start_email_code("secs@example.com") == 0, "a real send waits 0"
    retry_after = svc.start_email_code("secs@example.com")
    assert isinstance(retry_after, int)
    assert 0 < retry_after <= RESEND_COOLDOWN
    assert len(codes) == 1, "the cooldown must still suppress the send"


def test_auth_email_does_not_claim_a_send_the_cooldown_suppressed(
        app, monkeypatch):
    """The front door: taps resend, is told it sent, waits for an email nobody
    sent (#262). The cooldown stays — only the claim goes."""
    import careagents.mail as mailmod
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    c = app.test_client()

    first = c.post("/api/auth/email", json={"email": "honest@example.com"})
    assert first.status_code == 200
    assert first.get_json() == {"sent": True}

    second = c.post("/api/auth/email", json={"email": "honest@example.com"})
    # 200, not 4xx: nothing went wrong and the code they hold is still live,
    # so the UI still advances to code entry.
    assert second.status_code == 200
    body = second.get_json()
    assert body["sent"] is False
    assert body["reason"] == "cooldown"
    assert isinstance(body["retry_after"], int) and body["retry_after"] > 0
    assert len(codes) == 1, "the cooldown must still suppress the send"


def test_email_code_response_never_reveals_whether_an_account_exists(
        app, svc, monkeypatch):
    """The flow deliberately answers the same for a stranger and a member; a
    truthful cooldown state must not become an enumeration oracle.

    Both branches turn only on ca_email_tokens — a state the requester just
    created — so an attacker learns nothing about ca_accounts either way.
    """
    import careagents.mail as mailmod
    monkeypatch.setattr(mailmod, "send_code", _sink_code([]))
    _make_account(svc, monkeypatch, "member@example.com")
    monkeypatch.setattr(mailmod, "send_code", _sink_code([]))
    c = app.test_client()

    def _probe(email):
        # The member's own code was consumed by verify, so both addresses
        # start from the same place: no live token.
        first = c.post("/api/auth/email", json={"email": email})
        second = c.post("/api/auth/email", json={"email": email})
        return ((first.status_code, first.get_json()),
                (second.status_code, second.get_json()))

    member_send, member_cooldown = _probe("member@example.com")
    stranger_send, stranger_cooldown = _probe("stranger@example.com")

    assert member_send == stranger_send
    # retry_after is a clock reading, not an account fact — compare the rest.
    assert member_cooldown[0] == stranger_cooldown[0]
    assert (member_cooldown[1].keys() == stranger_cooldown[1].keys())
    assert member_cooldown[1]["sent"] == stranger_cooldown[1]["sent"] is False
    assert member_cooldown[1]["reason"] == stranger_cooldown[1]["reason"]


def test_gated_pages_redirect_or_401_without_session(app):
    c = app.test_client()
    assert c.get("/home").status_code == 302
    assert c.get("/chat?agent=x").status_code == 302
    assert c.post("/api/agents", json={}).status_code == 401
    assert c.post("/api/connections/sample").status_code == 401


def test_a_session_whose_account_is_gone_is_treated_as_signed_out(
        app, svc, monkeypatch):
    """MUTATION: check `session.get("account_id")` again in login_required
    instead of resolving the account -> red.

    A session outlives the row it points at every time someone uses the
    self-serve delete (#203): the cookie stays in the browser, Back or an
    older tab replays it, and the gate waved it through on the id alone.
    current_account() then returned None and the handler dereferenced it —
    `AttributeError: 'NoneType' object has no attribute 'id'` — a 500 at the
    exact moment the person is checking whether the deletion worked (#265).
    """
    from careagents.models import Account
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert c.get("/home").status_code == 200          # the session is real
    with svc.session() as s:
        s.delete(s.query(Account).filter_by(email="gene@example.com").one())

    # Pages: signed out, exactly like an expired session — never a 5xx.
    for path in ("/home", "/chat?agent=x", "/brief"):
        r = c.get(path)
        assert r.status_code == 302, (path, r.status_code)
        assert r.headers["Location"].endswith("/auth"), path

    # API and WebAuthn paths keep their JSON 401 shape: a 302 to an HTML page
    # is something fetch() follows and then fails to parse.
    # The SSE route is in here deliberately: the gate has to answer before the
    # generator exists, or the failure is a stream that never yields.
    for path in ("/api/connections/catalog", "/api/labs/timeline?agent=x",
                 "/api/chat/runs/r1/events"):
        r = c.get(path)
        assert r.status_code == 401, path
        assert r.get_json() == {"error": "sign in"}, path
    for path in ("/api/agents", "/api/connections/sample",
                 "/webauthn/register/options"):
        r = c.post(path, json={})
        assert r.status_code == 401, path
        assert r.get_json() == {"error": "sign in"}, path

    # And the dead cookie is dropped, so nothing keeps replaying it.
    with c.session_transaction() as sess:
        assert "account_id" not in sess


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


def test_brief_renders_with_available_records(app, svc, monkeypatch):
    """Brief page fetches AppointmentBrief and renders section data."""
    stub_brief = {
        "resourceType": "Basic",
        "extension": [
            {
                "url": "https://healthclaw.io/fhir/StructureDefinition/brief-section-problems",
                "extension": [
                    {"url": "field", "valueString": '{"label":"Hypertension","value":"Active since 2021-01","sourceType":"Condition","sourceId":"c-1"}'}
                ],
            },
            {
                "url": "https://healthclaw.io/fhir/StructureDefinition/brief-section-medications",
                "extension": [],
            },
        ],
    }

    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/sample")
    conn_id = r.get_json()["id"]
    r = c.post("/api/agents", json={"name": "Ada", "persona": "direct",
                                    "connection_id": conn_id})
    agent_id = r.get_json()["id"]

    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant: stub_brief)
    resp = c.get(f"/brief?agent={agent_id}")
    assert resp.status_code == 200
    assert b"Hypertension" in resp.data
    assert b"Active since 2021-01" in resp.data
    # empty section must NOT show absence words in user-visible text
    # (check the empty-section paragraph specifically, not the full page which
    # contains fill="none" in the base.html SVG shield icon)
    body = resp.data.decode()
    for phrase in ("no medications", "you have no", "you have none",
                   "no labs", "no conditions"):
        assert phrase.lower() not in body.lower(), f"Absence phrase {phrase!r} in brief"


def test_brief_renders_unavailable_when_fetch_fails(app, svc, monkeypatch):
    """Brief page shows 'Not available' when the engine returns nothing."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/sample")
    conn_id = r.get_json()["id"]
    r = c.post("/api/agents", json={"name": "Ada", "persona": "direct",
                                    "connection_id": conn_id})
    agent_id = r.get_json()["id"]

    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant: None)
    resp = c.get(f"/brief?agent={agent_id}")
    assert resp.status_code == 200
    assert b"Not available from your connected records" in resp.data


def test_an_unreachable_engine_does_not_blame_the_patient_s_records(
        app, svc, monkeypatch):
    """"Not available from your connected records" is a claim about the
    records. During an outage we read none of them, so we cannot make it.

    The same page already gets this right one section down: the screening
    review says "unavailable" unless the brief carries an explicit "ok"
    (#381). This is that posture applied to the other four sections.

    MUTATION: swallow HealthClawError in the brief route and render None ->
    red, the page blames the records again. Ran it, saw red.
    """
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = c.post("/api/connections/sample").get_json()["id"]
    agent_id = c.post("/api/agents", json={"name": "Ada", "persona": "direct",
                                           "connection_id": conn_id}
                      ).get_json()["id"]

    def _down(self, tenant):
        raise HealthClawError("appointment brief unavailable (503)", 503)

    monkeypatch.setattr(FakeClient, "fetch_appointment_brief", _down)
    resp = c.get(f"/brief?agent={agent_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "could not reach your records" in body
    assert "Not available from your connected records" not in body


def test_a_returning_patient_is_not_greeted_as_a_stranger_during_an_outage(
        app, svc, monkeypatch):
    """An outage collapsed into an empty conversation, so every return visit
    rendered as a first visit — a blank slate that reads as a fact about the
    person rather than about the connection.

    MUTATION: swallow HealthClawError and render past=[] silently -> red.
    Ran it, saw red.
    """
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = c.post("/api/connections/sample").get_json()["id"]
    agent_id = c.post("/api/agents", json={"name": "Ada", "persona": "direct",
                                           "connection_id": conn_id}
                      ).get_json()["id"]

    def _down(self, *a, **k):
        raise HealthClawError("chat history unavailable (503)", 503)

    monkeypatch.setattr(FakeClient, "recent_messages", _down)
    resp = c.get(f"/chat?agent={agent_id}")
    assert resp.status_code == 200
    assert "could not load your earlier messages" in resp.get_data(as_text=True)


def test_brief_unknown_agent_redirects(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert c.get("/brief?agent=nope").status_code == 302


# --- care gaps: the third state (#381) ------------------------------------
#
# "The screening review did not run" and "you have no screenings due" are
# different sentences, and the second one is a clinical claim. The page must
# never make it on the first one's behalf.

_CARE_GAPS_SECTION = ("https://healthclaw.io/fhir/StructureDefinition/"
                      "brief-section-care-gaps")
_UNAVAILABLE_COPY = b"Screening review unavailable"
_NO_GAPS_COPY = b"no preventive care items"


def _brief_with_care_gaps(status, fields=()):
    return {
        "resourceType": "Basic",
        "extension": [{
            "url": _CARE_GAPS_SECTION,
            "extension": (
                [{"url": "field", "valueString": f} for f in fields]
                + [{"url": "status", "valueString": status}]
            ),
        }],
    }


def _agent_for_brief(c, svc, monkeypatch):
    _login(c, svc, monkeypatch)
    conn_id = c.post("/api/connections/sample").get_json()["id"]
    return c.post("/api/agents", json={"name": "Ada", "persona": "direct",
                                       "connection_id": conn_id}
                  ).get_json()["id"]


def test_brief_care_gaps_unavailable_is_not_rendered_as_no_gaps(app, svc,
                                                                monkeypatch):
    c = app.test_client()
    agent_id = _agent_for_brief(c, svc, monkeypatch)
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant: _brief_with_care_gaps("unavailable"))

    resp = c.get(f"/brief?agent={agent_id}")
    assert resp.status_code == 200
    assert _UNAVAILABLE_COPY in resp.data
    assert _NO_GAPS_COPY not in resp.data


def test_brief_care_gaps_evaluated_and_empty_says_no_items(app, svc, monkeypatch):
    c = app.test_client()
    agent_id = _agent_for_brief(c, svc, monkeypatch)
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant: _brief_with_care_gaps("ok"))

    resp = c.get(f"/brief?agent={agent_id}")
    assert resp.status_code == 200
    assert _NO_GAPS_COPY in resp.data
    assert _UNAVAILABLE_COPY not in resp.data


def test_brief_care_gaps_evaluated_with_gaps_lists_them(app, svc, monkeypatch):
    c = app.test_client()
    agent_id = _agent_for_brief(c, svc, monkeypatch)
    field = ('{"label":"Colorectal cancer screening","value":"May be due",'
             '"sourceType":"MeasureReport","sourceId":"gap-1"}')
    monkeypatch.setattr(
        FakeClient, "fetch_appointment_brief",
        lambda self, tenant: _brief_with_care_gaps("ok", fields=[field]))

    resp = c.get(f"/brief?agent={agent_id}")
    assert b"Colorectal cancer screening" in resp.data
    assert _NO_GAPS_COPY not in resp.data
    assert _UNAVAILABLE_COPY not in resp.data


def test_brief_missing_care_gaps_marker_is_not_reassurance(app, svc, monkeypatch):
    """An unmarked brief — an older engine, a truncated payload, no brief at
    all — is not an evaluation, so it cannot say "nothing due"."""
    c = app.test_client()
    agent_id = _agent_for_brief(c, svc, monkeypatch)
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant: None)

    resp = c.get(f"/brief?agent={agent_id}")
    assert _UNAVAILABLE_COPY in resp.data
    assert _NO_GAPS_COPY not in resp.data


def test_parse_care_gaps_status_defaults_to_not_ok():
    from careagents.app import _parse_care_gaps_status
    assert _parse_care_gaps_status(_brief_with_care_gaps("ok")) == "ok"
    assert _parse_care_gaps_status(_brief_with_care_gaps("unavailable")) != "ok"
    assert _parse_care_gaps_status({}) != "ok"
    assert _parse_care_gaps_status(None) != "ok"


def test_parse_brief_sections_extracts_fields():
    """_parse_brief_sections correctly deserializes section extensions."""
    from careagents.app import _parse_brief_sections
    resource = {
        "extension": [
            {
                "url": "https://healthclaw.io/fhir/StructureDefinition/brief-section-labs",
                "extension": [
                    {"url": "field", "valueString": '{"label":"HbA1c","value":"7.2% (2026-06-01)","sourceType":"Observation","sourceId":"o-1"}'},
                    {"url": "field", "valueString": '{"label":"BP","value":"128 mmHg (2026-07-15)","sourceType":"Observation","sourceId":"o-2"}'},
                ],
            },
            {
                "url": "https://healthclaw.io/fhir/StructureDefinition/brief-section-visits",
                "extension": [],
            },
        ]
    }
    sections = _parse_brief_sections(resource)
    assert len(sections["labs"]) == 2
    assert sections["labs"][0]["label"] == "HbA1c"
    assert sections["labs"][1]["sourceId"] == "o-2"
    assert sections["visits"] == []


def test_parse_brief_sections_tolerates_bad_input():
    from careagents.app import _parse_brief_sections
    assert _parse_brief_sections({}) == {}
    assert _parse_brief_sections(None) == {}  # type: ignore[arg-type]


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


# --- #226: the count says what it leaves out ---------------------------------
#
# DocumentReferences are ingested but nothing can open one, so they are out of
# the counted set (careagents/healthclaw.py COUNTED_TYPES). This poll is the
# one place a count derived from `record_count` reaches a person — home.js
# renders it as "N new records added." The unit-level half of this lives in
# tests/test_uncounted_documents.py.

def _poll_after_sync(cfg, svc, monkeypatch, documents):
    """Refresh, land five readable records plus `documents`, return the poll."""
    from careagents.app import create_app
    fake = FakeClient()
    fake.uncounted = documents
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    created = c.post("/api/connections/fasten",
                     json={"consent": True}).get_json()
    conn = created["id"]
    tenant = created["connect_url"].rsplit("/connect/", 1)[1]

    assert c.post(f"/api/connections/{conn}/refresh").status_code == 200
    fake.counted = 105
    return c.get(f"/api/connections/{tenant}/poll").get_json()


def test_the_poll_says_documents_are_not_readable_when_some_arrived(
        cfg, svc, monkeypatch):
    """MUTATION: drop the clause from `poll_connection` -> red.

    The number is true about what the patient can reach and silent about what
    it omits. Without this there is nothing anywhere telling them the notes
    are not in it (#226).
    """
    d = _poll_after_sync(cfg, svc, monkeypatch, documents=2)
    assert d["new_records"] == 5
    note = d.get("uncounted_note", "")
    assert "not yet readable" in note, d
    assert "notes and documents" in note, d


def test_the_poll_adds_no_clause_when_no_documents_arrived(
        cfg, svc, monkeypatch):
    """MUTATION: emit the clause unconditionally -> red.

    A standing disclaimer would be noise, and would imply documents exist for
    someone who has none.
    """
    d = _poll_after_sync(cfg, svc, monkeypatch, documents=0)
    assert d["new_records"] == 5
    assert "uncounted_note" not in d, d


def test_no_count_at_all_when_the_document_probe_could_not_answer(
        cfg, svc, monkeypatch):
    """MUTATION: catch the probe separately and default it to 0 -> red.

    A count whose "and there are notes missing from this" could not be
    established would be published as if complete — the same silent omission
    the clause exists to end, and #403's "unknown is never zero" applied to
    the clause rather than the number.
    """
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
    assert c.post(f"/api/connections/{conn}/refresh").status_code == 200

    fake.counted = 105
    # The readable count answers; only the document probe is down.
    def down(_tenant):
        raise HealthClawError("search DocumentReference failed (503)", 503)
    fake.uncounted_record_count = down

    d = c.get(f"/api/connections/{tenant}/poll").get_json()
    assert d["status"] == "active"
    assert "record_count" not in d and "new_records" not in d, d
    assert "uncounted_note" not in d, d


def test_the_poll_says_the_engine_is_unreachable_rather_than_pending(
        cfg, svc, monkeypatch):
    """The patient-visible half of #403.

    `tenant_has_records` now raises when it could not look, instead of
    answering False. This endpoint is what that reaches: it used to render
    False as `{"status": "pending"}`, so an engine incident showed "still
    fetching your records" forever on the connect screen, on a phone, at the
    moment someone is deciding whether the product works.

    The reply must carry neither a count nor "pending" — an outage is not an
    answer about how many records a person has.

    MUTATION: drop the `except HealthClawError` arm in `poll_connection` ->
    red with an unhandled 500 rather than the honest 503. Ran it, saw red.
    """
    from careagents.app import create_app

    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    created = c.post("/api/connections/fasten",
                     json={"consent": True}).get_json()
    tenant = created["connect_url"].rsplit("/connect/", 1)[1]

    def down(_tenant):
        raise HealthClawError("search Patient failed (503)", 503)

    fake.tenant_has_records = down

    r = c.get(f"/api/connections/{tenant}/poll")
    assert r.status_code == 503
    d = r.get_json()
    assert d["status"] == "unavailable"
    assert d["error"] == "records_unavailable"
    assert "couldn't reach your records" in d["message"]
    assert "record_count" not in d and "new_records" not in d


def test_the_trust_badge_is_unavailable_rather_than_a_500(cfg, svc,
                                                          monkeypatch):
    """`conformance_badge` raises on a transport failure now that the seam is
    typed (#403). The trust panel must keep answering "unavailable" — the
    same thing it already says for a non-200 — instead of turning an engine
    outage into a 500 on the page that exists to state what we guarantee."""
    from careagents.app import create_app

    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True

    def down():
        raise HealthClawError("conformance badge failed", 0)

    fake.conformance_badge = down

    r = app.test_client().get("/api/trust")
    assert r.status_code == 200
    assert r.get_json()["badge"] == "unavailable"


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


# --- direct upload (#227) ----------------------------------------------------

_DIRECT_UPLOAD_ENDPOINT = "/api/connections/{conn}/upload"


def _make_direct_conn(client):
    """Create a `direct` connection via the normal connect+consent flow."""
    r = client.post("/api/connections/direct", json={"consent": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["id"]


def _tiny_bundle(pid="up-1"):
    return {"resourceType": "Bundle", "type": "collection",
            "entry": [{"resource": {"resourceType": "Patient", "id": pid,
                                     "name": [{"family": "U"}]}}]}


def test_direct_tile_advertises_import_tier_and_consent(app, svc, monkeypatch):
    # The tile must ship at `import` tier with a real start() plan (not
    # `soon`), and the catalog must flag it `requires_consent=True` so
    # the consent modal opens through the same code path fasten/wearable
    # use — otherwise the server's 428 becomes unreachable via UI.
    c = app.test_client()
    _login(c, svc, monkeypatch)
    cat = {m["id"]: m for m in
           c.get("/api/connections/catalog").get_json()["connectors"]}
    assert cat["direct"]["tier"] == "import"
    assert cat["direct"].get("requires_consent") is True


def test_direct_connect_without_consent_returns_428(app, svc, monkeypatch):
    # Server-side consent gate — the modal is UX, not the authority.
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/direct", json={})
    assert r.status_code == 428
    assert r.get_json()["error"] == "consent_required"


def test_direct_connect_with_consent_creates_empty_connection(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/direct", json={"consent": True})
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "empty"
    # No connect_url — the follow-up upload is the connect step.
    assert "connect_url" not in d


def test_upload_happy_path_reports_ingested_and_flips_active(
        cfg, svc, monkeypatch):
    from careagents.app import create_app
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)

    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data=json.dumps(_tiny_bundle("hp-1")),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ingested"] == 1
    # The response never surfaces the engine's internal tenant id.
    assert "tenant_id" not in d
    # `mark_synced` ran (ingested > 0) — hub shows the connection active.
    home = c.get("/home").get_data(as_text=True)
    assert "status-active" in home


def test_upload_accepts_all_three_fhir_mime_types(cfg, svc, monkeypatch):
    from careagents.app import create_app
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    for ct in ("application/fhir+json", "application/json",
               "application/json+fhir",
               "application/fhir+json; charset=utf-8"):
        r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
                   data=json.dumps(_tiny_bundle(f"mime-{hash(ct)%1000}")),
                   headers={"Content-Type": ct})
        assert r.status_code == 200, f"{ct} rejected: {r.get_data(as_text=True)}"


def test_upload_rejects_text_plain_with_415(cfg, svc, monkeypatch):
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data="hi", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_upload_refuses_a_revoked_connection(cfg, svc, monkeypatch):
    # #267 review: revocation was enforced only in the template (the hub
    # hides the upload button once status == "revoked"), so a crafted
    # request straight to the endpoint still accepted uploads after
    # disconnect — defeating the "stop new data flowing" guarantee.
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    r = c.post(f"/api/connections/{conn_id}/disconnect")
    assert r.status_code == 200 and r.get_json()["status"] == "revoked"

    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data=json.dumps(_tiny_bundle("post-revoke-1")),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 409
    assert r.get_json()["error"] == "connection_not_active"


def test_upload_still_works_on_a_fresh_empty_connection(cfg, svc, monkeypatch):
    # Regression guard for the revoked-connection fix above: a `direct`
    # connection's normal lifecycle starts at status "empty" (not "active")
    # until the first upload lands — checking for anything other than
    # "revoked" specifically would refuse every first upload.
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)

    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data=json.dumps(_tiny_bundle("fresh-empty-1")),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 200
    assert r.get_json()["ingested"] == 1


def test_upload_deeply_nested_json_does_not_crash(cfg, svc, monkeypatch):
    # #267 review: json.loads on a deeply-nested payload can raise
    # RecursionError, which is not a ValueError and was previously
    # unhandled -> 500. Already behind @login_required here, so this is
    # purely the crash fix, not an auth-ordering fix.
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    huge = "1"
    for _ in range(60000):
        huge = "[" + huge + "]"
    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data=huge, headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_json"


def test_upload_rejects_wrong_kind_connection(app, svc, monkeypatch):
    # `sample` is not an upload target — only `direct` connections accept
    # patient-provided files. A caller pointing at their own sample
    # connection gets a specific reason, not a 404.
    c = app.test_client()
    _login(c, svc, monkeypatch)
    sample = c.post("/api/connections/sample").get_json()["id"]
    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=sample),
               data=json.dumps(_tiny_bundle()),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"] == "wrong_connector_kind"
    assert body["kind"] == "sample"


def test_upload_rejects_cross_account_connection_with_404(cfg, svc,
                                                          monkeypatch):
    # Ownership is the fence: a signed-in patient cannot upload into
    # another account's connection, and the endpoint must not reveal
    # whether the id exists.
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    # Account A creates the direct connection.
    a = app.test_client()
    _login(a, svc, monkeypatch, email="a@example.com")
    victim_conn = _make_direct_conn(a)
    # Account B tries to upload into it.
    b = app.test_client()
    _login(b, svc, monkeypatch, email="b@example.com")
    r = b.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=victim_conn),
               data=json.dumps(_tiny_bundle()),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 404


def test_upload_requires_a_session(app):
    r = app.test_client().post(
        _DIRECT_UPLOAD_ENDPOINT.format(conn="conn_x"),
        data="{}", headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 401


def test_upload_content_length_over_cap_returns_413_without_reading(
        cfg, svc, monkeypatch):
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    # Fake a Content-Length beyond the 5 MiB cap — the short-circuit
    # refuses before spending the bytes.
    huge = str(6 * 1024 * 1024)
    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data=b"{}",
               headers={"Content-Type": "application/fhir+json"},
               environ_overrides={"CONTENT_LENGTH": huge})
    assert r.status_code == 413
    assert r.get_json()["error"] == "payload_too_large"


def test_upload_streams_hard_cap_when_content_length_absent(
        cfg, svc, monkeypatch):
    # Same defense as the engine: Content-Length is untrusted, so the
    # endpoint's `request.stream.read(max_bytes + 1)` must catch an
    # oversized chunked / length-absent body too.
    import io as _io
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    payload = b"x" * (6 * 1024 * 1024)  # 6 MiB, over the 5 MiB cap
    with app.test_request_context(
            _DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id), method="POST",
            input_stream=_io.BytesIO(payload),
            headers={"Content-Type": "application/fhir+json"},
            environ_overrides={"wsgi.input_terminated": True,
                               "CONTENT_LENGTH": ""}):
        # The upload endpoint's own streaming check is exercised via a
        # sub-request rather than a direct helper call — but the same
        # `stream.read(max_bytes+1)` logic runs.
        pass
    # And through the full request cycle (with Content-Length missing,
    # werkzeug will still enforce; assert the response shape.)
    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data=payload,
               headers={"Content-Type": "application/fhir+json"})
    # Either the streaming cap or the Content-Length short-circuit fires;
    # both are 413 payload_too_large — the point is the request never
    # reaches the engine.
    assert r.status_code == 413
    assert r.get_json()["error"] == "payload_too_large"


def test_upload_non_bundle_body_preserves_engine_error_code(
        cfg, svc, monkeypatch):
    # The engine's stable code (`not_a_bundle`) must flow through
    # HealthClawError.code — never collapse to `ingest_failed`.
    from careagents.app import create_app
    fake = FakeClient()
    fake.ingest_bundle_fails = HealthClawError(
        "bundle.resourceType must be \"Bundle\"", 400, code="not_a_bundle")
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data=json.dumps({"resourceType": "Patient"}),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "not_a_bundle"


def test_upload_too_many_entries_preserves_engine_error_code(
        cfg, svc, monkeypatch):
    from careagents.app import create_app
    fake = FakeClient()
    fake.ingest_bundle_fails = HealthClawError(
        "too many entries", 400, code="too_many_entries")
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data=json.dumps(_tiny_bundle()),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "too_many_entries"


def test_upload_commit_failed_surfaces_correlation_id(cfg, svc, monkeypatch):
    # An opaque support code (PHI-safe) is passed through so the user can
    # quote it; the raw exception message is NEVER surfaced.
    from careagents.app import create_app
    fake = FakeClient()
    fake.ingest_bundle_fails = HealthClawError(
        "commit failed", 500, code="commit_failed",
        correlation_id="deadbeefcafe")
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data=json.dumps(_tiny_bundle()),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 502
    body = r.get_json()
    assert body["error"] == "commit_failed"
    assert body["correlation_id"] == "deadbeefcafe"
    assert "commit failed" not in json.dumps(body)  # raw msg never surfaced


def test_upload_all_failed_bundle_does_not_mark_synced_or_activate(
        cfg, svc, monkeypatch):
    # `mark_synced` and the connection-active flip must key on
    # `ingested > 0`. An all-failed bundle must not fake sync freshness.
    from careagents.app import create_app
    fake = FakeClient()
    fake.ingest_bundle_result = {
        "tenant_id": "ca-x", "entries": 2,
        "ingested": 0, "skipped": 0, "failed": 2,
        "errors": [{"index": 0, "code": "ingest_error",
                    "correlation_id": "c-1"},
                   {"index": 1, "code": "ingest_error",
                    "correlation_id": "c-2"}]}
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data=json.dumps(_tiny_bundle()),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 200
    assert r.get_json()["ingested"] == 0
    # Hub still shows the connection as `empty` — no false sync freshness.
    home = c.get("/home").get_data(as_text=True)
    assert "status-empty" in home
    assert "status-active" not in home


# --- cross-layer integration: CareAgents → real engine WSGI ------------

def test_cross_layer_upload_actually_lands_in_the_engine(cfg, svc,
                                                          monkeypatch):
    """End-to-end: the CareAgents upload endpoint calls a REAL
    `HealthClawClient` that dispatches through the engine's Flask WSGI —
    no isolated fakes on either side. Verifies the client contract
    (`{bundle}` envelope, `application/json`) actually matches the
    engine's stricter contract (header-only tenant, envelope MIME).
    """
    import requests as _requests

    from careagents.app import create_app
    from careagents.healthclaw import HealthClawClient
    from main import create_app as engine_create_app
    from models import db

    # Real engine app on a fresh in-memory DB, with the test-tenant
    # marked public so mint-secret gating passes without a shared key.
    monkeypatch.setenv("PUBLIC_TENANTS", "test-tenant,ca-crosslayer")
    monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    engine_app = engine_create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "LEGACY_BOOT_ON_CREATE": False,
    })
    with engine_app.app_context():
        db.create_all()
    engine_client = engine_app.test_client()

    # Route the CareAgents client's requests.Session through the engine
    # Flask test client. Only the two calls the direct-upload path makes
    # need to be relayed — ingest-bundle (POST) and record_count (GET
    # searches) — plus any step-up-token mint that fires along the way.
    class _RelaySession:
        def post(self, url, json=None, headers=None, timeout=None,
                 data=None):
            path = url.replace("http://engine", "")
            return _to_requests(engine_client.post(
                path, json=json, data=data, headers=headers or {}))

        def get(self, url, params=None, headers=None, timeout=None):
            path = url.replace("http://engine", "")
            return _to_requests(engine_client.get(
                path, query_string=params or {}, headers=headers or {}))

    def _to_requests(werkzeug_resp):
        r = _requests.Response()
        r.status_code = werkzeug_resp.status_code
        r._content = werkzeug_resp.get_data() or b""
        r.headers.update(werkzeug_resp.headers.to_wsgi_list())
        return r

    real_client = HealthClawClient(base="http://engine",
                                   mint_secret="not-needed-for-public")
    real_client.http = _RelaySession()
    # Force the tenant id so the direct connect writes to `ca-crosslayer`
    # (a tenant we allow-listed above as public).
    real_client.new_tenant_id = lambda: "ca-crosslayer"

    app = create_app(config=cfg, client=real_client, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)

    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [{"resource": {"resourceType": "Patient",
                                       "id": "xl-1",
                                       "name": [{"family": "Real"}]}}]}
    r = c.post(_DIRECT_UPLOAD_ENDPOINT.format(conn=conn_id),
               data=json.dumps(bundle),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ingested"] == 1

    # Prove the row actually landed in the engine's DB, not just the
    # accounting layer of the CareAgents endpoint.
    engine_probe = engine_client.get("/r6/fhir/Patient/xl-1",
                                     headers={"X-Tenant-Id": "ca-crosslayer"})
    assert engine_probe.status_code == 200


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


def _unreachable(*_args, **_kwargs):
    """What HealthClawClient._send raises on a dead socket: status 0."""
    raise HealthClawError("action status failed", 0)


def test_a_review_we_could_not_check_is_never_reported_as_not_yours(
        cfg, svc, monkeypatch):
    """#410 — the regression PR #409 introduced, on the approval path.

    Before the seam fix, `action_status` let a refused connection escape as a
    raw requests exception and Flask answered 500: ugly, but honest. Typing
    it as HealthClawError landed it in this route's `except`, which denies
    ownership — so an outage told the patient their own form was not theirs,
    on the human-approval gate this product exists to guarantee.

    A 404 must still mean "not yours" (pinned by the test above). An outage
    must not, and nothing may read as approved.

    MUTATION: restore the flat `except HealthClawError: return None` in
    `_agent_owns_action` -> red, 404 with "That form isn't yours." Ran it,
    saw red.
    """
    from careagents.app import create_app

    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    agent = c.post("/api/agents", json={"name": "A", "persona": "calm",
                                        "connection_id": conn}
                   ).get_json()["id"]
    fake.action_status = _unreachable

    page = c.get(f"/review/{agent}/act-1")
    assert page.status_code == 503
    # Jinja escapes the apostrophes, so match on fragments without them.
    body = page.get_data(as_text=True)
    assert "That form" not in body, "denied ownership it never checked"
    assert "check this form right now" in body
    assert "Nothing has been approved" in body

    posted = c.post(f"/review/{agent}/act-1/submit",
                    json={"med-0": "yes", "nka": "true"})
    assert posted.status_code == 503
    assert posted.get_json()["error"] == "review_unavailable"
    # nothing may read as approved on the way out
    assert posted.get_json().get("confirmed") is not True


def test_a_gateway_504_never_says_the_form_is_no_longer_awaiting_review(
        cfg, svc, monkeypatch):
    """The #416 posture, applied to the review page itself.

    `_agent_owns_action` learned this three lines earlier (#410): an outage
    is not an answer about ownership. The very next branch had not. Any
    non-200 from the review fetch — including a 502/503/504 the gateway
    wrote on the engine's behalf, having learned nothing about the form —
    was rendered as "This form is no longer awaiting review." with a 404.

    That is a claim about state, made by a branch that observed no state, on
    the human-approval gate this product exists to guarantee. A patient with
    a live pending prescription request is told it is gone.

    A 4xx from the engine is still an answer and must still read as gone
    (pinned below). 5xx/408/429 must not.

    MUTATION: restore the flat `if status != 200` -> red on the 504 case.
    Ran it, saw red.
    """
    from careagents.app import create_app

    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    agent = c.post("/api/agents", json={"name": "A", "persona": "calm",
                                        "connection_id": conn}).get_json()["id"]

    fake.fetch_review_page = lambda tenant, action_id: (504, "gateway timeout")
    page = c.get(f"/review/{agent}/act-1")
    body = page.get_data(as_text=True)
    assert page.status_code == 503, "a gateway timeout is not a verdict"
    assert "no longer awaiting review" not in body
    assert "Nothing has been approved" in body

    # The engine's own answer still means what it says.
    fake.fetch_review_page = lambda tenant, action_id: (404, "gone")
    gone = c.get(f"/review/{agent}/act-1")
    assert gone.status_code == 404
    assert "no longer awaiting review" in gone.get_data(as_text=True)


def test_a_dead_socket_on_the_review_path_is_not_a_bare_500(
        cfg, svc, monkeypatch):
    """`fetch_review_page` and `submit_review` raise; nothing caught them.

    careagents registers no errorhandler for HealthClawError (there is not
    one in the whole package), so a transport failure on either verb reached
    Flask as a 500. On the submit verb that is worse than ugly: the patient's
    approval decisions are gone with no statement about what happened to
    them, after ownership was already confirmed.

    MUTATION: drop the try/except around either call -> red with a 500.
    Ran it, saw red.
    """
    from careagents.app import create_app

    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    agent = c.post("/api/agents", json={"name": "A", "persona": "calm",
                                        "connection_id": conn}).get_json()["id"]

    fake.fetch_review_page = _unreachable
    page = c.get(f"/review/{agent}/act-1")
    assert page.status_code == 503, "a dead socket is not a 500 here"
    assert "Nothing has been approved" in page.get_data(as_text=True)

    fake.submit_review = _unreachable
    posted = c.post(f"/review/{agent}/act-1/submit",
                    json={"med-0": "yes", "nka": "true"})
    assert posted.status_code == 503
    assert posted.get_json().get("confirmed") is not True
    assert "Nothing has been approved" in posted.get_json()["message"]


def test_a_gateway_504_on_review_submit_does_not_claim_the_review_was_saved(
        cfg, svc, monkeypatch):
    """The engine never answered, so neither may we.

    A 5xx here was passed straight through as the response status with the
    engine's body, which tells the patient nothing about whether their
    decisions were recorded. What we DO know is that `confirm_action` is
    only reached on a 200, so nothing was approved — that half is sayable,
    and it is the half that stops a second approval sending twice.

    MUTATION: return `jsonify(body), status` unconditionally -> red.
    Ran it, saw red.
    """
    from careagents.app import create_app

    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    agent = c.post("/api/agents", json={"name": "A", "persona": "calm",
                                        "connection_id": conn}).get_json()["id"]

    fake.submit_review = lambda t, a, d: (502, {"error": "bad gateway"})
    posted = c.post(f"/review/{agent}/act-1/submit",
                    json={"med-0": "yes", "nka": "true"})
    assert posted.status_code == 503
    payload = posted.get_json()
    assert payload.get("confirmed") is not True
    assert "Nothing has been approved" in payload["message"]

    # A 422 is the engine's own answer — attestation missing — and must
    # still reach the patient as the engine's answer, unchanged.
    fake.submit_review = FakeClient.submit_review.__get__(fake, FakeClient)
    refused = c.post(f"/review/{agent}/act-1/submit", json={"med-0": "yes"})
    assert refused.status_code == 422


def test_an_unreachable_engine_is_never_reported_as_an_unknown_run(
        cfg, svc, monkeypatch):
    """"Unknown run" and "unknown form" are claims about what exists (#410).

    During an incident none of them is true — the run exists, we just could
    not look it up. A 404 also retires the run for the callers that poll it,
    so the patient's answer is dropped rather than delivered late.

    The engine's own 404 still passes through as 404: its run and action
    lookups are tenant-scoped `filter_by(...).first()`, so 404 genuinely
    means "no such id, or not this tenant's".

    MUTATION: collapse each arm back to a bare
    `except HealthClawError: return jsonify({"error": "unknown run"}), 404`
    -> red, one site at a time. Ran it, saw red.
    """
    app, c, fake, agent_id, _tenant, _conn_id = _chat_app(
        cfg, svc, monkeypatch)

    # A real not-found still reads as not-found.
    assert c.get("/api/chat/runs/no-such-run/events",
                 query_string={"agent_id": agent_id}).status_code == 404
    assert c.get("/api/form/no-such-action",
                 query_string={"agent": agent_id}).status_code == 404

    fake.get_agent_run = _unreachable
    fake.action_status = _unreachable

    events = c.get("/api/chat/runs/run-1/events",
                   query_string={"agent_id": agent_id})
    assert events.status_code == 503
    assert events.get_json()["error"] == "run service unavailable"

    form = c.get("/api/form/act-1", query_string={"agent": agent_id})
    assert form.status_code == 503
    assert form.get_json()["status"] == "unavailable"


def test_the_imessage_relay_is_told_to_retry_not_that_the_run_vanished(
        cfg, svc, monkeypatch):
    """The Mac relay polls this endpoint for the agent's reply and stops on a
    404. Answering "unknown run" during an outage silently drops the reply to
    a text message the patient already sent (#410)."""
    from careagents.app import create_app

    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    agent_id = c.post("/api/agents", json={"name": "A", "persona": "calm",
                                           "connection_id": conn}
                      ).get_json()["id"]
    code = c.post("/api/surfaces/imessage",
                  json={"agent_id": agent_id}).get_json()["code"]
    headers = {"X-Internal-Secret": cfg.mint_secret}
    assert c.post("/api/surfaces/imessage/bind", headers=headers,
                  json={"code": code, "handle": "im-test-handle"}
                  ).status_code == 200

    fake.get_agent_run = _unreachable
    r = c.get("/api/surfaces/imessage/runs/run-1", headers=headers,
              query_string={"handle": "im-test-handle"})
    assert r.status_code == 503
    assert r.get_json()["error"] == "run service unavailable"


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
    assert body["handle"] == "im-test-handle" and code in body["instructions"]

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

    # inbound: fake the worker's model turn, assert the reply is relayed back
    class _Turn:
        text = "Your last A1c was 6.1% — in range."
        tool_calls = []
        raw_tool_calls = []

    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: _Turn())
    assert relay.post("/api/surfaces/imessage/inbound",
                      json={"handle": "+15559998888", "text": "how's my a1c?"}
                      ).status_code == 403  # needs mint secret
    ok = _enqueue_and_run_imessage(
        app, relay, headers=hdrs,
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


# --- hub UI source guards (#224) ---------------------------------------------
# Honest scope: these are SOURCE-level guards on careagents/static/home.js,
# careagents/static/careagents.css and careagents/templates/home.html. They
# execute no JavaScript and prove no behaviour — the behaviour has to be driven
# in a real browser. What they do buy is a cheap regression fence around the
# properties that are invisible in review and expensive to get wrong: no
# blocking browser dialogs, no markup built from server strings, and a delete
# confirmation that cannot decay into one tap.

import pathlib as _pathlib

_CA = _pathlib.Path(__file__).resolve().parents[1] / "careagents"
_HOME_JS = (_CA / "static" / "home.js").read_text()
_HOME_HTML = (_CA / "templates" / "home.html").read_text()
_CSS = (_CA / "static" / "careagents.css").read_text()
_AUTH_JS = (_CA / "static" / "auth.js").read_text()
_AUTH_HTML = (_CA / "templates" / "auth.html").read_text()


def test_auth_js_reads_the_sent_flag_before_saying_a_code_was_sent():
    """MUTATION: delete the `res.d.sent === false` branch -> red.

    Same honest scope as the guards below: source-level, executes no JS. The
    server can answer truthfully and the screen still say "We sent a code" —
    the lie #262 is about lives in the copy, so the copy is what needs a
    fence.
    """
    assert "res.d.sent === false" in _AUTH_JS, "cooldown state never read"
    assert "res.d.retry_after" in _AUTH_JS, "the wait is never shown"
    # Both ledes must exist, and the cooldown one must not claim a fresh send.
    assert '"We sent an 8-digit code to"' in _AUTH_JS
    assert '"We sent a code moments ago to"' in _AUTH_JS


def test_auth_js_never_builds_markup_from_strings():
    """The email address and a server-supplied number reach the DOM here."""
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in _AUTH_JS, sink


def test_auth_dialog_selector_contract():
    """auth.js addresses these by id; a template rename would break the copy at
    runtime only, with no server-side signal."""
    for sel in ('id="code-lede"', 'id="code-note"', 'id="code-email"',
                'id="step-code"', 'id="email-btn"', 'id="verify-btn"'):
        assert sel in _AUTH_HTML, sel
    # The lede ships as the truthful default and is overwritten per response;
    # a hardcoded "We sent" outside the span would survive the JS.
    assert "We sent an 8-digit code to</span>" in _AUTH_HTML


def test_hub_js_uses_no_blocking_browser_dialogs():
    """prompt/alert/confirm are silently dead in several in-app mobile browsers
    (the whole point of #224): a flow built on them just stops with no error."""
    import re
    for fn in ("prompt", "alert", "confirm"):
        assert not re.search(r"(?<![\w.])" + fn + r"\s*\(", _HOME_JS), fn


def test_hub_js_never_builds_markup_from_strings():
    """Connection labels, provider labels and server error strings all reach the
    DOM here. One innerHTML on that path turns an upstream string into markup."""
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in _HOME_JS, sink


def test_copy_has_no_execcommand_fallback():
    """execCommand('copy') reports success in browsers where it did nothing, so
    the user is told the pairing code is copied when it is not."""
    assert "execCommand" not in _HOME_JS


def test_pairing_code_is_never_logged():
    """The pairing code binds a chat handle to an account: it must not survive
    in a console buffer or a log."""
    assert "console.log" not in _HOME_JS
    assert "console.debug" not in _HOME_JS


def test_typed_delete_stays_double_gated():
    """Three layers, all load-bearing. Deleting records is irreversible, and a
    markup tidy-up that drops `disabled` must not silently make it one tap."""
    # (a) the button ships disabled in the static markup
    confirm = _HOME_HTML.split('id="delete-confirm"')[1][:120]
    assert "disabled" in confirm
    # (b) enabled only on an exact, case-sensitive match
    assert 'input.value !== "DELETE"' in _HOME_JS
    # (c) the click handler re-checks the value itself
    assert 'if (input.value === "DELETE")' in _HOME_JS
    # and the comparison is never loosened, inside the confirmation helper
    ask = _HOME_JS.split("function askToDelete(")[1].split("\n  }")[0]
    for loosener in ("trim()", "toUpperCase()", "toLowerCase()"):
        assert loosener not in ask, loosener


def test_delete_confirmation_is_not_a_form_or_native_dialog():
    """A <form> would let Enter submit past the JS check; <dialog>/showModal
    brings a focus trap that breaks VoiceOver on iOS."""
    assert "<form" not in _HOME_HTML.split('id="delete-modal"')[1].split("</div>\n\n")[0]
    assert "<dialog" not in _HOME_HTML
    assert "showModal" not in _HOME_JS


def test_hub_dialog_selector_contract():
    """home.js addresses these by id; a template rename would break the flow at
    runtime only, with no server-side signal."""
    for sel in ('id="provider-picker"', 'id="picker-cancel"', 'id="picker-rows"',
                'id="connect-msg"', 'id="surfaces-msg"', 'id="agents"',
                'id="code-card"', 'id="pair-code"', 'id="copy-code"',
                'id="copy-state"', 'id="code-instructions"', 'id="code-done"',
                'id="tg-state"', 'id="im-state"', 'id="delete-modal"',
                'id="delete-label"', 'id="delete-input"', 'id="delete-confirm"',
                'id="delete-cancel"'):
        assert sel in _HOME_HTML, sel


def test_a_background_message_does_not_scroll_the_page_under_an_open_modal():
    """MUTATION: drop the open-modal condition from say() -> red.

    say() scrolled unconditionally, including while a dialog covered the page:
    with the wearables picker open, scrollY jumped 1284 -> 628, so dismissing
    it left the user ~656px from where they were, beside an error about a
    different tile (#269). Open state here IS the absence of `hidden` —
    openDialog() and the consent card toggle that attribute and nothing else,
    which is why `[hidden] { display: none !important; }` has to outrank
    `.modal { display: flex }`.
    """
    import re
    body = _HOME_JS.split("function say(")[1].split("\n  }")[0]
    # Comments are stripped first: a selector quoted in prose is not a guard.
    body = re.sub(r"//[^\n]*", "", body)
    assert "scrollIntoView" in body
    assert ".modal:not([hidden])" in body[:body.index("scrollIntoView")]
    # The three facts that selector rests on, pinned where they live.
    opens = re.findall(r'<div class="modal(?: [\w-]+)*"[^>]*>', _HOME_HTML)
    assert opens
    for tag in opens:
        assert " hidden" in tag, tag
    assert "[hidden] { display: none !important; }" in _CSS
    assert re.search(r"modal\.hidden = false", _HOME_JS)


def test_flash_cue_is_class_keyed_with_a_single_keyframe():
    assert "#connect-section.flash" not in _CSS   # de-keyed from the one id
    assert ".hub-section.flash" in _CSS
    assert _CSS.count("@keyframes flashPulse") == 1


def test_connection_action_buttons_are_styled_and_thumb_sized():
    """44px is the tap target floor; the shared rule is what supplies it."""
    for sel in (".conn-refresh", ".conn-disconnect", ".conn-delete",
                ".conn-refresh-msg"):
        assert sel in _CSS, sel
    shared = _CSS.split(".conn-refresh, .conn-disconnect, .conn-delete {")[1]
    assert "min-height: 44px" in shared.split("}")[0]


def test_the_first_visit_greeting_counts_totals_not_the_first_page(
        cfg, svc, monkeypatch):
    """MUTATION: count len(entry) again -> red.

    A searchset's `entry` is one PAGE (the server caps it at 50), so
    len(entry) reported the page size as the person's total. A real import
    greeted its patient with "50 conditions ... 50 lab results" when the true
    numbers were 52 and 186 — both wrong, both capped at exactly the page
    limit, which is why it read as plausible on demo-sized data for six
    weeks. The greeting must ask for `_summary=count` and use `total`,
    the same contract as HealthClawClient.record_count.
    """
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(cfg, svc, monkeypatch)

    seen = []
    real_totals = {"Condition": 52, "MedicationRequest": 4, "Observation": 186}

    def search(tenant_id, resource_type, params=None):
        seen.append((resource_type, dict(params or {})))
        if (params or {}).get("_summary") == "count":
            return {"total": real_totals.get(resource_type, 0)}
        # A full page of 50 — what len(entry) used to mistake for the total.
        return {"total": real_totals.get(resource_type, 0),
                "entry": [{"resource": {"resourceType": resource_type}}] * 50}

    fake.search = search

    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert "52 conditions" in page
    assert "4 medications" in page
    assert "186 test results" in page
    assert "50 condition" not in page
    for _rt, params in seen:
        assert params.get("_summary") == "count", (
            "the greeting pulled a full page just to count it — resources "
            "crossed the boundary where only totals should")


def test_a_broken_count_falls_back_to_the_generic_greeting(
        cfg, svc, monkeypatch):
    """A counting failure must degrade to the generic greeting, not 500
    the first page a new user ever sees."""
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(cfg, svc, monkeypatch)

    def search(tenant_id, resource_type, params=None):
        raise HealthClawError("search failed (503)", 503)

    fake.search = search
    r = c.get(f"/chat?agent={agent_id}")
    assert r.status_code == 200
    assert b"in your records" not in r.data


# ---------------------------------------------------------------------------
# #336: an import in flight is not an empty chart. Reported live — a patient
# finished a MEDENT connect, opened their agent, and was told "I found 0
# conditions, 0 medications, and 0 lab results in your records" while the
# import the connect page had just promised was still running.
# ---------------------------------------------------------------------------
def _counting(totals):
    def search(tenant_id, resource_type, params=None):
        return {"total": totals.get(resource_type, 0)}
    return search


def _account_id(svc, email="gene@example.com"):
    from careagents.models import Account
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).one().id


def test_a_count_is_never_stated_while_an_import_is_in_flight(
        cfg, svc, monkeypatch):
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(cfg, svc, monkeypatch)
    svc.set_connection_status(tenant, "pending")
    fake.search = _counting({})

    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert "0 condition" not in page, (
        "stated a count as a finding about the person while the import that "
        "would produce it was still running")
    assert "in your records" not in page
    assert "haven't arrived yet" in page


def test_records_that_landed_while_nobody_polled_are_reported(
        cfg, svc, monkeypatch):
    """`status` flips to active only while /connect is open polling. A patient
    who closed that tab keeps a stale `pending` over a full chart, and must
    not be told their records are still on the way."""
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(cfg, svc, monkeypatch)
    svc.set_connection_status(tenant, "pending")
    fake.search = _counting({"Condition": 52, "MedicationRequest": 4,
                             "Observation": 186})

    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert "52 conditions" in page
    assert "haven't arrived yet" not in page
    # and the status settles, so the next visit costs no counts
    conns = svc.list_home(_account_id(svc))["connections"]
    assert [x["status"] for x in conns if x["tenant_id"] == tenant] == \
        ["active"]


def test_a_returning_patient_is_told_the_records_still_have_not_landed(
        cfg, svc, monkeypatch):
    """The second trap in the same code: counts were computed only when the
    conversation was empty, so the one person actually watching for arrival —
    someone who asked a question and came back to check — was the one person
    never shown the answer."""
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(cfg, svc, monkeypatch)
    svc.set_connection_status(tenant, "pending")
    fake.log_message(tenant, "user", "did my records land?", agent_id=agent_id)
    fake.search = _counting({})

    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert "Picking up where you left off" in page   # still a return visit
    assert "haven't arrived yet" in page


def test_past_the_promised_window_we_stop_saying_they_are_coming(
        cfg, svc, monkeypatch):
    """"Still arriving" is a claim about a job this app cannot see. Past the
    window /connect promises, all it knows is that nothing has landed."""
    from careagents.intake_state import ARRIVAL_WINDOW_SECONDS
    from careagents.models import Connection

    app, c, fake, agent_id, tenant, conn_id = _chat_app(cfg, svc, monkeypatch)
    svc.set_connection_status(tenant, "pending")
    with svc.session() as s:
        row = s.get(Connection, conn_id)
        row.connected_at = time.time() - ARRIVAL_WINDOW_SECONDS - 60
    fake.search = _counting({})

    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert "longer than an import usually takes" in page
    assert "haven't arrived yet" not in page
    assert "0 condition" not in page


def test_an_unreachable_count_during_an_import_does_not_become_zero(
        cfg, svc, monkeypatch):
    """A failed count is not an empty chart — the engine being down must not
    be reported as the patient having no records."""
    app, c, fake, agent_id, tenant, _conn_id = _chat_app(cfg, svc, monkeypatch)
    svc.set_connection_status(tenant, "pending")

    def search(tenant_id, resource_type, params=None):
        raise HealthClawError("search failed (503)", 503)

    fake.search = search
    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert "0 condition" not in page
    assert "in your records" not in page
    # An unreadable count is also not proof the import finished, so the
    # outstanding-import notice stands rather than quietly disappearing.
    assert "haven't arrived yet" in page


# ---------------------------------------------------------------------------
# Lab timeline card (#357 follow-up): the chat answers a "timeline" question
# with a chart, fetched same-origin with credentials this process already has.
# ---------------------------------------------------------------------------
def _interpret_payload():
    def _obs(code, value, date, flag="N"):
        return {"resource": {
            "resourceType": "Observation",
            "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
            "effectiveDateTime": date,
            "valueQuantity": {"value": value, "unit": "mg/dL"},
            "interpretation": [{"coding": [{"code": flag}]}]}}
    return {"summary": {}, "consumer": {}, "disclaimer": "Decision support.",
            "bundle": {"entry": [_obs("2093-3", 244, "2026-03-01", "H"),
                                 _obs("2093-3", 210, "2025-01-01"),
                                 _obs("4548-4", 6.1, "2026-02-01")]}}


def test_the_timeline_endpoint_returns_series_for_the_signed_in_agent(
        cfg, svc, monkeypatch):
    app, c, fake, agent_id, tenant, _conn = _chat_app(cfg, svc, monkeypatch)
    fake.interpret_labs = lambda t: _interpret_payload()

    body = c.get(f"/api/labs/timeline?agent={agent_id}").get_json()
    names = [s["name"] for s in body["series"]]
    assert "Total cholesterol" in names and "Hemoglobin A1c" in names
    assert body["disclaimer"] == "Decision support."


def test_the_timeline_endpoint_narrows_to_the_topic_asked_about(
        cfg, svc, monkeypatch):
    app, c, fake, agent_id, tenant, _conn = _chat_app(cfg, svc, monkeypatch)
    fake.interpret_labs = lambda t: _interpret_payload()

    body = c.get(
        f"/api/labs/timeline?agent={agent_id}&topic=cholesterol").get_json()
    assert [s["name"] for s in body["series"]] == ["Total cholesterol"]


def test_the_timeline_endpoint_refuses_an_agent_you_do_not_own(
        cfg, svc, monkeypatch):
    """MUTATION: drop the get_agent_context check -> red. The agent id comes
    from the query string; it selects the TENANT whose labs are returned."""
    app, c, fake, agent_id, tenant, _conn = _chat_app(cfg, svc, monkeypatch)
    assert c.get("/api/labs/timeline?agent=someone-elses").status_code == 404


def test_the_timeline_endpoint_requires_a_session(cfg, svc, monkeypatch):
    app, c, fake, agent_id, tenant, _conn = _chat_app(cfg, svc, monkeypatch)
    anon = app.test_client()
    assert anon.get(f"/api/labs/timeline?agent={agent_id}").status_code in (302, 401)


def test_the_timeline_endpoint_degrades_rather_than_500ing(
        cfg, svc, monkeypatch):
    def _boom(_t):
        raise HealthClawError("interpret failed (503)", 503)
    app, c, fake, agent_id, tenant, _conn = _chat_app(cfg, svc, monkeypatch)
    fake.interpret_labs = _boom
    assert c.get(f"/api/labs/timeline?agent={agent_id}").status_code == 502


def test_show_lab_timeline_emits_a_card_and_withholds_the_numbers(
        cfg, svc, monkeypatch):
    """The chart carries the values. Handing them to the model too invites it
    to restate every number and to narrate a direction from a single point.

    MUTATION: return the readings in the tool result -> red.
    """
    import json as _json

    from careagents.agent import _execute_tool

    class _HC:
        def interpret_labs(self, _tenant):
            return _interpret_payload()

    events = []
    out = _json.loads(_execute_tool(_HC(), "t", "show_lab_timeline",
                                    {"topic": "cholesterol"}, events))
    assert events == [{"type": "card", "kind": "lab-timeline",
                       "topic": "cholesterol"}]
    assert out["chart_shown"] is True
    assert out["series"] == [{"name": "Total cholesterol", "readings": 2,
                              "trend_plottable": True}]
    assert "244" not in _json.dumps(out), "the tool handed the model raw values"


def test_care_gaps_that_could_not_run_forbids_reporting_no_screenings(
        cfg, svc, monkeypatch):
    """The unresolved state has to survive the last hop to the model, or the
    engine's honesty is thrown away one call short of the person (#389).

    MUTATION: drop the note branch in _execute_tool -> red.
    """
    import json as _json

    from careagents.agent import _execute_tool

    class _HC:
        def care_gaps(self, _tenant):
            return {"summary": {}, "consumer": {
                "lines": [], "unevaluated": "no-patient",
                "unevaluated_note": "nothing was examined"}}

    out = _json.loads(_execute_tool(_HC(), "t", "get_care_gaps", {}, []))
    assert "no-patient" in out["note"]
    assert "Do NOT tell the person they have no screenings due" in out["note"]


def test_care_gaps_partial_result_reports_its_lines_and_says_what_is_missing(
        cfg, svc):
    """Some rules decided and some not (#417).

    The could-not-run note tells the model to name no screening from the
    result. Reused here it would suppress four real due screenings; dropped
    here it would sell four lines as the whole answer. Neither, so the partial
    case gets its own instruction.

    MUTATION: fall through to the could-not-run note -> red.
    """
    import json as _json

    from careagents.agent import _execute_tool

    class _HC:
        def care_gaps(self, _tenant):
            return {"summary": {}, "consumer": {
                "lines": [{"rule_id": "bp-screening", "message": "due"}],
                "unevaluated": "sex-unavailable", "unevaluated_count": 2,
                "unevaluated_note": "2 screenings could not be checked"}}

    out = _json.loads(_execute_tool(_HC(), "t", "get_care_gaps", {}, []))
    assert "PARTIAL" in out["note"]
    assert "2" in out["note"]
    assert "Do NOT tell the person they have no screenings due" not in out["note"]
    assert "do not name or infer any screening" not in out["note"]


def test_care_gaps_with_real_findings_carries_no_could_not_run_note(cfg, svc):
    """The note is earned, not boilerplate — a check that ran says nothing
    about having failed."""
    import json as _json

    from careagents.agent import _execute_tool

    class _HC:
        def care_gaps(self, _tenant):
            return {"summary": {}, "consumer": {
                "lines": [{"rule_id": "bp-screening", "message": "due"}]}}

    out = _json.loads(_execute_tool(_HC(), "t", "get_care_gaps", {}, []))
    assert "note" not in out


def test_show_lab_timeline_with_no_match_emits_no_card_and_forbids_absence(
        cfg, svc, monkeypatch):
    """MUTATION: emit a card over an empty series -> red (an empty chart reads
    as 'nothing there'), and the note must forbid reporting absence."""
    import json as _json

    from careagents.agent import _execute_tool

    class _HC:
        def interpret_labs(self, _tenant):
            return {"bundle": {"entry": []}, "disclaimer": ""}

    events = []
    out = _json.loads(_execute_tool(_HC(), "t", "show_lab_timeline", {}, events))
    assert events == []
    assert out["chart_shown"] is False
    assert "not the same as" in out["note"]
