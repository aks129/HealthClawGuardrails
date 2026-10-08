"""The Mac relay keeps real records off the text line, as Sendblue does.

Sendblue checks `sendblue_surface.real_records_blocked` on arrival and
again before delivery. The relay's two routes, inbound and runs, apply the
same rule: a non-sample connection queues no turn, and an answer owed from
before a switch to real records is withheld.

Synthetic handles only (555 numbers).
"""

from __future__ import annotations

import pytest

from careagents import sendblue_surface
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)
from tests.test_careagents_imessage import HDRS, PHONE, _inbound, _pair, _run
from tests.test_careagents_sendblue import _make_real


def _answer(c, run_id):
    return c.get(f"/api/surfaces/imessage/runs/{run_id}", headers=HDRS,
                  query_string={"handle": PHONE})


@pytest.mark.parametrize("kind", ["fasten", "wearables", "somethingnew"])
def test_a_real_records_agent_queues_no_turn_and_gets_the_app_pointer(
        cfg, svc, monkeypatch, kind):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _make_real(svc, agent_id, kind)
    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: pytest.fail("model was called"))
    runs_before = len(hc.runs)
    r = _inbound(c, PHONE, "how is my a1c?")
    assert r.status_code == 200
    body = r.get_json()
    assert body == {"reply": sendblue_surface.real_records_text(cfg.origin)}
    assert "run_id" not in body
    assert len(hc.runs) == runs_before


def test_a_sample_agent_is_answered_through_the_relay(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                         reply="Sample answer.")
    _pair(c, agent_id)
    r = _inbound(c, PHONE, "hello")
    assert r.status_code == 202
    run_id = r.get_json()["run_id"]
    _run(app)
    answer = _answer(c, run_id)
    assert answer.status_code == 200
    assert answer.get_json()["reply"] == "Sample answer."


def test_an_answer_owed_from_before_a_switch_to_real_records_is_withheld(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, hc, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                         reply="Your A1c is 6.1.")
    _pair(c, agent_id)
    r = _inbound(c, PHONE, "how is my a1c?")     # queued while sample
    assert r.status_code == 202
    run_id = r.get_json()["run_id"]
    _run(app)
    _make_real(svc, agent_id)                   # real records, mid-run
    answer = _answer(c, run_id)
    assert answer.status_code == 200
    body = answer.get_json()
    assert body["reply"] == sendblue_surface.real_records_text(cfg.origin)
    assert "6.1" not in str(body)
