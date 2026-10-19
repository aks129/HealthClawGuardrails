"""A turn that fails on our side gives back the day's turn it was charged.

The patient tester hit the daily cap on a chat where most turns had said
"Something went wrong on our side". The worker charges where the model is
called; when this worker then fails the run, it marks the run refunded
(while it still owns it) and gives the turn back once its failure is
accepted. Every miss errs toward the cap: never a double refund, never a
count below zero, never another day's turn. Synthetic data only.
"""

from __future__ import annotations

import pytest

from careagents import worker as worker_mod
from careagents.healthclaw import HealthClawError
from careagents.models import UsageDay
from careagents.worker import CHARGED_EVENT, REFUNDED_EVENT, RunWorker
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)


def _used(svc):  # noqa: F811
    with svc.session() as s:
        return sum(int(u.turns or 0) for u in s.query(UsageDay).all())


def _post(c, agent_id, request_id):
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": request_id}, buffered=False)
    status = r.status_code
    r.close()
    return status


def _work(c):
    runtime = c.application.extensions["careagents_runtime"]
    RunWorker(runtime["config"], runtime["client"], runtime["accounts"],
              "refund-worker").run_once()


def _events(fake, kind):
    return [e for evs in fake.events.values() for e in evs
            if e.get("type") == kind]


def _model_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("synthetic model failure")
    monkeypatch.setattr(worker_mod.llm, "complete", boom)


