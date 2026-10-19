"""QA on #980: two guards no test pinned (mutation survivors).

1. A recovered run that already has a checkpoint was answered by a model
   round before this worker took it, so a later failure must not give the
   turn back (worker.py `"answered": bool(checkpoints)`).
2. The own-run check compares the tenant on the engine's answer, not only
   the agent: an engine that hands back another tenant's run is not
   trusted (feedback.py `_run_is_theirs`).

Synthetic data only.
"""

from __future__ import annotations

import datetime as dt

from careagents.worker import CHARGED_EVENT, REFUNDED_EVENT, RunWorker
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)
from tests.test_careagents_beta_feedback_slice import (  # noqa: F401
    RUN, _flag, _own_run, _signed_in, made, sent)
from tests.test_careagents_failed_turn_refund import (
    _events, _model_fails, _post, _used, _work)


def test_a_recovered_run_with_a_checkpoint_is_not_given_back(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    _model_fails(monkeypatch)
    with c.session_transaction() as sess:
        account_id = sess["account_id"]
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    # An earlier worker charged this run, and a model round answered
    # (a checkpoint whose tool call has no result yet), before the run came back to be finished.
    svc.claim_daily_turn(account_id, 10, day=day)
    real = fake.agent_run_events

    def recovered(tenant_id, run_id, after=0, limit=100):
        out = real(tenant_id, run_id, after=after, limit=limit)
        return {**out, "events": list(out.get("events") or []) + [
            {"type": CHARGED_EVENT, "payload": {"day": day}},
            {"type": "agent.checkpoint",
             "payload": {"round": 1, "text": "", "tool_calls": [
                 {"id": "call-1", "name": "search_records",
                  "arguments": {}}]}}]}

    def tool_breaks(*a, **k):
        raise RuntimeError("synthetic tool failure")
    monkeypatch.setattr(RunWorker, "_execute_durable_tool", tool_breaks)
    assert _post(c, agent_id, "rec-1") == 200
    monkeypatch.setattr(fake, "agent_run_events", recovered)
    _work(c)
    (run,) = fake.runs.values()
    assert run["status"] == "failed"
    assert _used(svc) == 1
    assert _events(fake, REFUNDED_EVENT) == []


def test_an_engine_answer_for_another_tenant_is_not_trusted(
        made, sent, monkeypatch):  # noqa: F811
    app, svc_ = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc_, monkeypatch)
    agent_id, tenant = _own_run(app, svc_, c)
    fake = app.extensions["careagents_runtime"]["client"]

    def wrong_tenant(tenant_id, run_id):
        return {"id": run_id, "tenant_id": "ca-someone-else",
                "agent_id": agent_id, "status": "completed"}
    monkeypatch.setattr(fake, "get_agent_run", wrong_tenant)
    assert _flag(c).status_code == 200
    assert RUN not in sent[0]["text"]
