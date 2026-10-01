"""A turn the worker will refuse (paused, or terms not accepted) never reaches
a model, so it does not spend the day's allowance (#856 QA review).

The turn is charged in the worker, where the model is called (#856 sign-off
F4), so `_turn` runs the worker as a deployed turn would.
"""

from __future__ import annotations

from careagents.models import UsageDay
from tests.careagents_stage1_helpers import approve_terms
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, _login, cfg, svc)


def _turn(c, agent_id, request_id):
    from careagents.worker import RunWorker
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": request_id}, buffered=False)
    status = r.status_code
    r.close()
    runtime = c.application.extensions["careagents_runtime"]
    RunWorker(runtime["config"], runtime["client"], runtime["accounts"],
              "cap-worker").run_once()
    return status


def _used(svc):  # noqa: F811
    with svc.session() as s:
        return sum(int(u.turns or 0) for u in s.query(UsageDay).all())


def test_paused_turns_do_not_use_up_the_daily_cap(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(cfg, "chat_turns_per_day", 1)
    svc.set_paused("gene@example.com", True)
    assert _turn(c, agent_id, "p-1") == 200
    assert _turn(c, agent_id, "p-2") == 200     # not a 429
    assert _used(svc) == 0
    svc.set_paused("gene@example.com", False)
    assert _turn(c, agent_id, "p-3") == 200
    assert _used(svc) == 1
    assert _turn(c, agent_id, "p-4") == 429     # the cap still holds


def test_a_turn_waiting_on_the_terms_does_not_use_the_cap(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/direct", json={"consent": True}).get_json()
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    approve_terms(monkeypatch, "2026-10-01")
    assert _turn(c, agent_id, "t-1") == 200
    assert _used(svc) == 0


def test_a_turn_over_the_cap_is_answered_without_the_model(
        cfg, svc, monkeypatch):  # noqa: F811
    """Two turns admitted before either ran: the second finds the day spent
    when the worker charges it, and answers with the limit sentence."""
    from careagents import agent as agent_mod, beta
    from careagents.worker import RunWorker
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    calls = []

    class _Turn:
        text, tool_calls, raw_tool_calls = "model answer", [], []
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: calls.append(1) or _Turn())
    monkeypatch.setattr(cfg, "chat_turns_per_day", 1)
    for rid in ("q-1", "q-2"):
        r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                      "request_id": rid}, buffered=False)
        assert r.status_code == 200
        r.close()
    w = RunWorker(cfg, fake, svc, "cap-worker")
    while w.run_once():
        pass
    assert calls == [1]
    assert _used(svc) == 1
    answers = [row["content"] for rows in fake.logged.values()
               for row in rows if row["role"] == "assistant"]
    assert answers == ["model answer", beta.DAILY_LIMIT_TEXT]
    assert _turn(c, agent_id, "q-3") == 429     # and admission says so


def test_a_recovered_run_is_not_charged_twice(cfg, svc, monkeypatch):  # noqa: F811
    """A run that made a checkpoint before its worker died was charged then.
    Its recovery finishes the answer it has, uncharged, even at the cap."""
    from careagents.worker import RunWorker
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(cfg, "chat_turns_per_day", 1)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "rec-1"}, buffered=False)
    r.close()
    run_id = next(i for i, run in fake.runs.items()
                  if run["status"] == "queued")
    acct_id = svc.get_worker_agent_context(agent_id)["account_id"]
    assert svc.claim_daily_turn(acct_id, 1) == (True, 1)   # the first try
    fake._append_run_event(run_id, "agent.checkpoint", {
        "checkpoint_id": "round-1", "round": 1, "text": "kept answer",
        "tool_calls": [], "raw_tool_calls": []})
    RunWorker(cfg, fake, svc, "recovery-worker").run_once()
    assert fake.runs[run_id]["status"] == "completed"
    answers = [row["content"] for rows in fake.logged.values()
               for row in rows if row["role"] == "assistant"]
    assert answers == ["kept answer"]
    assert _used(svc) == 1
