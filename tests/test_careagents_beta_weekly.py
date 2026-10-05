"""The weekly number (beta spec 4.5): distinct accounts per ISO week, as
integers, counting real-record activity only."""

from __future__ import annotations

import datetime as dt

import pytest

from careagents import beta
from careagents.models import Account, ActivityDay, Connection
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, _login, _make_account, cfg, svc)

NOW = dt.datetime(2026, 10, 7, 12, tzinfo=dt.timezone.utc)   # 2026-W41


def _ts(y, m, d):
    return dt.datetime(y, m, d, 12, tzinfo=dt.timezone.utc).timestamp()


def test_counts_are_distinct_accounts_per_iso_week(svc):  # noqa: F811
    with svc.session() as s:
        s.add_all([
            Account(id="acct_a", email="a@example.com",
                    created_at=_ts(2026, 10, 6)),
            Account(id="acct_b", email="b@example.com",
                    created_at=_ts(2026, 9, 29)),
        ])
    with svc.session() as s:
        s.add_all([
            Connection(account_id="acct_a", kind="fasten", tenant_id="t-a",
                       consented_at=_ts(2026, 10, 6),
                       consent_version="2026-08-01"),
            Connection(account_id="acct_b", kind="sample", tenant_id="t-b"),
            ActivityDay(account_id="acct_a", day="2026-10-06", asked=3),
            ActivityDay(account_id="acct_a", day="2026-10-07", asked=1,
                        approved=1),
        ])
    rows = beta.weekly_counts(svc.session, weeks=2, now=NOW)
    assert rows == [
        {"week": "2026-W41", "signed_up": 1, "real_connected": 1,
         "asked": 1, "approved": 1},
        {"week": "2026-W40", "signed_up": 1, "real_connected": 0,
         "asked": 0, "approved": 0},
    ]


def test_every_value_is_an_integer_or_the_week_label(svc):  # noqa: F811
    for row in beta.weekly_counts(svc.session, weeks=3, now=NOW):
        assert set(row) == {"week", "signed_up", "real_connected",
                            "asked", "approved"}
        assert all(isinstance(v, int) for k, v in row.items() if k != "week")


def test_count_activity_adds_and_refuses_unknown_fields(svc, monkeypatch):  # noqa: F811
    acct = _make_account(svc, monkeypatch, "counter@example.com")
    acct_id = getattr(acct, "id", acct)
    svc.count_activity(acct_id, "asked")
    svc.count_activity(acct_id, "asked")
    svc.count_activity(acct_id, "approved")
    with svc.session() as s:
        row = s.query(ActivityDay).one()
        assert (row.asked, row.approved) == (2, 1)
    with pytest.raises(ValueError):
        svc.count_activity(acct_id, "message_text")


def test_a_sample_turn_does_not_count(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.worker import RunWorker
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "sample-count"},
               buffered=False)
    next(iter(r.response))
    r.close()
    RunWorker(cfg, fake, svc, "count-worker").run_once()
    with svc.session() as s:
        assert s.query(ActivityDay).count() == 0


def _real_agent(cfg, svc, monkeypatch):  # noqa: F811
    """A Fasten connection made active, with an assistant on it."""
    from careagents import agent as agent_mod
    from careagents.app import create_app

    class _Turn:
        text, tool_calls, raw_tool_calls = "ok", [], []
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: _Turn())
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/fasten", json={"consent": True}).get_json()
    svc.set_connection_status(fake.tenants[-1], "active")
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    return c, fake, agent_id


def test_a_real_record_turn_counts_as_asked(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.worker import RunWorker
    c, fake, agent_id = _real_agent(cfg, svc, monkeypatch)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "real-count"},
               buffered=False)
    next(iter(r.response))
    r.close()
    RunWorker(cfg, fake, svc, "count-worker").run_once()
    with svc.session() as s:
        assert s.query(ActivityDay).one().asked == 1


def test_a_refused_turn_does_not_count_as_asked(cfg, svc, monkeypatch):  # noqa: F811
    """A paused account's turn never reached a model, so it was not a
    question answered from the record."""
    from careagents.worker import RunWorker
    c, fake, agent_id = _real_agent(cfg, svc, monkeypatch)
    svc.set_paused("gene@example.com", True)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "paused-count"},
               buffered=False)
    next(iter(r.response))
    r.close()
    RunWorker(cfg, fake, svc, "count-worker").run_once()
    with svc.session() as s:
        assert s.query(ActivityDay).count() == 0


def test_an_approval_counts_on_a_real_assistant_only(cfg, svc, monkeypatch):  # noqa: F811
    """`nka` here is the synthetic review form's own attestation input, as
    in the existing relay test. It is the human's answer, never inferred."""
    c, fake, agent_id = _real_agent(cfg, svc, monkeypatch)
    ok = c.post(f"/review/{agent_id}/act-1/submit",
                json={"med-0": "yes", "nka": "true"})
    assert ok.status_code == 200 and ok.get_json()["confirmed"] is True
    with svc.session() as s:
        assert s.query(ActivityDay).one().approved == 1

    sample = c.post("/api/connections/sample").get_json()
    sample_agent = sample.get("agent_id") or c.post("/api/agents", json={
        "name": "S", "persona": "calm",
        "connection_id": sample["id"]}).get_json()["id"]
    r = c.post(f"/review/{sample_agent}/act-1/submit",
               json={"med-0": "yes", "nka": "true"})
    assert r.status_code == 200 and r.get_json()["confirmed"] is True
    with svc.session() as s:
        assert s.query(ActivityDay).one().approved == 1
