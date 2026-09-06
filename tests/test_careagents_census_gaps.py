"""Guards the 2026-09 census of `tests/test_careagents.py` found missing.

Every test here was written mutation-first: the production control it names
was deleted, the full 201-test `tests/test_careagents.py` suite stayed green,
and only the test below went red. Each docstring records that mutation so the
guard can be re-verified rather than trusted.

Scope note, deliberately narrow: these are the controls the existing file
*names* but does not exercise. They are not a redesign of its coverage.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import requests as _requests
from sqlalchemy import LargeBinary, String, Text, inspect as sa_inspect

from careagents.models import Base
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, _login, _sink_code, _turn, cfg, svc)

_CA = pathlib.Path(__file__).resolve().parents[1] / "careagents"
_HOME_JS = (_CA / "static" / "home.js").read_text()
_CHAT_JS = (_CA / "static" / "chat.js").read_text()


# --- the non-negotiable: CareAgents stores no PHI ----------------------------

def _all_text_in_careagents_db(service) -> list[tuple[str, str, str]]:
    """Every text-ish value in every CareAgents table, as (table, column, value).

    The account layer is small and entirely non-PHI by design, so reading all
    of it is cheap and needs no allow-list to maintain.
    """
    out: list[tuple[str, str, str]] = []
    inspector = sa_inspect(service.engine)
    live = set(inspector.get_table_names())
    with service.session() as s:
        for table in Base.metadata.sorted_tables:
            if table.name not in live:
                continue
            columns = [c for c in table.columns
                       if isinstance(c.type, (String, Text, LargeBinary))]
            if not columns:
                continue
            for row in s.execute(table.select()):
                mapping = row._mapping
                for column in columns:
                    value = mapping.get(column.name)
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", "replace")
                    if isinstance(value, str) and value:
                        out.append((table.name, column.name, value))
    return out


def test_no_chat_turn_text_is_ever_written_to_a_careagents_table(
        cfg, svc, monkeypatch):  # noqa: F811
    """The half of `..._persisted_to_healthclaw_not_careagents` nobody checks.

    That guard asserts the transcript reached the fake HealthClaw. It never
    looks at CareAgents' own tables, so it is silent on the "not careagents"
    claim in its own name.

    MUTATION (careagents/app.py, in `api_chat` before
    `hc.claim_inbound_message`): write the turn into the account layer, e.g.
    set `Connection.label = text[:120]` for the run's tenant. All 201 tests in
    tests/test_careagents.py stay green; this one goes red.
    """
    secret = "my chest has been hurting since Tuesday"
    reply = "that is worth discussing with a clinician"
    _app, c, _fake, agent_id, _tenant, _conn = _chat_app(
        cfg, svc, monkeypatch, reply=reply)
    _turn(c, agent_id, secret)

    leaks = [(table, column, value)
             for table, column, value in _all_text_in_careagents_db(svc)
             if secret in value or reply in value]
    assert not leaks, (
        "chat transcript text reached CareAgents' own database; PHI-adjacent "
        f"data belongs only in the HealthClaw tenant: {leaks}")


# --- email-code auth ---------------------------------------------------------

def test_a_consumed_login_code_cannot_be_replayed(svc, monkeypatch):  # noqa: F811
    """`used=False` in `AccountService.verify_email_code`.

    The existing email-code tests cover the resend rotation and the
    burn-after-N-guesses counter, and both keep passing without this filter:
    the resend test is carried by `order_by(exp.desc())` picking the newer
    token, and the brute-force test by the independent `attempts` check. The
    filter's own job — refusing a code that was already spent successfully —
    is what nothing covers.

    MUTATION: drop `used=False` from the token query. 201 green; this red.
    """
    import careagents.mail as mailmod
    from careagents.accounts import AuthError

    codes: list[str] = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    svc.start_email_code("replay@example.com")
    code = codes[-1]

    assert svc.verify_email_code(
        "replay@example.com", code).email == "replay@example.com"
    with pytest.raises(AuthError):
        svc.verify_email_code("replay@example.com", code)


def test_an_expired_login_code_is_refused(svc, monkeypatch):  # noqa: F811
    """`EmailToken.exp >= time.time()` in `verify_email_code`.

    Nothing in tests/test_careagents.py advances a clock, so the 10-minute
    life of the consumer login code is unpinned: without this the code in a
    mailbox stays a valid credential indefinitely, until a fresh one is
    requested.

    MUTATION: drop the `.filter(EmailToken.exp >= time.time())` clause.
    201 green; this red.
    """
    import careagents.accounts as accounts_mod
    import careagents.mail as mailmod
    from careagents.accounts import CODE_TTL, AuthError

    codes: list[str] = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    svc.start_email_code("stale@example.com")
    code = codes[-1]

    real_time = accounts_mod.time.time
    monkeypatch.setattr(accounts_mod.time, "time",
                        lambda: real_time() + CODE_TTL + 1)
    with pytest.raises(AuthError):
        svc.verify_email_code("stale@example.com", code)


# --- cross-account refusal, against a real second account --------------------

def test_another_accounts_connection_is_refused_on_every_route(
        cfg, svc, monkeypatch):  # noqa: F811
    """The refresh / delete / disconnect guards pass ids that exist nowhere
    (`conn_someone_else`, `conn_not_mine`), so they prove a 404 for an unknown
    id and say nothing about ownership. Only the upload guard builds a real
    victim.

    MUTATION: drop `account_id` from the `filter_by` in
    `AccountService.get_connection` / `revoke_connection` /
    `delete_connection`. Today only
    `test_upload_rejects_cross_account_connection_with_404` goes red; the
    three tests named for "a connection you do not own" and "other people's
    connections" stay green. With this guard, all four go red.
    """
    from careagents.app import create_app

    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True

    victim = app.test_client()
    _login(victim, svc, monkeypatch, email="victim@example.com")
    victim_conn = victim.post("/api/connections/sample").get_json()["id"]

    attacker = app.test_client()
    _login(attacker, svc, monkeypatch, email="mallory@example.com")

    assert attacker.post(
        f"/api/connections/{victim_conn}/refresh").status_code == 404
    assert attacker.post(
        f"/api/connections/{victim_conn}/disconnect").status_code == 404
    assert attacker.delete(
        f"/api/connections/{victim_conn}").status_code == 404


def test_another_accounts_agent_is_refused_on_the_timeline_route(
        cfg, svc, monkeypatch):  # noqa: F811
    """`test_the_timeline_endpoint_refuses_an_agent_you_do_not_own` passes the
    literal id `someone-elses`, which belongs to no account at all, so it
    exercises the unknown-agent branch rather than the cross-account one. The
    agent id selects the TENANT whose labs are returned.

    MUTATION: drop `account_id` from the `filter_by` in
    `AccountService.get_agent_context`. That test stays green; this one goes
    red.
    """
    from careagents.app import create_app

    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True

    victim = app.test_client()
    _login(victim, svc, monkeypatch, email="victim2@example.com")
    conn = victim.post("/api/connections/sample").get_json()["id"]
    victim_agent = victim.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn}).get_json()["id"]

    attacker = app.test_client()
    _login(attacker, svc, monkeypatch, email="mallory2@example.com")
    assert attacker.get(
        f"/api/labs/timeline?agent={victim_agent}").status_code == 404
    assert attacker.get(
        f"/api/form/act-1?agent={victim_agent}").status_code == 404


# --- the worker's substitute for account scoping -----------------------------

def test_the_worker_refuses_a_run_whose_tenant_is_not_the_agents(
        cfg, svc, monkeypatch):  # noqa: F811
    """`get_worker_agent_context` resolves an agent by id ALONE — its docstring
    says the worker "verifies this result's tenant against the tenant on the
    claimed HealthClaw run before it handles any data". That verification is
    the worker's entire substitute for the browser's account scoping, and
    nothing exercises it.

    MUTATION (careagents/worker.py `_execute`): weaken
    `if context is None or context["tenant"] != tenant:` to
    `if context is None:`. 201 green; this red.

    Scoped honestly: this is an integrity check, not a PHI boundary. Every
    read in `_execute` uses `run["tenant_id"]`, and the engine will not create
    a run on a tenant the caller holds no step-up token for — so a mismatched
    run reads the CALLER's own tenant, not the victim's. What it borrows is
    the victim's agent identity (name, persona, advisor), and it lands the
    reply in a conversation keyed by the victim's agent id. `account_id` from
    the context is unused in worker.py, so nothing else re-derives the owner.
    """
    from careagents.worker import RunWorker

    _app, c, fake, agent_id, tenant, _conn = _chat_app(cfg, svc, monkeypatch)
    worker = RunWorker(cfg, fake, svc, "test-worker")

    foreign = "ca-someone-else"
    assert tenant != foreign

    # A complete run dict, so a worker without the check runs on rather than
    # tripping over a missing key — the failure has to come from the control.
    run = {"id": "run-x", "tenant_id": foreign, "agent_id": agent_id,
           "conversation_id": fake.conversation_id(agent_id),
           "message_id": "m-1", "surface": "web", "status": "running",
           "message": {"id": "m-1", "role": "user", "text": "hello"}}

    read_tenants: list[str] = []
    real_recent = fake.recent_messages

    def _recording(tenant_arg, *a, **k):
        read_tenants.append(tenant_arg)
        return real_recent(tenant_arg, *a, **k)

    monkeypatch.setattr(fake, "recent_messages", _recording)

    class _NoHeartbeat:
        @staticmethod
        def stop():
            return None

    # Two separate claims. The harm is the read: assert it never happened,
    # whatever the worker did afterwards. Then assert the refusal is the
    # named one, so a future crash cannot pass for a guard.
    raised: Exception | None = None
    try:
        worker._execute(run, _NoHeartbeat())
    except Exception as exc:  # noqa: BLE001 — the point is what got read
        raised = exc

    assert foreign not in read_tenants, (
        "the worker read the transcript of a tenant the claimed agent does "
        f"not belong to: {read_tenants}")
    assert isinstance(raised, ValueError), (
        f"expected the tenant-mismatch refusal, got {raised!r}")


def test_the_worker_sends_the_safety_core_to_the_model(
        cfg, svc, monkeypatch):  # noqa: F811
    """`test_every_persona_shares_the_safety_core` and
    `test_agent_with_advisor_gets_specialized_prompt` both call
    `system_prompt()` directly from the test. They prove the function composes
    the safety contract; nothing proves the runtime path uses it.

    MUTATION (careagents/worker.py `_execute`): replace the `system_prompt(...)`
    call with a bare `f"You are {agent['name']}, a personal care agent."`.
    201 green; this red — an agent answering a patient with no 911 rule, no
    "never claim no known allergies", and no truncation honesty.
    """
    from careagents import agent as agent_mod
    from careagents.personas import SAFETY_CORE

    prompts: list[str] = []

    class _Turn:
        def __init__(self):
            self.text, self.tool_calls, self.raw_tool_calls = "ok", [], []

    def _capture(_cfg, system, _history, _tools):
        prompts.append(system)
        return _Turn()

    _app, c, _fake, agent_id, _tenant, _conn = _chat_app(
        cfg, svc, monkeypatch)
    monkeypatch.setattr(agent_mod.llm, "complete", _capture)
    monkeypatch.setattr("careagents.worker.llm.complete", _capture)
    _turn(c, agent_id, "am I due for anything?")

    assert prompts, "the model was never called"
    assert SAFETY_CORE in prompts[0], (
        "the worker sent a system prompt without the safety core")


# --- hub + chat UI source guards ---------------------------------------------

_DIALOGS = ("prompt", "alert", "confirm")


def test_no_blocking_browser_dialog_survives_a_window_qualifier():
    """`test_hub_js_uses_no_blocking_browser_dialogs` matches
    `(?<![\\w.])confirm\\s*\\(`. The negative lookbehind excludes `.`, which is
    there to skip `foo.confirm`, but it also skips `window.confirm(` —
    the canonical spelling.

    MUTATION (careagents/static/home.js): replace
    `const agreed = await askToDelete(...)` with
    `const agreed = window.confirm("Delete these records?")`. 201 green,
    including the two guards named for exactly this; this one red.
    """
    for source, name in ((_HOME_JS, "home.js"), (_CHAT_JS, "chat.js")):
        for fn in _DIALOGS:
            bare = re.findall(r"(?<![\w.])" + fn + r"\s*\(", source)
            qualified = re.findall(
                r"\b(?:window|self|globalThis)\s*\.\s*" + fn + r"\s*\(",
                source)
            assert not bare and not qualified, (name, fn, bare + qualified)


def test_the_delete_flow_still_routes_through_the_typed_confirmation():
    """`test_typed_delete_stays_double_gated` checks that `askToDelete` is
    well-formed and that the markup ships `disabled`. It never checks that the
    delete handler CALLS it, so the same mutation above replaced the typed
    gate with a one-tap native dialog and left that guard green.
    """
    handler = _HOME_JS.split('querySelectorAll(".conn-delete")')[1]
    handler = handler.split("});")[0]
    assert "askToDelete(" in handler, (
        "the delete button no longer routes through the typed confirmation")


def test_chat_js_never_builds_markup_from_server_or_model_strings():
    """chat.js renders the model's answers, the patient's own messages and lab
    values, and has NO source guard at all — home.js and auth.js have seven
    between them.

    MUTATION (careagents/static/chat.js): change `n.textContent = text` to
    `n.innerHTML = text` in the `el()` helper. 201 green; this red.

    The one allowed `innerHTML` is the static three-dot typing indicator,
    which interpolates nothing.
    """
    allowed = '<i></i><i></i><i></i>'
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                 "setHTMLUnsafe", "document.write"):
        for line in _CHAT_JS.splitlines():
            if sink in line and allowed not in line:
                pytest.fail(f"chat.js builds markup from a string: {line!r}")


# --- the fake-client gap, closed for the chat path ---------------------------

def _to_requests(werkzeug_resp):
    r = _requests.Response()
    r.status_code = werkzeug_resp.status_code
    r._content = werkzeug_resp.get_data() or b""
    r.headers.update(werkzeug_resp.headers.to_wsgi_list())
    return r


def test_a_chat_turn_is_accepted_by_the_real_engine_end_to_end(
        cfg, svc, monkeypatch):  # noqa: F811
    """The chat path against a REAL `HealthClawClient` and a REAL engine app.

    `test_cross_layer_upload_actually_lands_in_the_engine` does this for the
    upload path and is the only cross-layer test in tests/test_careagents.py.
    Everything on the chat path — `claim_inbound_message`, `create_agent_run`,
    `claim_agent_run`, `finalize_agent_run`, `recent_messages` — has only ever
    met `FakeClient`, which accepts wire shapes the engine validates: the
    `_CONVERSATION_ID` / `_REQUEST_ID` charset regexes, the `agent_id` length
    bound, the step-up mint, and the conversation-belongs-to-another-agent 409.

    That is the "ids do not transfer between the two systems, and the
    rejection is silent" trap. It is not currently live — this test passes
    unmodified — and pinning it is what keeps it that way.
    """
    from careagents import agent as agent_mod
    from careagents.app import create_app
    from careagents.healthclaw import HealthClawClient
    from careagents.worker import RunWorker
    from main import create_app as engine_create_app
    from models import db

    monkeypatch.setenv("PUBLIC_TENANTS", "test-tenant,ca-xlchat")
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "mint-secret")
    monkeypatch.setenv("STEP_UP_SECRET", "dev-secret")
    monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    engine_app = engine_create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "LEGACY_BOOT_ON_CREATE": False,
    })
    with engine_app.app_context():
        db.create_all()
    ec = engine_app.test_client()

    class _Relay:
        """The CareAgents client's requests.Session, on the engine's WSGI."""

        @staticmethod
        def post(url, json=None, headers=None, timeout=None, data=None):
            return _to_requests(ec.post(url.replace("http://engine", ""),
                                        json=json, data=data,
                                        headers=headers or {}))

        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            return _to_requests(ec.get(url.replace("http://engine", ""),
                                       query_string=params or {},
                                       headers=headers or {}))

    real = HealthClawClient(base="http://engine", mint_secret="mint-secret")
    real.http = _Relay()
    real.new_tenant_id = lambda: "ca-xlchat"

    class _Turn:
        def __init__(self):
            self.text = "here you go"
            self.tool_calls, self.raw_tool_calls = [], []

    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: _Turn())

    app = create_app(config=cfg, client=real, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]

    worker = RunWorker(cfg, real, svc, "test-worker")
    worker.run_once()          # registers worker presence with the engine

    r = c.post("/api/chat", json={"agent_id": agent_id,
                                  "message": "hi there"}, buffered=False)
    worker.run_once()
    body = r.get_data(as_text=True)
    assert r.status_code == 200, body
    assert '"type": "done"' in body, body

    stored = real.recent_messages(
        "ca-xlchat", limit=20,
        conversation_id=real.conversation_id(agent_id))
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert stored[0]["content"] == "hi there"
    assert stored[1]["content"] == "here you go"
