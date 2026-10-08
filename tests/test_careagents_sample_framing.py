"""On the sample connection nobody can read a made-up result as their own.

A patient tester at 375px tapped "Explore with made-up records", asked
"What do my labs say?" and was told her creatinine rose in six days and to
contact her doctor promptly: today's dates, "your" throughout, and nothing
on the page saying the records were made up. She took it as about her.

Two places carry the framing, both on the sample only:
- the chat page shows one plain line saying the records are made up;
- every answer the worker finishes opens with that same line, so it holds
  for the web chat, a reload of the history, and a texted answer alike.

A real connection's page and answers are unchanged. All data is synthetic.
"""

from __future__ import annotations

from careagents import beta
from careagents.personas import system_prompt
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, _login, cfg, svc)

MODEL_TEXT = "Creatinine rose from 0.8 to 1.3 mg/dL in 6 days."


def _real_chat_app(cfg, svc, monkeypatch):  # noqa: F811
    """`_chat_app`, but on a real (uploaded-records) connection."""
    from careagents.app import create_app

    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/direct", json={"consent": True}).get_json()
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    return c, fake, agent_id


def _answer(c, fake, agent_id, cfg, svc, monkeypatch, prompts,  # noqa: F811
            says=MODEL_TEXT):
    """Ask once and run the worker; the answer as the transcript keeps it.
    The model `says` its text; `prompts` collects the system prompt each
    model call was given."""
    from careagents import agent as agent_mod
    from careagents.worker import RunWorker

    class _Turn:
        text, tool_calls, raw_tool_calls = says, [], []
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda _cfg, system, *a, **k:
                        prompts.append(system) or _Turn())
    r = c.post("/api/chat", json={"agent_id": agent_id,
                                  "message": "What do my labs say?",
                                  "request_id": "frame-1"}, buffered=False)
    assert r.status_code == 200
    r.close()
    RunWorker(cfg, fake, svc, "frame-worker").run_once()
    answers = [row["content"] for rows in fake.logged.values()
               for row in rows if row["role"] == "assistant"]
    assert len(answers) == 1
    return answers[0]


def test_a_sample_answer_opens_by_saying_the_records_are_made_up(
        cfg, svc, monkeypatch):  # noqa: F811
    _app, c, fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    prompts = []
    answer = _answer(c, fake, agent_id, cfg, svc, monkeypatch, prompts)
    assert answer.startswith(beta.SAMPLE_FRAME)
    assert answer.endswith(MODEL_TEXT)
    # And the model was told whose records these are.
    assert prompts and all("made-up" in p for p in prompts)


def test_a_model_that_repeats_the_line_still_shows_it_once(
        cfg, svc, monkeypatch):  # noqa: F811
    """From the second turn the model has read its earlier answers, each
    opening with the line, and may write it itself."""
    _app, c, fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    answer = _answer(c, fake, agent_id, cfg, svc, monkeypatch, [],
                     says=f"{beta.SAMPLE_FRAME}\n\n{MODEL_TEXT}")
    assert answer == f"{beta.SAMPLE_FRAME}\n\n{MODEL_TEXT}"


def test_a_real_answer_is_exactly_what_the_model_said(
        cfg, svc, monkeypatch):  # noqa: F811
    c, fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    prompts = []
    assert _answer(c, fake, agent_id, cfg, svc, monkeypatch,
                   prompts) == MODEL_TEXT
    assert prompts and not any("made-up" in p for p in prompts)


def test_the_sample_line_is_plain_and_says_not_yours():
    line = beta.SAMPLE_FRAME
    assert "made-up" in line and "not yours" in line
    assert len(line.split()) <= 15


def test_the_sample_chat_page_says_the_records_are_made_up(
        cfg, svc, monkeypatch):  # noqa: F811
    _app, c, _fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    body = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert 'class="beta-banner sample-banner"' in body
    assert beta.SAMPLE_FRAME in body
    # The banner sits above the log, so it does not scroll away.
    assert body.index(beta.SAMPLE_FRAME) < body.index('id="log"')
    # The greeting counts "in these made-up records", not "in your records".
    assert "in your records" not in body


def test_a_real_chat_page_has_no_sample_line(
        cfg, svc, monkeypatch):  # noqa: F811
    c, _fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    body = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert beta.SAMPLE_FRAME not in body
    assert "sample-banner" not in body
    assert "made-up" not in body


def test_the_prompt_frames_the_sample_and_only_the_sample():
    sample = system_prompt("Juniper", "calm", sample=True)
    real = system_prompt("Juniper", "calm")
    assert "made-up" in sample and "made-up" not in real
    assert sample.startswith(real.split("\n\n")[0])
    # Off by default: a caller that does not say sample gets today's prompt.
    assert system_prompt("Juniper", "calm", sample=False) == real


# --- the visit brief --------------------------------------------------------
# "Get me ready for my visit" opens /brief, which leads with the engine's
# trend sentence ("your creatinine rose ..."). The same line goes at the top.

def _brief_page(c, agent_id, monkeypatch):
    from tests.test_careagents_brief_lab_trends import RISE, _brief_with_trend
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant: _brief_with_trend())
    body = c.get(f"/brief?agent={agent_id}").get_data(as_text=True)
    assert RISE in body          # the trend section is on the page
    return body


def test_the_sample_brief_says_the_records_are_made_up(
        cfg, svc, monkeypatch):  # noqa: F811
    _app, c, _fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    body = _brief_page(c, agent_id, monkeypatch)
    assert 'class="beta-banner sample-banner"' in body
    # At the top: above the heading and above the trend sentence.
    at = body.index(beta.SAMPLE_FRAME)
    assert at < body.index("<h1>Visit brief</h1>")
    assert at < body.index('class="brief-alert"')


def test_a_real_brief_has_no_sample_line(cfg, svc, monkeypatch):  # noqa: F811
    c, _fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    body = _brief_page(c, agent_id, monkeypatch)
    assert beta.SAMPLE_FRAME not in body
    assert "sample-banner" not in body
    assert "made-up" not in body
