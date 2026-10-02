"""Pausing one account's real records (beta pathway spec section 4.6).

The spec's test, section 6: "A paused account's turns answer the paused
message and call no provider." Plus what pausing has to mean everywhere
else records are read or sent from this side: the labs timeline, the
brief, the approvals list, the review page and its submit, refresh,
upload, and starting a new real connection. Sample records are synthetic
and stay available.
"""

from __future__ import annotations

import json

import pytest

from careagents.accounts import RECORDS_PAUSED_TEXT
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, _turn, cfg, svc)

EMAIL = "tester@example.org"
_EMPTY_BUNDLE = json.dumps({"resourceType": "Bundle", "type": "collection",
                            "entry": []})


@pytest.fixture
def model_calls(monkeypatch):
    """Count every model call; answer immediately."""
    from careagents import agent as agent_mod

    calls = []

    class _Turn:
        text, tool_calls, raw_tool_calls = "from the model", [], []

    def complete(*args, **kwargs):
        calls.append(1)
        return _Turn()

    monkeypatch.setattr(agent_mod.llm, "complete", complete)
    return calls


def _app(cfg, svc, monkeypatch):  # noqa: F811
    """Signed in, with a real (file upload) connection and its assistant,
    and a sample connection with its own."""
    from careagents.app import create_app

    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    real = c.post("/api/connections/direct",
                  json={"consent": True}).get_json()["id"]
    real_agent = c.post("/api/agents", json={
        "name": "Real", "connection_id": real}).get_json()["id"]
    sample = c.post("/api/connections/sample").get_json()["id"]
    sample_agent = c.post("/api/agents", json={
        "name": "Sample", "connection_id": sample}).get_json()["id"]
    return c, real, real_agent, sample_agent


def test_a_paused_accounts_turns_answer_paused_and_call_no_model(
        cfg, svc, monkeypatch, model_calls):  # noqa: F811
    c, _real, real_agent, _sample_agent = _app(cfg, svc, monkeypatch)
    assert svc.pause_real_records(EMAIL) is True

    body = _turn(c, real_agent, "what are my labs?").get_data(as_text=True)

    assert model_calls == []
    assert json.dumps(RECORDS_PAUSED_TEXT)[1:-1] in body
    assert '"type": "done"' in body


def test_a_turn_queued_before_the_pause_still_answers_paused(
        cfg, svc, monkeypatch, model_calls):  # noqa: F811
    from careagents.worker import RunWorker

    c, _real, real_agent, _sample_agent = _app(cfg, svc, monkeypatch)
    runtime = c.application.extensions["careagents_runtime"]
    response = c.post("/api/chat", json={
        "agent_id": real_agent, "message": "queued"}, buffered=False)
    next(iter(response.response))  # accepted: the run exists, not yet run
    svc.pause_real_records(EMAIL)

    RunWorker(runtime["config"], runtime["client"], runtime["accounts"],
              "pause-worker").run_once()

    assert model_calls == []
    assert json.dumps(RECORDS_PAUSED_TEXT)[1:-1] in response.get_data(
        as_text=True)


def test_sample_records_stay_available_while_paused(
        cfg, svc, monkeypatch, model_calls):  # noqa: F811
    c, _real, _real_agent, sample_agent = _app(cfg, svc, monkeypatch)
    svc.pause_real_records(EMAIL)

    body = _turn(c, sample_agent, "hello").get_data(as_text=True)

    assert model_calls == [1]
    assert "from the model" in body
    assert c.get("/api/labs/timeline",
                 query_string={"agent": sample_agent}).status_code != 423


def test_resuming_lets_turns_reach_the_model_again(
        cfg, svc, monkeypatch, model_calls):  # noqa: F811
    c, _real, real_agent, _sample_agent = _app(cfg, svc, monkeypatch)
    svc.pause_real_records(EMAIL)
    _turn(c, real_agent, "one")
    assert svc.resume_real_records(EMAIL) is True

    body = _turn(c, real_agent, "two").get_data(as_text=True)

    assert model_calls == [1]
    assert "from the model" in body


