"""The daily funnel and issues count (careagents/funnel.py), the owner page,
the digest, and the "Connect your records" next step.

Timestamps and integers only. Synthetic data only.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Float, Integer, String

from careagents import funnel, invites
from careagents.models import Base
from tests.careagents_stage1_helpers import allowlist_cfg
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, _login, _turn, cfg, svc)


def _row(svc, account_id):  # noqa: F811
    with svc.session() as s:
        row = s.get(funnel.FunnelAccount, account_id)
        return None if row is None else {
            m: getattr(row, m) for m in (*funnel.MILESTONES, "invited_by")}


def _acct_id(svc, email="gene@example.com"):  # noqa: F811
    from careagents.models import Account
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).one().id


def _today(svc):  # noqa: F811
    return funnel.daily(svc.session, days=1)["days"][0]


# --- the tables hold nothing but counts -------------------------------------

#: Every column the new tables may have, with its type. A new column is a
#: decision about what CareAgents keeps, so it is made here, on purpose.
_ALLOWED = {
    "ca_funnel_accounts": {
        "account_id": String, "invited_by": String,
        **{m: Float for m in funnel.MILESTONES}},
    "ca_issue_days": {"id": String, "day": String, "kind": String,
                      "count": Integer},
    "ca_issue_detail_days": {"id": String, "day": String, "kind": String,
                             "detail": String, "count": Integer},
    "ca_digest_days": {"day": String, "sent_at": Float},
    "ca_owner_alerts": {"hour": String, "sent_at": Float},
    "ca_sign_in_days": {"id": String, "account_id": String, "day": String,
                        "method": String, "count": Integer},
    "ca_invite_links": {"id": String, "account_id": String, "code": String,
                        "cap": Integer, "used": Integer, "created_at": Float,
                        "revoked_at": Float},
    "ca_invite_pending": {"mailbox_hash": String, "link_id": String,
                          "created_at": Float},
    "ca_invite_joins": {"mailbox_hash": String, "link_id": String,
                        "inviter_id": String, "joined_at": Float,
                        "real_records": String},
}


def test_new_tables_hold_only_their_listed_columns():
    for name, allowed in _ALLOWED.items():
        table = Base.metadata.tables[name]
        assert set(table.columns.keys()) == set(allowed), name
        for col in table.columns:
            assert isinstance(col.type, allowed[col.name]), (name, col.name)
            if isinstance(col.type, String):
                # Ids, codes, days, kinds, emails: never room for a message.
                assert col.type.length and col.type.length <= 255


def test_no_message_text_reaches_the_new_tables(cfg, svc, monkeypatch):  # noqa: F811
    from tests.test_careagents_census_gaps import _all_text_in_careagents_db
    canary = "CANARY-FUNNEL-7731"
    app, c, fake, agent_id, tenant, _ = _chat_app(
        cfg, svc, monkeypatch, reply=f"answer {canary}")
    _turn(c, agent_id, f"question {canary}")
    c.post("/feedback", json={"stuck": canary})
    funnel.count_issue(svc.session, "chat_error")
    assert _row(svc, _acct_id(svc))["first_answer"]
    hits = [(t, col) for t, col, v in _all_text_in_careagents_db(svc)
            if canary in v and t in _ALLOWED]
    assert hits == []


# --- writing ----------------------------------------------------------------

def test_a_milestone_is_stamped_once(svc, monkeypatch):  # noqa: F811
    from tests.test_careagents import _make_account
    acct = _make_account(svc, monkeypatch, "once@example.com")
    assert _row(svc, acct.id)["joined"]                 # made at sign-up
    assert funnel.mark(svc.session, acct.id, "first_answer", at=100.0)
    assert not funnel.mark(svc.session, acct.id, "first_answer", at=200.0)
    assert _row(svc, acct.id)["first_answer"] == 100.0
    with pytest.raises(ValueError):
        funnel.mark(svc.session, acct.id, "message_text")


def test_an_account_from_before_the_table_gets_its_joined_filled(
        svc, monkeypatch):  # noqa: F811
    from tests.test_careagents import _make_account
    acct = _make_account(svc, monkeypatch, "old@example.com")
    with svc.session() as s:
        s.query(funnel.FunnelAccount).delete()
    funnel.mark(svc.session, acct.id, "feedback_sent")
    row = _row(svc, acct.id)
    assert row["joined"] and row["feedback_sent"]


def test_issues_are_counted_by_kind_and_day(svc):  # noqa: F811
    day = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)
    for _ in range(3):
        funnel.count_issue(svc.session, "mail_failed", day)
    funnel.count_issue(svc.session, "chat_error", day)
    with svc.session() as s:
        rows = {(r.day, r.kind): r.count
                for r in s.query(funnel.IssueDay).all()}
    assert rows == {("2026-10-01", "mail_failed"): 3,
                    ("2026-10-01", "chat_error"): 1}
    with pytest.raises(ValueError):
        funnel.count_issue(svc.session, "something said in chat")


def test_a_counter_that_fails_never_fails_the_request(cfg, svc, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise RuntimeError("database gone")
    monkeypatch.setattr(funnel, "count_issue", boom)
    monkeypatch.setattr(funnel, "mark", boom)
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    assert _turn(c, agent_id, "hi").status_code == 200
    r = c.post("/api/auth/verify", json={"email": "x@example.com",
                                         "code": "00000000"})
    assert r.status_code == 400


# --- where each milestone and issue is recorded -----------------------------

def test_sign_up_answer_and_issues_are_recorded(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    aid = _acct_id(svc)
    assert _row(svc, aid)["joined"] and not _row(svc, aid)["first_answer"]
    _turn(c, agent_id, "hi")
    assert _row(svc, aid)["first_answer"]
    # A wrong code is a failed sign-in.
    c.post("/api/auth/verify", json={"email": "gene@example.com",
                                     "code": "00000000"})
    # The burst limiter answers 429.
    monkeypatch.setattr(cfg, "chat_turns_per_window", 0)
    assert c.post("/api/chat", json={"agent_id": agent_id,
                                     "message": "hi"}).status_code == 429
    today = _today(svc)
    assert today["joined"] >= 1 and today["first_answer"] == 1
    assert today["sign_in_failed"] == 1 and today["rate_limited"] == 1


def test_a_failed_run_counts_a_chat_error(cfg, svc, monkeypatch):  # noqa: F811
    from careagents import agent as agent_mod
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)

    def die(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(agent_mod.llm, "complete", die)
    _turn(c, agent_id, "hi")
    assert _today(svc)["chat_error"] == 1
    assert not _row(svc, _acct_id(svc))["first_answer"]


def test_a_proposed_and_an_approved_action_are_recorded(
        cfg, svc, monkeypatch):  # noqa: F811
    """On made-up records too: most testers never connect real ones. `nka`
    is the synthetic review form's own attestation input, the human's
    answer, as in the existing relay tests."""
    from careagents import agent as agent_mod
    from careagents.llm import LLMTurn, ToolCall
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    seq = iter([LLMTurn(tool_calls=[ToolCall("1", "start_intake_form", {})]),
                LLMTurn(text="I've drafted your intake form.")])
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: next(seq))
    _turn(c, agent_id, "fill my intake form")
    aid = _acct_id(svc)
    assert _row(svc, aid)["action_proposed"]
    assert not _row(svc, aid)["action_approved"]
    r = c.post(f"/review/{agent_id}/act-1/submit",
               json={"med-0": "yes", "nka": "true"})
    assert r.status_code == 200 and r.get_json()["confirmed"] is True
    assert _row(svc, aid)["action_approved"]


def test_feedback_is_recorded(cfg, svc, monkeypatch):  # noqa: F811
    from tests.test_careagents import _make_account
    acct = _make_account(svc, monkeypatch, "fb@example.com")
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email="fb@example.com")
    from careagents import mail
    monkeypatch.setattr(mail, "send_message", lambda *a, **k: mail.SENT)
    r = c.post("/feedback", json={"stuck": "it was fine"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _row(svc, acct.id)["feedback_sent"]


# --- reading ----------------------------------------------------------------

def test_daily_report_counts_days_conversion_and_inviters(svc, monkeypatch):  # noqa: F811
    from tests.test_careagents import _make_account
    a = _make_account(svc, monkeypatch, "a@example.com")
    b = _make_account(svc, monkeypatch, "b@example.com")
    funnel.mark(svc.session, a.id, "first_answer")
    funnel.set_inviter(svc.session, b.id, a.id)
    with svc.session() as s:
        s.add(invites.InviteJoin(mailbox_hash=invites.mailbox_hash(
            "b@example.com"), link_id="inv_x",
                                 inviter_id=a.id, joined_at=1.0,
                                 real_records="granted"))
    report = funnel.daily(svc.session, days=3)
    assert len(report["days"]) == 3
    assert report["days"][0]["joined"] == 2
    assert report["days"][0]["via_invite"] == 1
    # `confirmed` is not in the chain (how an account joined, not a step
    # after joining): joined -> first_answer counts both accounts.
    answered = report["conversion"][0]
    assert (answered["from"], answered["to"]) == ("joined", "first_answer")
    assert (answered["from_n"], answered["to_n"],
            answered["percent"]) == (2, 1, 50)
    funnel.mark(svc.session, a.id, "confirmed")
    report = funnel.daily(svc.session, days=1)
    assert report["reached"]["confirmed"] == 1
    assert report["conversion"][0]["from_n"] == 2
    assert report["per_inviter"] == [{"inviter_id": a.id, "joined": 1}]


def test_cli_prints_the_daily_table(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    out = app.test_cli_runner().invoke(args=["funnel", "daily", "--days",
                                             "2"]).output
    assert "join" in out and "chat_error" in out
    assert "conversion (all time)" in out
    assert "gene@example.com" not in out


# --- the owner page -----------------------------------------------------------

def _svc_cfg(svc, **env):  # noqa: F811
    """A Config on the `svc` fixture's database, left as it is. `_cfg()`
    drops every table on a non-SQLite test database (Postgres in CI)."""
    from careagents.config import Config
    return Config(env={"CARE_DATABASE_URL": svc.cfg.database_url,
                       "CARE_RP_ID": "localhost",
                       "CARE_ORIGIN": "http://localhost",
                       "OPENAI_API_KEY": "k",
                       "HEALTHCLAW_MINT_SECRET": "mint-secret",
                       "CARE_ALLOW_REAL_MAIL": "1", **env})


def _app(svc, **env):  # noqa: F811
    from careagents.app import create_app
    a = create_app(config=_svc_cfg(svc, **env), client=FakeClient(),
                   accounts=svc)
    a.config["TESTING"] = True
    return a


def test_admin_page_is_closed_to_everyone_when_unset(svc, monkeypatch):  # noqa: F811
    app = _app(svc)
    assert app.test_client().get("/admin/funnel").status_code == 404
    c = app.test_client()
    _login(c, svc, monkeypatch, email="owner@example.com")
    assert c.get("/admin/funnel").status_code == 404


def test_admin_page_refuses_signed_in_non_owners(svc, monkeypatch):  # noqa: F811
    app = _app(svc, CARE_OWNER_EMAILS="owner@example.com",
               CARE_REAL_RECORDS="allowlist",
               CARE_REAL_RECORDS_ALLOWLIST="tester@example.com")
    assert app.test_client().get("/admin/funnel").status_code == 404
    c = app.test_client()
    # A real-records allowlisted tester is not an owner.
    _login(c, svc, monkeypatch, email="tester@example.com")
    assert c.get("/admin/funnel").status_code == 404


def test_admin_page_shows_the_owner_counts_and_emails_only_in_testers(
        svc, monkeypatch):  # noqa: F811
    app = _app(svc, CARE_OWNER_EMAILS=" Owner@Example.com ,x@example.com")
    other = app.test_client()
    _login(other, svc, monkeypatch, email="someone@example.com")
    funnel.count_issue(svc.session, "fasten_failed")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="owner@example.com")
    r = c.get("/admin/funnel")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    for heading in ("Accounts reaching each step", "Conversion between steps",
                    "Issues", "Invites per inviter"):
        assert heading in html
    assert "fasten failed" in html
    # Owner decision 2026-10-16: a tester's email is on this page in the
    # Testers table, and nowhere else on it.
    testers, rest = html.split('id="funnel-days"', 1)
    assert "someone@example.com" in testers
    assert "someone@example.com" not in rest


# --- the digest -------------------------------------------------------------

def test_digest_is_off_without_an_address(svc):  # noqa: F811
    assert funnel.send_digest_if_due(svc.session, _svc_cfg(svc)) is None


def test_digest_sends_once_per_day(svc, monkeypatch):  # noqa: F811
    from careagents import mail
    cfg_ = _svc_cfg(svc, CARE_DIGEST_TO="owner@example.com",
                    RESEND_API_KEY="re_x",
                CARE_ALLOW_REAL_MAIL="1")
    sent = []

    def fake_send(_cfg, to, subject, html, text, reply_to=None):
        sent.append((to, subject, text))
        return mail.SENT
    monkeypatch.setattr(mail, "send_message", fake_send)
    funnel.count_issue(svc.session, "chat_error",
                       dt.datetime(2026, 10, 10, 9, tzinfo=dt.timezone.utc))
    day = dt.datetime(2026, 10, 11, 0, 5, tzinfo=dt.timezone.utc)
    assert funnel.send_digest_if_due(svc.session, cfg_, day) == mail.SENT
    later = day + dt.timedelta(hours=6)
    assert funnel.send_digest_if_due(svc.session, cfg_, later) is None
    assert len(sent) == 1
    to, subject, text = sent[0]
    assert to == "owner@example.com" and "2026-10-10" in subject
    assert "chat_error: 1" in text
    # The next day is its own digest.
    nxt = day + dt.timedelta(days=1)
    assert funnel.send_digest_if_due(svc.session, cfg_, nxt) == mail.SENT
    assert len(sent) == 2


def test_a_refused_digest_is_tried_again(svc, monkeypatch):  # noqa: F811
    from careagents import mail
    cfg_ = _svc_cfg(svc, CARE_DIGEST_TO="owner@example.com",
                    RESEND_API_KEY="re_x",
                CARE_ALLOW_REAL_MAIL="1")
    outcomes = [mail.NOT_SENT, mail.SENT]
    monkeypatch.setattr(mail, "send_message",
                        lambda *a, **k: outcomes.pop(0))
    day = dt.datetime(2026, 10, 11, 1, tzinfo=dt.timezone.utc)
    assert funnel.send_digest_if_due(svc.session, cfg_, day) == mail.NOT_SENT
    assert funnel.send_digest_if_due(svc.session, cfg_, day) == mail.SENT
    assert funnel.send_digest_if_due(svc.session, cfg_, day) is None
    # The refusal was a real send that failed: counted on the day it failed
    # (today), not the day the digest reports on.
    assert funnel.daily(svc.session, days=1)["days"][0]["mail_failed"] == 1


def test_racing_workers_send_one_digest(tmp_path, monkeypatch):
    import threading
    from careagents import mail
    from careagents.accounts import AccountService
    from careagents.config import Config
    cfg_ = Config(env={"CARE_DATABASE_URL": f"sqlite:///{tmp_path}/d.db",
                       "OPENAI_API_KEY": "k",
                       "CARE_DIGEST_TO": "owner@example.com"})
    svc_ = AccountService(cfg_)
    sent, start = [], threading.Barrier(6)
    monkeypatch.setattr(mail, "send_message",
                        lambda *a, **k: sent.append(1) or mail.SENT)
    day = dt.datetime(2026, 10, 11, 2, tzinfo=dt.timezone.utc)

    def tick():
        start.wait()
        funnel.send_digest_if_due(svc_.session, cfg_, day)
    try:
        threads = [threading.Thread(target=tick) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(sent) == 1
    finally:
        svc_.engine.dispose()


# --- "Connect your records" after the first made-up answer ------------------

def _invited_app(monkeypatch, invited=True, **env):
    """Stage 1: allowlist mode, Fasten key set, terms approved."""
    from careagents.accounts import AccountService
    from careagents.app import create_app
    cfg_ = allowlist_cfg(**env)
    svc_ = AccountService(cfg_)
    from careagents import agent as agent_mod

    class _Turn:
        text, tool_calls, raw_tool_calls = "an answer", [], []
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: _Turn())
    fake = FakeClient()
    app = create_app(config=cfg_, client=fake, accounts=svc_)
    app.config["TESTING"] = True
    if invited:
        svc_.invite_real_records("gene@example.com", "acct_inviter")
    c = app.test_client()
    _login(c, svc_, monkeypatch)
    sample = c.post("/api/connections/sample").get_json()
    return app, svc_, c, sample["agent_id"]


def test_invited_account_sees_connect_after_its_first_answer(monkeypatch):
    app, svc_, c, agent_id = _invited_app(monkeypatch)
    try:
        hub = c.get("/home").get_data(as_text=True)
        assert 'id="connect-records"' not in hub        # no answer yet
        chat = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
        assert 'id="connect-next" hidden' in chat       # shown after one
        _turn(c, agent_id, "what do my labs say?")
        hub = c.get("/home").get_data(as_text=True)
        assert 'id="connect-records"' in hub
        card = hub[hub.index('id="connect-records"'):]
        card = card[:card.index("</section>")]
        assert 'data-connector="fasten"' in card and 'data-consent="1"' in card
        assert "Connect your records" in card
        chat = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
        assert 'id="connect-next">' in chat
        assert 'href="/home#connect-records"' in chat
    finally:
        svc_.engine.dispose()


def test_the_card_leads_through_consent_to_fasten(monkeypatch):
    """The card's tile posts what the menu's does: refused without the
    tester-terms consent, then a pending connection and Fasten's connect
    page (stubbed at the HealthClaw client) with it."""
    from careagents import tester_terms
    app, svc_, c, agent_id = _invited_app(monkeypatch)
    try:
        _turn(c, agent_id, "hi")
        assert c.post("/api/connections/fasten",
                      json={}).status_code == 428
        r = c.post("/api/connections/fasten", json={
            "consent": True,
            "consent_version": tester_terms.CONSENT_VERSION})
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert body["status"] == "pending"
        assert "/connect/" in body["connect_url"]
        assert _row(svc_, _acct_id(svc_))["connect_started"]
        # Real records on the way: the card has done its job.
        assert 'id="connect-records"' not in c.get("/home").get_data(
            as_text=True)
    finally:
        svc_.engine.dispose()


@pytest.mark.parametrize("env", [{}, {"FASTEN_PUBLIC_KEY": ""}])
def test_no_card_without_an_invite_or_a_live_fasten(monkeypatch, env):
    invited = bool(env)          # the second case is invited, Fasten unset
    app, svc_, c, agent_id = _invited_app(monkeypatch, invited=invited, **env)
    try:
        _turn(c, agent_id, "hi")
        assert 'id="connect-records"' not in c.get("/home").get_data(
            as_text=True)
        assert 'id="connect-next"' not in c.get(
            f"/chat?agent={agent_id}").get_data(as_text=True)
        if not invited:
            # The gate is untouched: still refused server-side.
            r = c.post("/api/connections/fasten", json={"consent": True})
            assert r.status_code == 503
    finally:
        svc_.engine.dispose()


def test_fasten_failed_counts_only_past_the_gate(monkeypatch):
    """A refusal at the real-records gate is not a Fasten failure; an
    invited account refused because Fasten is not set up is."""
    from careagents import tester_terms
    consent = {"consent": True,
               "consent_version": tester_terms.CONSENT_VERSION}
    app, svc_, c, _ = _invited_app(monkeypatch, invited=False)
    try:
        assert c.post("/api/connections/fasten",
                      json=consent).status_code == 503
        assert _today(svc_)["fasten_failed"] == 0
    finally:
        svc_.engine.dispose()
    app, svc_, c, _ = _invited_app(monkeypatch, FASTEN_PUBLIC_KEY="")
    try:
        assert c.post("/api/connections/fasten",
                      json=consent).status_code == 503
        assert _today(svc_)["fasten_failed"] == 1
    finally:
        svc_.engine.dispose()


def test_a_paused_account_gets_no_card(monkeypatch):
    app, svc_, c, agent_id = _invited_app(monkeypatch)
    try:
        _turn(c, agent_id, "hi")
        svc_.set_paused("gene@example.com", True)
        assert 'id="connect-records"' not in c.get("/home").get_data(
            as_text=True)
    finally:
        svc_.engine.dispose()
