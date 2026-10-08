"""SENDBLUE_REAL_RECORDS opens the Sendblue line only.

The flag stands for Sendblue's HIPAA instance and BAA. The Mac relay and
Telegram have neither, so with the flag on they still refuse an agent on
real records, while Sendblue answers it.

Synthetic data only (555 numbers).
"""

from __future__ import annotations

import pytest

from careagents import sendblue_surface
from careagents.config import Config
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg)
from tests.test_careagents_imessage import HDRS, PHONE, _inbound
from tests.test_careagents_imessage import _pair as _relay_pair
from tests.test_careagents_imessage import _run
from tests.test_careagents_sendblue import (
    PHONE as SB_PHONE, _app, _deliverer, _env, _hook, _make_real)
from tests.test_careagents_sendblue import _pair as _sb_pair


@pytest.fixture
def on_cfg(cfg):  # noqa: F811
    return Config(env=_env(cfg, SENDBLUE_REAL_RECORDS="1"))


@pytest.fixture
def on_svc(on_cfg):
    from careagents.accounts import AccountService
    service = AccountService(on_cfg)
    yield service
    service.engine.dispose()


def test_the_flag_is_on_in_these_tests(on_cfg):
    assert on_cfg.sendblue_real_records is True


def test_sendblue_answers_a_real_agent_with_the_flag_on(
        on_cfg, on_svc, monkeypatch):
    app, c, fake, hc, agent_id = _app(on_cfg, on_svc, monkeypatch,
                                      reply="Real answer.")
    _sb_pair(c, agent_id)
    _make_real(on_svc, agent_id)
    fake.sent.clear()
    runs_before = len(hc.runs)
    _hook(c, "hello", handle="scope-1")
    assert len(hc.runs) == runs_before + 1          # a turn was queued
    _run(app)
    _deliverer(app, fake).once()
    # The worker's own consent check may answer it; the transport does not
    # hold it back.
    assert len(fake.sent) == 1 and fake.sent[0][0] == SB_PHONE
    assert fake.sent[0][1] != sendblue_surface.real_records_text(
        on_cfg.origin)


def test_the_relay_still_refuses_a_real_agent_with_the_flag_on(
        on_cfg, on_svc, monkeypatch):
    app, c, hc, agent_id, *_ = _chat_app(on_cfg, on_svc, monkeypatch)
    _relay_pair(c, agent_id)
    _make_real(on_svc, agent_id)
    runs_before = len(hc.runs)
    r = _inbound(c, PHONE, "how is my a1c?")
    assert r.status_code == 200
    assert r.get_json() == {
        "reply": sendblue_surface.real_records_text(on_cfg.origin)}
    assert len(hc.runs) == runs_before


def test_the_relay_still_withholds_a_mid_run_switch_with_the_flag_on(
        on_cfg, on_svc, monkeypatch):
    app, c, hc, agent_id, *_ = _chat_app(on_cfg, on_svc, monkeypatch,
                                         reply="Your A1c is 6.1.")
    _relay_pair(c, agent_id)
    run_id = _inbound(c, PHONE, "how is my a1c?").get_json()["run_id"]
    _run(app)
    _make_real(on_svc, agent_id)
    body = c.get(f"/api/surfaces/imessage/runs/{run_id}", headers=HDRS,
                 query_string={"handle": PHONE}).get_json()
    assert body["reply"] == sendblue_surface.real_records_text(on_cfg.origin)
    assert "6.1" not in str(body)


def test_telegram_still_refuses_a_real_agent_with_the_flag_on(
        on_cfg, on_svc, monkeypatch):
    app, c, hc, agent_id, *_ = _chat_app(on_cfg, on_svc, monkeypatch)
    # A code minted while sample, then the switch: bind must refuse too.
    code = c.post("/api/surfaces/telegram",
                  json={"agent_id": agent_id}).get_json()["code"]
    _make_real(on_svc, agent_id)
    r = c.post("/api/surfaces/telegram", json={"agent_id": agent_id})
    assert r.status_code == 409
    assert r.get_json()["error"] == "real_records"
    bind = app.test_client().post(
        "/api/surfaces/telegram/bind",
        json={"code": f"care_{code}", "chat_id": 4242},
        headers={"X-Internal-Secret": on_cfg.mint_secret})
    assert bind.status_code == 409
    assert hc.bound == []
