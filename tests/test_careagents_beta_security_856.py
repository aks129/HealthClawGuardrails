"""Security tester probes for PR #856 (beta stage 1), head 5f8d7f2.

Tests named `test_exploit_*` are xfail(strict=True): each pins a gap the
security review found, and goes red (XPASS) the day the gap is closed, so
whoever closes it flips the marker. The others pin behaviour the review
confirmed and must stay green.
"""

from __future__ import annotations

import threading
import time


from careagents.models import Connection, RealRecordInvite
from tests.careagents_consent_helpers import consented
from tests.careagents_stage1_helpers import approve_terms, pend_terms
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, cfg, svc)

EMAIL = "gene@example.com"


def _real_app(cfg, svc, monkeypatch, email=EMAIL):  # noqa: F811
    # Connected before the terms bump: the tests below approve a
    # newer version and expect this one to be stale.
    pend_terms(monkeypatch)
    from careagents.app import create_app
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    conn = c.post("/api/connections/fasten", json=consented()).get_json()
    svc.set_connection_status(fake.tenants[-1], "active")
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    return app, c, fake, agent_id, conn["id"]


# --- pause scope -------------------------------------------------------------

def test_exploit_paused_account_still_executes_a_pending_action(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, _ = _real_app(cfg, svc, monkeypatch)
    executed = []
    monkeypatch.setattr(fake, "confirm_action",
                        lambda t, a: executed.append(a) or {"status": "ok"})
    assert svc.set_paused(EMAIL, True)
    r = c.post(f"/review/{agent_id}/act-1/submit", data={"nka": "on"})
    assert executed == [], (
        f"paused account executed an action: {r.status_code} {executed}")
    # Fixed after the review: refused with the hub's pause sentence, and the
    # form itself is not served while paused.
    from careagents import beta
    assert r.status_code == 423
    assert r.get_json()["message"] == beta.PAUSED_HUB_TEXT
    page = c.get(f"/review/{agent_id}/act-1")
    assert page.status_code == 423
    assert "Your records are paused" in page.get_data(as_text=True)


def test_paused_account_still_reads_its_own_brief_and_labs(
        cfg, svc, monkeypatch):  # noqa: F811
    """Scope note, not a bypass: these engine reads are deterministic (no
    model) and audited engine-side. Pinned so the gap is visible."""
    app, c, fake, agent_id, _ = _real_app(cfg, svc, monkeypatch)
    reads = []
    monkeypatch.setattr(fake, "interpret_labs",
                        lambda t: reads.append("labs") or {"bundle": {}})
    svc.set_paused(EMAIL, True)
    assert c.get(f"/api/labs/timeline?agent={agent_id}").status_code == 200
    assert reads == ["labs"]


# --- re-consent --------------------------------------------------------------

def test_exploit_revoked_connection_is_reconsented(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, conn_id = _real_app(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    with svc.session() as s:
        s.get(Connection, conn_id).status = "revoked"
    r = c.post(f"/api/connections/{conn_id}/consent", json=consented())
    assert r.status_code == 404, r.get_json()
    # Fixed after the review, in the route and in the service.
    with svc.session() as s:
        assert s.get(Connection, conn_id).consent_version == "2026-08-01"
    from tests.test_careagents_beta_reconsent import _account_id
    assert svc.record_consent(_account_id(svc), conn_id, "2026-10-01") is False


def test_reconsent_across_accounts_and_on_sample_is_refused(
        cfg, svc, monkeypatch):  # noqa: F811
    app, a, fake, _agent, a_conn = _real_app(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    b = app.test_client()
    _login(b, svc, monkeypatch, email="other@example.com")
    b_sample = b.post("/api/connections/sample").get_json()["id"]
    # B swaps in A's connection id
    r = b.post(f"/api/connections/{a_conn}/consent", json=consented())
    assert r.status_code == 404
    assert "consent_version" not in (r.get_json() or {})
    with svc.session() as s:
        assert s.get(Connection, a_conn).consent_version != "2026-10-01"
    # sample, own account
    r = b.post(f"/api/connections/{b_sample}/consent", json=consented())
    assert r.status_code == 404
    # no session at all
    anon = app.test_client()
    r = anon.post(f"/api/connections/{a_conn}/consent", json=consented())
    assert r.status_code in (302, 401, 403)


# --- off beats everything, including a pending Fasten row ---------------------

def test_off_refuses_reuse_of_a_pending_fasten_row(monkeypatch):
    from careagents.accounts import AccountService
    from careagents.app import create_app
    from tests.careagents_stage1_helpers import allowlist_cfg
    cfg_on = allowlist_cfg(CARE_REAL_RECORDS_ALLOWLIST=EMAIL)
    svc_ = AccountService(cfg_on)
    fake = FakeClient()
    app = create_app(config=cfg_on, client=fake, accounts=svc_)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc_, monkeypatch, email=EMAIL)
    first = c.post("/api/connections/fasten", json=consented())
    assert first.status_code == 200, first.get_json()
    monkeypatch.setattr(cfg_on, "real_records", "off")
    again = c.post("/api/connections/fasten", json=consented())
    assert again.status_code != 200
    assert "connect_url" not in (again.get_json() or {})


# --- the lease under real threads ----------------------------------------------

def test_concurrent_fasten_connects_make_one_row(tmp_path, monkeypatch):
    """Real threads against a file-backed SQLite database; the fake engine's
    connect URL sleeps so every request overlaps the first one's insert."""
    from careagents.accounts import AccountService
    from careagents.app import create_app
    from tests.careagents_stage1_helpers import allowlist_cfg
    db = tmp_path / "race.db"
    cfg_ = allowlist_cfg(CARE_DATABASE_URL=f"sqlite:///{db}",
                         CARE_REAL_RECORDS="on")
    svc_ = AccountService(cfg_)
    fake = FakeClient()
    orig = fake.fasten_connect_url

    def slow(*a, **k):
        time.sleep(0.3)
        return orig(*a, **k)
    monkeypatch.setattr(fake, "fasten_connect_url", slow)
    app = create_app(config=cfg_, client=fake, accounts=svc_)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc_, monkeypatch, email=EMAIL)
    cookie = c.get_cookie("session")
    codes = []

    def go():
        t = app.test_client()
        t.set_cookie("session", cookie.value)
        codes.append(t.post("/api/connections/fasten",
                            json=consented()).status_code)
    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with svc_.session() as s:
        rows = s.query(Connection).filter_by(kind="fasten").count()
    assert rows == 1, (rows, codes)
    assert codes.count(200) >= 1 and set(codes) <= {200, 409}, codes


# --- delete removes the invite --------------------------------------------------

def test_account_delete_removes_mixed_case_invite(cfg, svc, monkeypatch):  # noqa: F811
    """Invited with odd case and spaces, signed up, deleted: no invite row,
    no activity rows, and a fresh sign-up with the same email is not paused."""
    from careagents.app import create_app
    from careagents.models import Account, ActivityDay
    svc.invite_real_records("  Gene@Example.COM ", "ops-1")
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email="Gene@Example.com")
    with svc.session() as s:
        acct_id = s.query(Account).filter_by(email=EMAIL).one().id
    svc.count_activity(acct_id, "asked")
    svc.set_paused(EMAIL, True)
    assert svc.delete_account(acct_id)
    with svc.session() as s:
        assert s.get(RealRecordInvite, EMAIL) is None
        assert s.query(ActivityDay).filter_by(account_id=acct_id).count() == 0
    # Informational: re-sign-up after delete is a fresh, unpaused account.
    c2 = app.test_client()
    _login(c2, svc, monkeypatch, email=EMAIL)
    with svc.session() as s:
        assert s.query(Account).filter_by(email=EMAIL).one().real_paused_at is None


# --- round 2 (head 4b0decf): account-wide accept and the cap skip ------------

def test_account_wide_accept_never_reaches_another_account_or_revoked(
        cfg, svc, monkeypatch):  # noqa: F811
    """One accept covers the account's own live real connections only:
    not another account's, not a revoked one, not a still-connecting one."""
    app, a, fake, _agent, a_fasten = _real_app(cfg, svc, monkeypatch)
    a_direct = a.post("/api/connections/direct",
                      json=consented()).get_json()["id"]
    a_gone = a.post("/api/connections/direct",
                    json=consented()).get_json()["id"]
    b = app.test_client()
    _login(b, svc, monkeypatch, email="other@example.com")
    b_direct = b.post("/api/connections/direct",
                      json=consented()).get_json()["id"]
    b_pending = b.post("/api/connections/fasten",
                       json=consented()).get_json()["id"]
    with svc.session() as s:
        s.get(Connection, a_gone).status = "revoked"
    approve_terms(monkeypatch, "2026-10-01")
    # B anchors on A's connection: refused, nothing on either account moves.
    assert b.post(f"/api/connections/{a_fasten}/consent",
                  json=consented()).status_code == 404
    with svc.session() as s:
        assert {s.get(Connection, i).consent_version
                for i in (a_fasten, a_direct, b_direct)} == {"2026-08-01"}
    # A accepts from its Fasten connection.
    assert a.post(f"/api/connections/{a_fasten}/consent",
                  json=consented()).status_code == 200
    with svc.session() as s:
        v = {i: (s.get(Connection, i).consent_version,
                 s.get(Connection, i).reconsented_at)
             for i in (a_fasten, a_direct, a_gone, b_direct, b_pending)}
    assert v[a_fasten][0] == v[a_direct][0] == "2026-10-01"
    assert v[a_gone] == ("2026-08-01", None)          # revoked: untouched
    assert v[b_direct] == ("2026-08-01", None)        # other account
    assert v[b_pending] == ("2026-08-01", None)
    # B anchoring on its own pending row reaches only B's rows.
    b.post(f"/api/connections/{b_pending}/consent", json=consented())
    with svc.session() as s:
        assert s.get(Connection, a_gone).consent_version == "2026-08-01"


# Fixed in the #856 follow-up: the day's turn is charged in the worker,
# where the model is called, after turn_block passes.
def test_exploit_cap_skip_then_accept_reaches_model_uncharged(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents import agent as agent_mod
    from careagents.models import UsageDay
    from careagents.worker import RunWorker

    class _Turn:
        text, tool_calls, raw_tool_calls = "model answer", [], []
    calls = []
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: calls.append(1) or _Turn())
    app, c, fake, agent_id, conn_id = _real_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(cfg, "chat_turns_per_day", 1)
    approve_terms(monkeypatch, "2026-10-01")       # connection now stale
    for i in range(3):                              # admitted, never charged
        r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                      "request_id": f"race-{i}"},
                   buffered=False)
        assert r.status_code == 200
        r.close()
    assert c.post(f"/api/connections/{conn_id}/consent",
                  json=consented()).status_code == 200
    w = RunWorker(cfg, fake, svc, "race-worker")
    while w.run_once():
        pass
    with svc.session() as s:
        used = sum(int(u.turns or 0) for u in s.query(UsageDay).all())
    assert len(calls) <= used <= 1, (
        f"{len(calls)} model calls against a cap of 1, {used} charged")
    # The two turns past the cap are answered with the limit sentence.
    from careagents import beta
    answers = [row["content"] for rows in fake.logged.values()
               for row in rows if row["role"] == "assistant"]
    assert answers.count(beta.DAILY_LIMIT_TEXT) == 2
