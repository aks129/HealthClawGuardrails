"""The worker refuses a real-record turn whose model host is not approved.

The approved-host check in careagents/config.py runs only on a process
where CARE_REAL_RECORDS is not off, and a fatal error there is a boot
failure. The worker makes the model calls and may not carry the web's
CARE_REAL_RECORDS, so a real-record turn could reach an unapproved host.
The worker now checks per turn, before any record read or model call,
whatever its own CARE_REAL_RECORDS says. Sample turns are unaffected.

Synthetic data only.
"""

from __future__ import annotations

import json
import logging

import pytest

from careagents import agent, funnel, mail, tester_terms
from careagents.config import Config, ConfigError
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _turn, cfg, svc)

UNAPPROVED = "https://llm.unvetted.example/v1"


def _frames(body: str) -> list[dict]:
    return [json.loads(line[len("data: "):])
            for line in body.splitlines() if line.startswith("data: ")]


def _make_real(svc, tenant):  # noqa: F811
    """The chat app's sample connection, as a consented real-record one."""
    from careagents.models import Connection
    with svc.session() as s:
        for c in s.query(Connection).filter_by(tenant_id=tenant).all():
            c.kind = "fasten"
            c.consent_version = tester_terms.CONSENT_VERSION


def _one_turn(cfg, svc, monkeypatch, *, real, host):  # noqa: F811
    """One turn. Returns (frames, model calls, owner mails)."""
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    if real:
        _make_real(svc, tenant)
    if host:
        monkeypatch.setattr(cfg, "openai_base", host)
    calls = []
    real_complete = agent.llm.complete

    def counting(*a, **k):
        calls.append(1)
        return real_complete(*a, **k)
    monkeypatch.setattr(agent.llm, "complete", counting)
    sent = []
    monkeypatch.setattr(cfg, "owner_emails",
                        frozenset({"owner@example.com"}))
    monkeypatch.setattr(mail, "send_notice",
                        lambda *a: sent.append(a) or mail.SENT)
    monkeypatch.setattr("careagents.app.time.sleep", lambda _s: None)
    r = _turn(c, agent_id, "what were my results")
    return _frames(r.get_data(as_text=True)), calls, sent


def test_a_real_record_turn_to_an_unapproved_host_is_refused(
        cfg, svc, monkeypatch):  # noqa: F811
    """MUTATION: drop the per-turn check in the worker -> red."""
    # The worker without the web's setting: the boot check never ran.
    monkeypatch.setattr(cfg, "real_records", "off")
    assert cfg.provider == "openai"
    frames, calls, sent = _one_turn(cfg, svc, monkeypatch,
                                    real=True, host=UNAPPROVED)
    assert calls == [], "the model was called for an unapproved host"
    errors = [f for f in frames if f["type"] == "error"]
    assert errors and errors[-1]["text"] == agent.UNAVAILABLE_TEXT
    assert not any(f["type"] == "text" for f in frames)
    today = funnel.daily(svc.session, days=1)["days"][0]
    assert today["chat_error"] == 1 and today["model_auth"] == 1
    assert len(sent) == 1


def test_a_real_record_turn_to_an_approved_host_passes(
        cfg, svc, monkeypatch):  # noqa: F811
    """MUTATION: refuse every real-record turn -> red."""
    frames, calls, sent = _one_turn(cfg, svc, monkeypatch,
                                    real=True, host=None)
    assert calls, "an approved host must reach the model"
    assert frames[-1]["type"] == "done"
    assert frames[-1]["status"] == "completed"
    assert sent == []


def test_a_host_named_by_the_owner_passes(cfg, svc, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cfg, "extra_model_hosts",
                        frozenset({"llm.unvetted.example"}))
    frames, calls, _ = _one_turn(cfg, svc, monkeypatch,
                                 real=True, host=UNAPPROVED)
    assert calls and frames[-1]["status"] == "completed"


def test_a_sample_turn_is_unaffected_by_the_host(cfg, svc, monkeypatch):  # noqa: F811
    """MUTATION: check the host on sample turns too -> red."""
    frames, calls, sent = _one_turn(cfg, svc, monkeypatch,
                                    real=False, host=UNAPPROVED)
    assert calls
    assert frames[-1]["status"] == "completed"
    assert not any(f["type"] == "error" for f in frames)
    assert sent == []


# --- config ------------------------------------------------------------------

def _cfg(**env):
    return Config(env={"CARE_DATABASE_URL": "sqlite:///:memory:",
                       "CARE_RP_ID": "localhost",
                       "CARE_ORIGIN": "http://localhost", **env})


@pytest.mark.parametrize("env,vetted", [
    ({"ANTHROPIC_API_KEY": "k"}, True),
    ({"ANTHROPIC_API_KEY": "k",
      "ANTHROPIC_BASE_URL": "https://proxy.example"}, False),
    ({"OPENAI_API_KEY": "k"}, True),
    ({"OPENAI_API_KEY": "k", "OPENAI_BASE_URL": UNAPPROVED}, False),
    ({"OPENAI_API_KEY": "k", "OPENAI_BASE_URL": UNAPPROVED,
      "CARE_REAL_RECORDS_MODEL_HOSTS": "LLM.unvetted.example"}, True),
    # A suffix or a lookalike is not the host.
    ({"OPENAI_API_KEY": "k",
      "OPENAI_BASE_URL": "https://api.openai.com.evil.example/v1"}, False),
])
def test_the_host_verdict_is_known_with_real_records_off(env, vetted):
    c = _cfg(**env)
    assert c.real_records == "off"
    assert c.model_host_vetted is vetted


def test_the_boot_check_still_refuses_where_real_records_are_on():
    with pytest.raises(ConfigError):
        _cfg(OPENAI_API_KEY="k", OPENAI_BASE_URL=UNAPPROVED,
             CARE_REAL_RECORDS="on")


# --- the boot warning ----------------------------------------------------------

def test_the_worker_warns_when_real_records_exist_but_the_setting_is_unset(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    from careagents.worker import warn_real_records_unset
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    monkeypatch.setattr(cfg, "real_records", "off")
    assert warn_real_records_unset(cfg, svc) is False       # sample only
    _make_real(svc, tenant)
    with caplog.at_level(logging.WARNING, logger="careagents.worker"):
        assert warn_real_records_unset(cfg, svc) is True
    assert "mirror the web's CARE_REAL_RECORDS" in caplog.text
    monkeypatch.setattr(cfg, "real_records", "allowlist")
    assert warn_real_records_unset(cfg, svc) is False


def test_the_boot_warning_never_stops_the_worker(cfg, monkeypatch):  # noqa: F811
    from careagents.worker import warn_real_records_unset

    class _Broken:
        def session(self):
            raise RuntimeError("database gone")
    assert warn_real_records_unset(cfg, _Broken()) is False
