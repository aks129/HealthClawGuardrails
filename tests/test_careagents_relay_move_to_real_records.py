"""QA for #906: a mid-run switch to real records the way a person makes one.

The PR's own mid-run tests flip `Connection.kind` in place on the same
tenant. Nothing in CareAgents does that: every real-record connection gets
a new tenant, and a person switches by moving the assistant to it
(`POST /api/agents/<id>/connection`). Polled after such a move, the run is
looked up under the new tenant, where it does not exist.

Two rows: the answer must never come back (holds), and the relay should
get the app pointer rather than an outage (spec; pinned as a strict xfail
until the run lookup handles a moved agent).

Synthetic data only (555 numbers).
"""

from __future__ import annotations

import pytest

from careagents import sendblue_surface
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)
from tests.test_careagents_imessage import HDRS, PHONE, _inbound, _pair, _run

CANARY = "QA906-OWED-ANSWER"


def _owed_run_then_move(cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch, reply=CANARY)
    assert _pair(c, agent_id).status_code == 200
    r = _inbound(c, PHONE, "how is my a1c?")          # queued while sample
    assert r.status_code == 202
    run_id = r.get_json()["run_id"]
    _run(app)
    real = c.post("/api/connections/direct", json={"consent": True})
    assert real.status_code == 200, real.get_json()
    from careagents.models import Connection
    with svc.session() as s:                          # as an upload leaves it
        s.get(Connection, real.get_json()["id"]).status = "active"
    moved = c.post(f"/api/agents/{agent_id}/connection",
                   json={"connection_id": real.get_json()["id"]})
    assert moved.status_code == 200, moved.get_json()
    return c.get(f"/api/surfaces/imessage/runs/{run_id}", headers=HDRS,
                 query_string={"handle": PHONE})


def test_an_owed_answer_never_follows_a_move_to_real_records(
        cfg, svc, monkeypatch):  # noqa: F811
    polled = _owed_run_then_move(cfg, svc, monkeypatch)
    assert CANARY not in polled.get_data(as_text=True)


@pytest.mark.xfail(strict=True, reason="QA #906: after a move to a real "
                   "connection the run is looked up under the new tenant; "
                   "the relay gets an error, not the app pointer")
def test_a_move_to_real_records_mid_run_gets_the_app_pointer(
        cfg, svc, monkeypatch):  # noqa: F811
    polled = _owed_run_then_move(cfg, svc, monkeypatch)
    assert polled.status_code == 200, polled.get_json()
    assert polled.get_json()["reply"] == sendblue_surface.real_records_text(
        cfg.origin)
