"""QA #947: the run-event replay on the live chat path names the account.

`POST /api/chat` answers with the same `_stream_run` generator as the
replay route, and it is the path every web chat turn takes. The security
row (`test_run_event_reads_while_streaming_name_the_account`) closes that
stream before it reads anything and drives only the replay route, so
dropping `account_id=acct.id` from the POST call site survived it. This
test reads the POST stream itself, with the worker finishing the turn
part-way through.
"""
# ruff: noqa: F811  (the cfg and svc fixtures, imported then requested)

from __future__ import annotations

from careagents import models
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)


def test_the_chat_stream_itself_names_the_account(cfg, svc, monkeypatch):
    from careagents import healthclaw
    from careagents.worker import RunWorker
    app, c, fake, agent_id, _tenant, _conn = _chat_app(
        cfg, svc, monkeypatch, reply="ok")
    labels = []
    real = fake.agent_run_events
    ran = []

    def spy(*a, **k):
        labels.append(healthclaw.agent_header())
        if not ran:
            # The turn completes while the stream is open, so the replay
            # goes on to read the answer's events.
            ran.append(RunWorker(cfg, fake, svc, "qa947-worker").run_once())
        return real(*a, **k)
    fake.agent_run_events = spy
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "qa947-1"}, buffered=False)
    assert r.status_code == 200
    body = b"".join(r.response)
    r.close()
    with svc.session() as s:
        aid = s.query(models.Account).one().id
    assert ran, "the stream made no run-event read"
    assert b"ok" in body
    assert labels and set(labels) == {f"careagents:{aid}"}, set(labels)
