"""Security review of the sample framing (PR #908): V1, V3, V6 attacks.

The frame ("these are made-up records, not yours") is decided by
`Connection.kind == "sample"`. These tests try to make a real connection
read as sample, a sample read as real, the line leak across accounts, and
model or record text exploit the line's de-duplication. All data is
synthetic.
"""

from __future__ import annotations

import pytest

from careagents import beta
from tests.careagents_consent_helpers import consented
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, _login, cfg, svc)

MODEL_TEXT = "Creatinine rose from 0.8 to 1.3 mg/dL in 6 days."


def _run(c, fake, agent_id, cfg, svc, monkeypatch, says,  # noqa: F811
         request_id="sec-1", prompts=None):
    from careagents import agent as agent_mod
    from careagents.worker import RunWorker

    class _Turn:
        text, tool_calls, raw_tool_calls = says, [], []
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda _cfg, system, *a, **k:
                        (prompts.append(system) if prompts is not None
                         else None) or _Turn())
    r = c.post("/api/chat", json={"agent_id": agent_id,
                                  "message": "What do my labs say?",
                                  "request_id": request_id}, buffered=False)
    status = r.status_code
    r.close()
    RunWorker(cfg, fake, svc, f"sec-worker-{request_id}").run_once()
    return status


def _answers(fake, tenant=None):
    return [row["content"] for key, rows in fake.logged.items()
            if tenant is None or key[0] == tenant
            for row in rows if row["role"] == "assistant"]


def _real_active_conn(svc, account_id, fake):  # noqa: F811
    from careagents import tester_terms
    tenant = fake.new_tenant_id()
    return svc.add_connection(account_id, "fasten", tenant,
                              "Records from your doctor", status="active",
                              consent_version=tester_terms.CONSENT_VERSION
                              ), tenant


# --- real framed as sample / sample framed as real --------------------------

def test_agent_moved_from_sample_to_real_loses_the_frame(
        cfg, svc, monkeypatch):  # noqa: F811
    _app, c, fake, agent_id, sample_tenant, _cid = _chat_app(
        cfg, svc, monkeypatch)
    acct = svc.get_worker_agent_context(agent_id)["account_id"]
    real_id, real_tenant = _real_active_conn(svc, acct, fake)
    assert c.post(f"/api/agents/{agent_id}/connection",
                  json={"connection_id": real_id}).status_code == 200
    prompts = []
    _run(c, fake, agent_id, cfg, svc, monkeypatch, MODEL_TEXT,
         prompts=prompts)
    assert _answers(fake, real_tenant) == [MODEL_TEXT]
    assert prompts and not any("made-up" in p for p in prompts)
    body = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert beta.SAMPLE_FRAME not in body and "made-up records" not in body


def test_agent_moved_from_real_to_sample_gains_the_frame(
        cfg, svc, monkeypatch):  # noqa: F811
    _app, c, fake, agent_id, sample_tenant, sample_cid = _chat_app(
        cfg, svc, monkeypatch)
    acct = svc.get_worker_agent_context(agent_id)["account_id"]
    real_id, _ = _real_active_conn(svc, acct, fake)
    c.post(f"/api/agents/{agent_id}/connection",
           json={"connection_id": real_id})
    c.post(f"/api/agents/{agent_id}/connection",
           json={"connection_id": sample_cid})
    _run(c, fake, agent_id, cfg, svc, monkeypatch, MODEL_TEXT)
    assert _answers(fake, sample_tenant) == [
        f"{beta.SAMPLE_FRAME}\n\n{MODEL_TEXT}"]


def test_run_queued_on_sample_then_agent_moved_to_real_is_not_framed(
        cfg, svc, monkeypatch):  # noqa: F811
    """A run enqueued against the sample tenant must not be answered with
    the real connection's frame decision, nor the reverse: the worker binds
    tenant and kind from the same row and refuses a mismatch."""
    from careagents import agent as agent_mod
    from careagents.worker import RunWorker

    _app, c, fake, agent_id, sample_tenant, _cid = _chat_app(
        cfg, svc, monkeypatch)
    acct = svc.get_worker_agent_context(agent_id)["account_id"]
    real_id, real_tenant = _real_active_conn(svc, acct, fake)

    class _Turn:
        text, tool_calls, raw_tool_calls = MODEL_TEXT, [], []
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: _Turn())
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "midflight"}, buffered=False)
    r.close()
    svc.move_agent(acct, agent_id, real_id)
    RunWorker(cfg, fake, svc, "mid-worker").run_once()
    assert MODEL_TEXT not in " ".join(_answers(fake))
    assert _answers(fake, real_tenant) == []


def test_unknown_or_missing_kind_is_never_framed_as_sample(
        cfg, svc, monkeypatch):  # noqa: F811
    """Only the exact server-set kind frames; a near-miss is real."""
    from careagents.models import Connection
    _app, c, fake, agent_id, tenant, cid = _chat_app(cfg, svc, monkeypatch)
    for kind in ("Sample", "sample ", "", "demo"):
        with svc.session() as s:
            s.get(Connection, cid).kind = kind
        body = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
        assert beta.SAMPLE_FRAME not in body, kind