def test_a_failed_turn_does_not_use_up_the_cap(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(cfg, "chat_turns_per_day", 1)
    _model_fails(monkeypatch)
    assert _post(c, agent_id, "f-1") == 200
    _work(c)
    assert _used(svc) == 0
    (charged,) = _events(fake, CHARGED_EVENT)
    (refunded,) = _events(fake, REFUNDED_EVENT)
    assert refunded["payload"] == charged["payload"]
    assert set(charged["payload"]) == {"day"}
    # The next turn is still allowed, and a good one is charged.
    _chat_app_reply(monkeypatch)
    assert _post(c, agent_id, "f-2") == 200
    _work(c)
    assert _used(svc) == 1
    assert _post(c, agent_id, "f-3") == 429


def _chat_app_reply(monkeypatch):
    class _Turn:
        text, tool_calls, raw_tool_calls = "fine", [], []
    monkeypatch.setattr(worker_mod.llm, "complete", lambda *a, **k: _Turn())


def test_a_good_turn_stays_charged(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    assert _post(c, agent_id, "g-1") == 200
    _work(c)
    assert _used(svc) == 1
    assert _events(fake, REFUNDED_EVENT) == []


def test_a_deadline_failure_gives_the_turn_back(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)

    def late(_run):
        raise worker_mod.RunDeadlineExceeded()
    monkeypatch.setattr(RunWorker, "_check_deadline", staticmethod(late))
    assert _post(c, agent_id, "d-1") == 200
    _work(c)
    assert _events(fake, CHARGED_EVENT)
    assert _used(svc) == 0


def test_a_worker_that_lost_the_run_gives_nothing_back(
        cfg, svc, monkeypatch):  # noqa: F811
    # Its failure was refused: the run may still finish elsewhere.
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    _model_fails(monkeypatch)
    real = fake.transition_agent_run

    def refuse_failed(run_id, worker_id, status, **kw):
        if status == "failed":
            raise HealthClawError("worker does not own run", 409)
        return real(run_id, worker_id, status, **kw)
    monkeypatch.setattr(fake, "transition_agent_run", refuse_failed)
    assert _post(c, agent_id, "l-1") == 200
    _work(c)
    assert _used(svc) == 1


def test_a_run_already_marked_is_not_given_back_twice(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    _model_fails(monkeypatch)
    real = fake.agent_run_events

    def with_marker(tenant_id, run_id, after=0, limit=100):
        out = real(tenant_id, run_id, after=after, limit=limit)
        return {**out, "events": list(out.get("events") or []) + [
            {"type": REFUNDED_EVENT, "payload": {"day": "2026-10-16"}}]}
    assert _post(c, agent_id, "m-1") == 200
    monkeypatch.setattr(fake, "agent_run_events", with_marker)
    _work(c)
    assert _used(svc) == 1


def test_a_charge_from_before_the_day_was_recorded_is_kept(
        cfg, svc, monkeypatch):  # noqa: F811
    # A recovered run charged by older code: its marker has no day, so
    # which day's turn to give back is unknown, and none is.
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    _model_fails(monkeypatch)
    real = fake.agent_run_events

    def old_charge(tenant_id, run_id, after=0, limit=100):
        out = real(tenant_id, run_id, after=after, limit=limit)
        return {**out, "events": list(out.get("events") or []) + [
            {"type": CHARGED_EVENT, "payload": {}}]}
    assert _post(c, agent_id, "o-1") == 200
    from careagents.models import Account
    with svc.session() as s:
        aid = s.query(Account).filter_by(email="gene@example.com").one().id
    svc.claim_daily_turn(aid, 5)         # what the older code charged
    monkeypatch.setattr(fake, "agent_run_events", old_charge)
    _work(c)
    assert _used(svc) == 1
    assert _events(fake, REFUNDED_EVENT) == []


def test_the_failure_branch_reads_nothing_more_from_the_engine(
        cfg, svc, monkeypatch):  # noqa: F811
    # The failure branch runs outside the turn's acting_for block, so a
    # read there would not carry the account's label. It reads none.
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    _model_fails(monkeypatch)
    real = fake.agent_run_events
    reads = []

    def counted(*a, **k):
        reads.append(1)
        return real(*a, **k)
    monkeypatch.setattr(fake, "agent_run_events", counted)
    assert _post(c, agent_id, "r-1") == 200
    _work(c)
    assert _used(svc) == 0
    assert len(reads) == 1


def test_a_refund_that_fails_never_breaks_the_failure_answer(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    _model_fails(monkeypatch)

    def broken(*a, **k):
        raise RuntimeError("usage table is gone")
    monkeypatch.setattr(svc, "refund_daily_turn", broken)
    assert _post(c, agent_id, "b-1") == 200
    _work(c)
    (run,) = fake.runs.values()
    assert run["status"] == "failed"


def test_a_turn_that_failed_after_a_model_round_counts(
        cfg, svc, monkeypatch):  # noqa: F811
    # The model answered one round (a tool call), then the tool failed:
    # the model call was paid for, so the turn is not given back.
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)

    class _Call:
        id, name, arguments = "call-1", "search_records", {}

    class _Turn:
        text, tool_calls, raw_tool_calls = "", [_Call()], []
    monkeypatch.setattr(worker_mod.llm, "complete", lambda *a, **k: _Turn())

    def tool_breaks(*a, **k):
        raise RuntimeError("synthetic tool failure")
    monkeypatch.setattr(RunWorker, "_execute_durable_tool", tool_breaks)
    assert _post(c, agent_id, "a-1") == 200
    _work(c)
    (run,) = fake.runs.values()
    assert run["status"] == "failed"
    assert _events(fake, "agent.checkpoint")
    assert _used(svc) == 1
    assert _events(fake, REFUNDED_EVENT) == []


def test_at_most_three_failed_turns_a_day_are_given_back(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.models import REFUNDS_PER_DAY
    assert REFUNDS_PER_DAY == 3
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    _model_fails(monkeypatch)
    for i in range(5):
        assert _post(c, agent_id, f"cap-{i}") == 200
        _work(c)
    # Five failures: three given back, two count.
    assert _used(svc) == 2


# --- refund_daily_turn -----------------------------------------------------

def _acct(svc):  # noqa: F811
    from careagents.models import Account
    with svc.session() as s:
        a = Account(email="refund@example.test")
        s.add(a)
        s.flush()
        return a.id


def test_a_refund_never_goes_below_zero(svc):  # noqa: F811
    aid = _acct(svc)
    assert svc.claim_daily_turn(aid, 5, day="2026-10-15") == (True, 1)
    assert svc.refund_daily_turn(aid, "2026-10-15") is True
    assert svc.refund_daily_turn(aid, "2026-10-15") is False
    assert _used(svc) == 0


def test_a_refund_gives_back_only_its_own_day(svc):  # noqa: F811
    aid = _acct(svc)
    svc.claim_daily_turn(aid, 5, day="2026-10-15")
    svc.claim_daily_turn(aid, 5, day="2026-10-16")
    assert svc.refund_daily_turn(aid, "2026-10-14") is False
    assert svc.refund_daily_turn(aid, "2026-10-15") is True
    with svc.session() as s:
        rows = {u.day: u.turns for u in s.query(UsageDay).all()}
    assert rows == {"2026-10-15": 0, "2026-10-16": 1}


def test_the_daily_refund_limit_is_per_account_and_day(svc):  # noqa: F811
    from careagents.models import REFUNDS_PER_DAY, RefundDay
    aid = _acct(svc)
    for _ in range(REFUNDS_PER_DAY + 2):
        svc.claim_daily_turn(aid, 50, day="2026-10-15")
    given = [svc.refund_daily_turn(aid, "2026-10-15")
             for _ in range(REFUNDS_PER_DAY + 2)]
    assert given == [True] * REFUNDS_PER_DAY + [False, False]
    assert _used(svc) == 2
    # Another day has its own limit.
    svc.claim_daily_turn(aid, 50, day="2026-10-16")
    assert svc.refund_daily_turn(aid, "2026-10-16") is True
    with svc.session() as s:
        rows = {r.day: r.count for r in s.query(RefundDay).all()}
    assert rows == {"2026-10-15": REFUNDS_PER_DAY, "2026-10-16": 1}


def test_a_refund_with_nothing_to_give_back_spends_no_refund(svc):  # noqa: F811
    from careagents.models import RefundDay
    aid = _acct(svc)
    assert svc.refund_daily_turn(aid, "2026-10-15") is False
    with svc.session() as s:
        assert s.query(RefundDay).count() == 0


def test_racing_refunds_never_pass_the_limit(tmp_path, monkeypatch):
    import threading

    from careagents.accounts import AccountService
    from careagents.models import REFUNDS_PER_DAY
    from tests.test_careagents_beta_signup import _cfg
    cfg_ = _cfg(CARE_DATABASE_URL=f"sqlite:///{tmp_path / 'race.db'}")
    svc_ = AccountService(cfg_)
    aid = _acct(svc_)
    for _ in range(10):
        svc_.claim_daily_turn(aid, 50, day="2026-10-15")
    results, gate = [], threading.Barrier(8)

    def refund():
        gate.wait()
        try:
            results.append(svc_.refund_daily_turn(aid, "2026-10-15"))
        except Exception as exc:  # noqa: BLE001 - recorded, then asserted
            results.append(exc)
    threads = [threading.Thread(target=refund) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) <= REFUNDS_PER_DAY
    assert _used(svc_) == 10 - results.count(True)
    svc_.engine.dispose()


def test_an_account_and_its_refund_rows_go_together(svc):  # noqa: F811
    from careagents.models import RefundDay
    aid = _acct(svc)
    svc.claim_daily_turn(aid, 5, day="2026-10-15")
    svc.refund_daily_turn(aid, "2026-10-15")
    assert svc.delete_account(aid) is True
    with svc.session() as s:
        assert s.query(RefundDay).count() == 0


@pytest.mark.parametrize("cap", [1, 3])
def test_charge_then_refund_round_trips_under_the_cap(svc, cap):  # noqa: F811
    aid = _acct(svc)
    for _ in range(cap):
        assert svc.claim_daily_turn(aid, cap, day="2026-10-15")[0]
    assert svc.claim_daily_turn(aid, cap, day="2026-10-15")[0] is False
    svc.refund_daily_turn(aid, "2026-10-15")
    assert svc.claim_daily_turn(aid, cap, day="2026-10-15") == (True, cap)