def test_reads_and_sends_outside_chat_are_refused_while_paused(
        cfg, svc, monkeypatch, model_calls):  # noqa: F811
    c, real, real_agent, _sample_agent = _app(cfg, svc, monkeypatch)
    svc.pause_real_records(EMAIL)

    probes = {
        "labs timeline": c.get("/api/labs/timeline",
                               query_string={"agent": real_agent}),
        "brief": c.get("/brief", query_string={"agent": real_agent}),
        "approvals": c.get(f"/agents/{real_agent}/approvals"),
        "refresh": c.post(f"/api/connections/{real}/refresh", json={}),
        "upload": c.post(f"/api/connections/{real}/upload",
                         data=_EMPTY_BUNDLE,
                         content_type="application/fhir+json"),
    }
    for name, r in probes.items():
        assert r.status_code == 423, name
        assert "Your records are paused" in r.get_data(as_text=True), name


def test_the_review_page_and_its_submit_are_refused_while_paused(
        cfg, svc, monkeypatch, model_calls):  # noqa: F811
    c, _real, real_agent, _sample_agent = _app(cfg, svc, monkeypatch)
    # FakeClient owns one pending form, act-1, on every tenant.
    assert c.get(f"/review/{real_agent}/act-1").status_code == 200
    svc.pause_real_records(EMAIL)

    page = c.get(f"/review/{real_agent}/act-1")
    submit = c.post(f"/review/{real_agent}/act-1/submit",
                    json={"nka": "true"})

    assert page.status_code == submit.status_code == 423
    assert submit.get_json()["error"] == "records_paused"


def test_a_paused_account_cannot_start_a_new_real_connection(
        cfg, svc, monkeypatch, model_calls):  # noqa: F811
    c, _real, _real_agent, _sample_agent = _app(cfg, svc, monkeypatch)
    svc.pause_real_records(EMAIL)

    tiers = {m["id"]: m["tier"] for m in
             c.get("/api/connections/catalog").get_json()["connectors"]}
    assert tiers["fasten"] == "soon"
    assert c.post("/api/connections/direct",
                  json={"consent": True}).status_code != 200


def test_pause_and_resume_report_whether_anything_changed(svc, monkeypatch):  # noqa: F811
    from careagents.accounts import AuthError
    from tests.test_careagents import _make_account

    acct_id = _make_account(svc, monkeypatch, EMAIL).id
    assert svc.real_records_paused(acct_id) is False
    assert svc.pause_real_records(" Tester@Example.org ") is True
    assert svc.pause_real_records(EMAIL) is False
    assert svc.real_records_paused(acct_id) is True
    assert [r["email"] for r in svc.paused_accounts()] == [EMAIL]
    assert svc.resume_real_records(EMAIL) is True
    assert svc.resume_real_records(EMAIL) is False
    assert svc.paused_accounts() == []
    with pytest.raises(AuthError):
        svc.pause_real_records("nobody@example.org")


def test_the_operator_commands_pause_list_and_resume(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.app import create_app
    from tests.test_careagents import _make_account

    _make_account(svc, monkeypatch, EMAIL)
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    runner = app.test_cli_runner()

    r = runner.invoke(args=["records", "pause", EMAIL])
    assert r.exit_code == 0 and f"paused {EMAIL}" in r.output
    r = runner.invoke(args=["records", "paused"])
    assert EMAIL in r.output
    r = runner.invoke(args=["records", "resume", EMAIL])
    assert r.exit_code == 0 and f"resumed {EMAIL}" in r.output
    assert "no paused accounts" in runner.invoke(
        args=["records", "paused"]).output
    r = runner.invoke(args=["records", "pause", "nobody@example.org"])
    assert r.exit_code != 0
