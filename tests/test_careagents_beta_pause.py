"""A paused account's turns answer the paused sentence and call no provider
(spec section 6; R3). The run is queued BEFORE the pause, which is the case
an admission check would miss.

Pause covers every turn on the account, the sample included: one state, not
two, and the sample is synthetic, so nothing is lost by stopping it too.
"""

from __future__ import annotations

import pytest

from careagents import beta
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)

EMAIL = "gene@example.com"   # the address _login uses


def _queue(c, agent_id, request_id):
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": request_id}, buffered=False)
    next(iter(r.response))
    r.close()


def _texts(fake):
    run_id = next(iter(fake.runs))
    return [e["payload"].get("text") for e in fake.events[run_id]
            if e["type"] == "agent.text"]


def test_a_run_queued_before_the_pause_answers_paused_without_a_model(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.worker import RunWorker
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    _queue(c, agent_id, "paused-1")
    assert fake.runs, "the run was not queued before the pause"
    assert svc.set_paused(EMAIL, True) is True
    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: pytest.fail("paused turn hit a model"))
    monkeypatch.setattr(fake, "recent_messages",
                        lambda *a, **k: pytest.fail("paused turn read records"))

    RunWorker(cfg, fake, svc, "pause-worker").run_once()

    assert _texts(fake) == [beta.PAUSED_TEXT]
    assert next(iter(fake.runs.values()))["status"] == "completed"


def test_resuming_lets_the_next_turn_through(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.worker import RunWorker
    app, c, fake, agent_id, tenant, _ = _chat_app(
        cfg, svc, monkeypatch, reply="back again")
    svc.set_paused(EMAIL, True)
    svc.set_paused(EMAIL, False)
    _queue(c, agent_id, "resumed-1")
    RunWorker(cfg, fake, svc, "resume-worker").run_once()
    assert _texts(fake) == ["back again"]


def test_refresh_and_upload_refuse_while_paused(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, conn_id = _chat_app(cfg, svc, monkeypatch)
    direct = c.post("/api/connections/direct",
                    json={"consent": True}).get_json()["id"]
    svc.set_paused(EMAIL, True)
    r = c.post(f"/api/connections/{conn_id}/refresh")
    assert r.status_code == 423
    # No assistant speaks here, so not the chat sentence (#856 review).
    assert r.get_json()["message"] == beta.PAUSED_RECORDS_TEXT
    r = c.post(f"/api/connections/{direct}/upload", data=b"{}",
               content_type="application/fhir+json")
    assert r.status_code == 423
    assert r.get_json() == {"error": "records_paused",
                            "message": beta.PAUSED_RECORDS_TEXT}
    assert fake.purged == []


def test_a_sample_is_refused_while_paused(cfg, svc, monkeypatch):  # noqa: F811
    """Nothing new is added while paused, the made-up records included:
    hiding the button is not the control (#856 sign-off F2)."""
    from careagents.app import create_app
    from tests.test_careagents import FakeClient, _login
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    svc.set_paused(EMAIL, True)
    r = c.post("/api/connections/sample")
    assert r.status_code == 423
    assert r.get_json() == {"error": "records_paused",
                            "message": beta.PAUSED_RECORDS_TEXT}
    assert fake.tenants == []
    with svc.session() as s:
        from careagents.models import Connection
        assert s.query(Connection).count() == 0
    # Resumed, the same tap works.
    svc.set_paused(EMAIL, False)
    assert c.post("/api/connections/sample").status_code == 200


def test_a_foreign_connection_is_still_a_404_while_paused(
        cfg, svc, monkeypatch):  # noqa: F811
    """The pause check sits after the ownership lookup, so it never tells
    a paused account anything about another account's connection ids."""
    app, c, fake, agent_id, tenant, conn_id = _chat_app(cfg, svc, monkeypatch)
    svc.set_paused(EMAIL, True)
    assert c.post("/api/connections/conn_nothere/refresh").status_code == 404
    assert c.post("/api/connections/conn_nothere/upload", data=b"{}",
                  content_type="application/fhir+json").status_code == 404


def test_the_paused_sentences_are_plain_and_phi_free():
    for text in (beta.PAUSED_TEXT, beta.PAUSED_RECORDS_TEXT,
                 beta.PAUSED_HUB_TEXT):
        assert "—" not in text
        assert "contactus@healthclaw.io" in text
    assert "answer" not in beta.PAUSED_RECORDS_TEXT
