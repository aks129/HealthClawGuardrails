"""QA for #876: a failed chat turn's answer, on the real engine.

The PR's own tests use FakeClient, which accepts any request id and any
reply_to. These drive the real HealthClawClient onto the real engine WSGI,
so the wire format (request id charset, reply_to inside the conversation,
the run state machine and lease ownership) is the engine's, not a fake's.

Synthetic data only.
"""

from __future__ import annotations

from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, _turn, cfg, svc)


def _rows(chain):
    from r6.command_center.models import ConversationMessage
    with chain.engine_app.app_context():
        return [(m.role, m.text, m.reply_to, m.request_id, m.id)
                for m in ConversationMessage.query.filter_by(
                    tenant_id=TENANT).order_by(ConversationMessage.created_at,
                                               ConversationMessage.id)]


def _run(chain):
    from r6.agent_runs.models import AgentRun
    with chain.engine_app.app_context():
        [run] = AgentRun.query.filter_by(tenant_id=TENANT).all()
        return run.to_dict() if hasattr(run, "to_dict") else {
            "id": run.id, "status": run.status}


def _failing_turn(chain, cfg, svc, monkeypatch):  # noqa: F811
    from careagents.worker import RunWorker

    def _boom(*_a, **_k):
        raise RuntimeError("synthetic provider outage for Jane Roe")
    monkeypatch.setattr("careagents.worker.llm.complete", _boom)
    c = chain.app.test_client()
    _login(c, svc, monkeypatch, email="acceptance@example.com")
    RunWorker(cfg, chain.hc, svc, "qa-worker").run_once()
    r = _turn(c, chain.agent, "how are my labs?")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_a_failed_turn_is_answered_once_in_the_engine_transcript(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.agent import GENERIC_FAILURE_TEXT
    from careagents.worker import RunWorker
    chain = Chain(cfg, svc, monkeypatch)
    _failing_turn(chain, cfg, svc, monkeypatch)

    run = _run(chain)
    assert run["status"] == "failed"
    rows = _rows(chain)
    assert [r[0] for r in rows] == ["user", "assistant"], rows
    user, answer = rows
    assert answer[1] == GENERIC_FAILURE_TEXT
    assert "Jane" not in answer[1] and "outage" not in answer[1]
    # The engine accepted reply_to and the request id charset.
    assert answer[2] == user[4]
    assert answer[3] == f"run:{run['id']}:failed"

    # A replay of the write (a retried worker, a redelivered call) lands on
    # the engine's idempotency key, not a second row.
    full = chain.hc.get_agent_run(TENANT, run["id"])
    RunWorker(cfg, chain.hc, svc, "qa-worker")._answer_failed_turn(
        full, GENERIC_FAILURE_TEXT)
    assert [r[0] for r in _rows(chain)] == ["user", "assistant"]


def test_a_worker_without_the_lease_writes_nothing_on_the_engine(
        cfg, svc, monkeypatch):  # noqa: F811
    """A run claimed by one worker and failed by another: the engine
    refuses the stranger's `failed` transition, so no answer is written and
    the run stays with its owner, who may still finish it."""
    from careagents.worker import RunWorker
    chain = Chain(cfg, svc, monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("synthetic provider outage")
    monkeypatch.setattr("careagents.worker.llm.complete", _boom)
    c = chain.app.test_client()
    _login(c, svc, monkeypatch, email="acceptance@example.com")
    RunWorker(cfg, chain.hc, svc, "owner").run_once()
    r = c.post("/api/chat", json={"agent_id": chain.agent,
                                  "message": "hi", "request_id": "qa-lease"},
               buffered=False)
    r.close()
    claimed = chain.hc.claim_agent_run("owner", cfg.run_lease_seconds)
    assert claimed is not None and claimed["status"] == "running"

    # Let the stranger's event append through untouched so it reaches the
    # transition: the engine is then the only thing that can stop it.
    real_append = chain.hc.append_agent_run_event
    calls = []

    def _append(run_id, worker_id, kind, payload):
        calls.append(kind)
        try:
            return real_append(run_id, worker_id, kind, payload)
        except Exception:  # noqa: BLE001 - a refused append is fine here
            return None
    monkeypatch.setattr(chain.hc, "append_agent_run_event", _append)
    RunWorker(cfg, chain.hc, svc, "stranger").process(claimed)

    assert "agent.error" in calls
    assert _run(chain)["status"] == "running"
    assert [r[0] for r in _rows(chain)] == ["user"]
