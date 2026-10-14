"""QA for #906: a mid-run switch to real records the way a person makes one.

The PR's first mid-run tests flipped `Connection.kind` in place on the same
tenant. Nothing in CareAgents does that: every real-record connection gets
a new tenant, and a person switches by moving the assistant to it
(`POST /api/agents/<id>/connection`). Polled after such a move, the run
lives under the old tenant; asked under the new one, the live engine
answers 401, which the route used to report as an outage (503) that the
relay retries until it times out.

The runs route now checks the assistant's current connection before it
asks anything about the run, so the relay gets the app pointer at once and
nothing from the run is fetched.

Synthetic data only (555 numbers).
"""

from __future__ import annotations

from careagents import sendblue_surface
from careagents.healthclaw import HealthClawError
from tests.careagents_consent_helpers import consented
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, sample_framed, svc)
from tests.test_careagents_imessage import HDRS, PHONE, _inbound, _pair, _run

CANARY = "QA906-OWED-ANSWER"


def _owed_run_then_move(cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch, reply=CANARY)
    assert _pair(c, agent_id).status_code == 200
    r = _inbound(c, PHONE, "how is my a1c?")          # queued while sample
    assert r.status_code == 202
    run_id = r.get_json()["run_id"]
    _run(app)
    real = c.post("/api/connections/direct", json=consented())
    assert real.status_code == 200, real.get_json()
    from careagents.models import Connection
    with svc.session() as s:                          # as an upload leaves it
        s.get(Connection, real.get_json()["id"]).status = "active"
    moved = c.post(f"/api/agents/{agent_id}/connection",
                   json={"connection_id": real.get_json()["id"]})
    assert moved.status_code == 200, moved.get_json()

    # From here on, nothing about the run may be fetched. Were it asked,
    # this stands in for the live engine: 401 under the new tenant.
    asked = []

    def _trap(*a, **k):
        asked.append(a)
        raise HealthClawError("unauthorized", 401)
    monkeypatch.setattr(hc, "get_agent_run", _trap)
    monkeypatch.setattr(hc, "agent_run_events", _trap)
    polls = [c.get(f"/api/surfaces/imessage/runs/{run_id}", headers=HDRS,
                   query_string={"handle": PHONE}) for _ in range(2)]
    return polls, asked


def test_an_owed_answer_never_follows_a_move_to_real_records(
        cfg, svc, monkeypatch):  # noqa: F811
    polls, _ = _owed_run_then_move(cfg, svc, monkeypatch)
    for polled in polls:
        assert CANARY not in polled.get_data(as_text=True)


def test_a_move_to_real_records_mid_run_gets_the_app_pointer(
        cfg, svc, monkeypatch):  # noqa: F811
    polls, asked = _owed_run_then_move(cfg, svc, monkeypatch)
    for polled in polls:                              # every poll, at once
        assert polled.status_code == 200, polled.get_json()
        assert polled.get_json()["reply"] == (
            sendblue_surface.real_records_text(cfg.origin))
    assert asked == []


def test_a_sample_assistant_still_reads_its_run(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch, reply=CANARY)
    _pair(c, agent_id)
    run_id = _inbound(c, PHONE, "hello").get_json()["run_id"]
    pending = c.get(f"/api/surfaces/imessage/runs/{run_id}", headers=HDRS,
                    query_string={"handle": PHONE})
    assert pending.status_code == 202                 # still running
    _run(app)
    done = c.get(f"/api/surfaces/imessage/runs/{run_id}", headers=HDRS,
                 query_string={"handle": PHONE})
    assert done.status_code == 200
    assert done.get_json()["reply"] == sample_framed(CANARY)