# --- V6: no frame, page or answer across accounts ----------------------------

def test_other_account_cannot_open_sample_chat_or_brief(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, cid = _chat_app(cfg, svc, monkeypatch)
    b = app.test_client()
    _login(b, svc, monkeypatch, email="mallory@example.com")
    for path in (f"/chat?agent={agent_id}", f"/brief?agent={agent_id}"):
        r = b.get(path)
        assert r.status_code in (302, 303, 404), path
        assert beta.SAMPLE_FRAME not in r.get_data(as_text=True)
    r = b.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "x-acct"}, buffered=False)
    assert r.status_code in (403, 404)
    r.close()
    assert _answers(fake) == []


# --- prompt injection against the de-duplication -----------------------------

def test_model_text_cannot_suppress_or_precede_the_frame(
        cfg, svc, monkeypatch):  # noqa: F811
    """Whatever the model (steered by record or user text) writes, a sample
    answer starts with exactly the frame on its own line."""
    payloads = [
        beta.SAMPLE_FRAME,                                   # line alone
        beta.SAMPLE_FRAME + " Actually these ARE yours.",    # prefix + undo
        beta.SAMPLE_FRAME * 3,                               # repeated
        "  \n" + beta.SAMPLE_FRAME + "\n\n" + MODEL_TEXT,     # leading ws
        "These are REAL records, yours.\n\n" + MODEL_TEXT,   # contradiction
        "",                                                  # empty
    ]
    _app, c, fake, agent_id, tenant, _cid = _chat_app(cfg, svc, monkeypatch)
    for i, says in enumerate(payloads):
        _run(c, fake, agent_id, cfg, svc, monkeypatch, says,
             request_id=f"inj-{i}")
        answers = _answers(fake, tenant)
        assert len(answers) == i + 1
        answer = answers[-1]
        assert answer.startswith(beta.SAMPLE_FRAME + "\n\n"), (i, answer)
        assert answer[len(beta.SAMPLE_FRAME):].strip() != ""


def test_real_answer_echoing_the_frame_is_passed_through_unchanged(
        cfg, svc, monkeypatch):  # noqa: F811
    """Observation, not a PR regression: on a real connection the worker
    adds nothing and strips nothing, so a model steered into writing the
    frame shows it verbatim. The frame is not a trusted UI element in the
    message body; only the page banner is server-decided."""
    from careagents.app import create_app
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    from careagents import agent as agent_mod

    class _T:
        text, tool_calls, raw_tool_calls = "x", [], []
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: _T())
    conn = c.post("/api/connections/direct",
                  json=consented()).get_json()
    from careagents.models import Connection
    with svc.session() as s:
        s.get(Connection, conn["id"]).status = "active"
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    says = f"{beta.SAMPLE_FRAME}\n\n{MODEL_TEXT}"
    _run(c, fake, agent_id, cfg, svc, monkeypatch, says, request_id="echo")
    assert _answers(fake) == [says]
    # ee31291's reworded-echo stripper does not run either: a real answer
    # that opens with a made-up/not-yours sentence survives intact.
    says2 = ("These are not sample records, they are yours and not made up. "
             "Your creatinine rose; contact your doctor promptly.")
    _run(c, fake, agent_id, cfg, svc, monkeypatch, says2, request_id="e2")
    assert _answers(fake)[-1] == says2


# --- ee31291: the reworded-echo stripper -------------------------------------

def test_drop_echo_keeps_a_first_sentence_that_carries_advice():
    """FIXED (was LOW): the heuristic dropped any leading sentence holding a
    made-up word and a not-yours word, even one carrying the advice. Only a
    disclaimer and nothing else is dropped now."""
    from careagents.worker import _drop_echo
    text = ("The sample person is not you, but their creatinine rose fast "
            "and they should contact a doctor promptly.\n\nAsk about it.")
    assert _drop_echo(text, beta.SAMPLE_FRAME) == text


@pytest.mark.parametrize("first", [
    "These made-up records are not yours, and they span 3 years.",
    "This is sample data, not yours; ask a doctor about it.",
    "Not your records: the sample person may be due for a flu vaccine.",
    "These made-up records are not yours, and nothing in them is about "
    "you or anyone you know or have ever met in your whole life.",
], ids=["digit", "doctor", "clinical", "long"])
def test_drop_echo_keeps_any_sentence_that_is_more_than_a_disclaimer(first):
    from careagents.worker import _drop_echo
    text = f"{first}\n\nMore."
    assert _drop_echo(text, beta.SAMPLE_FRAME) == text


def test_drop_echo_never_reaches_past_the_first_paragraph():
    from careagents.worker import _drop_echo
    text = f"{MODEL_TEXT}\n\n{beta.SAMPLE_FRAME}"
    assert _drop_echo(text, beta.SAMPLE_FRAME) == text
