"""A turn the worker will refuse (paused, or terms not accepted) never reaches
a model, so it does not spend the day's allowance (#856 QA review).

The turn is charged in the worker, where the model is called (#856 sign-off
F4), so `_turn` runs the worker as a deployed turn would.
"""

from __future__ import annotations

import pathlib

import pytest

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


def test_a_worker_killed_in_its_first_model_call_is_charged_once(
        cfg, svc, monkeypatch):  # noqa: F811
    """QA on #862: kill -9 during the first model call, before any
    checkpoint, and the recovered run was charged again (0 -> 2). The
    charge leaves a marker on the run; recovery sees it and skips."""
    from careagents import agent as agent_mod
    from careagents.worker import RunWorker
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "kill-1"}, buffered=False)
    r.close()

    class _Killed(BaseException):
        """Not an Exception: nothing in the worker catches it, as with
        kill -9."""

    def die(*a, **k):
        raise _Killed()
    monkeypatch.setattr(agent_mod.llm, "complete", die)

    class _Lease:
        def check(self):
            pass
    run = fake.claim_agent_run("doomed-worker")
    try:
        RunWorker(cfg, fake, svc, "doomed-worker")._execute(run, _Lease())
    except _Killed:
        pass
    assert _used(svc) == 1
    # The lease runs out; the run goes back on the queue.
    fake.runs[run["id"]].update(status="queued", worker_id=None)

    class _Turn:
        text, tool_calls, raw_tool_calls = "answer after recovery", [], []
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: _Turn())
    RunWorker(cfg, fake, svc, "recovery-worker").run_once()
    assert fake.runs[run["id"]]["status"] == "completed"
    assert _used(svc) == 1


CHAT_JS = (pathlib.Path(__file__).resolve().parents[1]
           / "careagents" / "static" / "chat.js")

_LIMIT_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function cut(re) {
  const m = src.match(re);
  if (!m) throw new Error('not found: ' + re);
  return m[0];
}
const limitText = new Function(
  cut(/const PACE_TEXT = [^;]+;/) + '\n'
  + cut(/function limitText\(d\) \{[\s\S]*?\n  \}\n/)
  + 'return limitText;')();
const daily = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify({
  daily: limitText(daily),
  pace: limitText({ error: 'rate_limited' }),
  empty: limitText({}),
  blankMessage: limitText({ error: 'x', message: '' }),
}));
"""


def test_the_chat_says_the_servers_sentence_at_the_daily_limit(
        cfg, svc, monkeypatch):  # noqa: F811
    """Patient tester on #862: every 429 said "give it a few minutes", which
    is false at the daily limit. The page shows the server's sentence; the
    burst limiter's 429, which sends none, keeps the pace sentence."""
    import json
    import shutil
    import subprocess
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(cfg, "chat_turns_per_day", 1)
    assert _turn(c, agent_id, "lim-1") == 200
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "lim-2"})
    assert r.status_code == 429
    body = r.get_json()
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", _LIMIT_HARNESS, "--", str(CHAT_JS),
                          json.dumps(body)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    from careagents import beta
    assert got["daily"] == beta.DAILY_LIMIT_TEXT
    assert "few minutes" in got["pace"]
    assert got["empty"] == got["pace"] == got["blankMessage"]


def test_the_daily_limit_sentence_is_plain():
    from careagents import beta
    assert "UTC" not in beta.DAILY_LIMIT_TEXT
    assert "midnight" not in beta.DAILY_LIMIT_TEXT
    assert beta.DAILY_LIMIT_TEXT == ("You've reached today's message limit. "
                                     "It resets overnight.")


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
