"""Model routing: the easiest turns on Haiku, the rest on Sonnet + advisor.

The classifier is the guardrail here. A clinical question answered by the
small model at low effort is the failure that matters, so Haiku is an
allowlist (greetings, thanks, what the app can do, how to use it) and every
other message — including anything ambiguous — goes to Sonnet. Haiku that
reaches for a clinical tool is overruled inside the same call.
"""
from __future__ import annotations

import types

import pytest

from careagents import llm, routing

# --- the classifier ----------------------------------------------------------

HAIKU = [
    "hi",
    "Hello!",
    "hey there",
    "good morning",
    "thanks",
    "Thank you so much!",
    "ok thanks, bye",
    "What can you do?",
    "who are you",
    "how does this work?",
    "How do I connect my records?",
    "How do I connect my real records?",
    "how do I add a passkey",
    "Can I text you from my phone?",
    "how do I delete my account",
    "I have some feedback about the app",
    "how do I sign in on my laptop",
    "Can I use iMessage with this?",
]

SONNET = [
    "",
    "yes",
    "tell me more",
    "what do you think?",
    "thanks, is my A1c ok?",
    "hi, my chest hurts",
    "what records do you have",
    "what can you tell me about my labs",
    "How is my cholesterol?",
    "I take metformin 500 mg",
    "my blood pressure was 150/95",
    "Is 7.2% bad?",
    "what does my diagnosis mean",
    "should I stop my medication",
    "thank you doctor, what about my dose",
    "I feel dizzy",
    "how do I connect my records and check my potassium",
    "hola, ¿cómo están mis resultados?",
    "help me get ready for my appointment",
    "can you fill in my intake form",
    "what screenings am I due for",
    "any allergies on file?",
    "hello " * 60,
    # agent.run_turn's last call when the tool budget is spent arrives as a
    # plain user message, so it is classified on its own words.
    "(system: tool budget reached — answer now with what you have)",
    # Live failures of the earlier word-list classifier (#931 F1): Haiku
    # answered both, with no tool call to escalate on.
    "can I text you at night when I feel like ending it all",
    "quick phone question, can hiv spread from kissing",
    # An app question with anything else in it is not an app question.
    "how do I connect my records and is my sugar ok",
    "who are you, I feel awful",
    "how do I delete my account, I want to end it all",
    "can I text you from my phone about something private",
    "hi, quick question",
]

CRISIS = [
    "I want to end it all", "thinking about suicide", "I might kill myself",
    "I took an overdose", "I can't breathe", "chest pain since an hour",
    "is this a stroke", "call 911?", "I'm having an emergency",
    "i wanna die", "I keep hurting myself",
]


@pytest.mark.parametrize("text", CRISIS)
def test_crisis_language_is_never_haiku(text):
    """MUTATION: drop the _CRISIS check -> still Sonnet (the allowlist
    refuses these too); drop both -> red. The rule is stated on its own so
    a future template cannot admit one."""
    assert routing.is_crisis(text), text
    assert routing.classify(text) == routing.TIER_SONNET, text
    # Wrapped in small talk and an app question, still never Haiku.
    assert routing.classify(f"hi, {text}") == routing.TIER_SONNET
    assert routing.classify(f"who are you {text}") == routing.TIER_SONNET


@pytest.mark.parametrize("text", HAIKU)
def test_non_clinical_messages_go_to_haiku(text):
    assert routing.classify(text) == routing.TIER_HAIKU, text


@pytest.mark.parametrize("text", SONNET)
def test_clinical_or_ambiguous_messages_go_to_sonnet(text):
    """MUTATION: drop the clinical scan, the digit rule or the length cap
    from routing.classify -> one of these goes to Haiku -> red."""
    assert routing.classify(text) == routing.TIER_SONNET, text


def _hist(*msgs):
    return [dict(m) for m in msgs]


def test_the_tier_stays_sonnet_once_the_turn_reached_a_clinical_tool():
    """The loop calls the model several times per turn. A greeting that
    escalated must not drop back to Haiku for the next round."""
    history = _hist(
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "1", "name": "get_labs", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"})
    assert routing.tier_for(history) == routing.TIER_SONNET


def test_a_new_turn_is_classified_on_its_own_message():
    history = _hist(
        {"role": "user", "content": "How is my A1c?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "1", "name": "get_labs", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"},
        {"role": "assistant", "content": "It rose."},
        {"role": "user", "content": "thanks!"})
    assert routing.tier_for(history) == routing.TIER_HAIKU


def test_the_form_status_tool_keeps_a_turn_on_haiku():
    history = _hist(
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "1", "name": "check_form_status",
                         "arguments": {"action_id": "a"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"})
    assert routing.tier_for(history) == routing.TIER_HAIKU


def test_every_tool_but_the_form_check_is_clinical():
    from careagents.agent import TOOLS
    names = {t["name"] for t in TOOLS}
    assert names - routing.CLINICAL_TOOLS == {"check_form_status"}


# --- the provider call --------------------------------------------------------

class _Text:
    def __init__(self, t):
        self.type, self.text = "text", t


class _ToolUse:
    def __init__(self, name, id_="tu_1"):
        self.type, self.name, self.id, self.input = "tool_use", name, id_, {}


class _Raw:
    """A server block the SDK does not model: advisor blocks arrive so."""
    def __init__(self, type_, content=None):
        self.type, self.content = type_, content


def _resp(content, stop_reason="end_turn", iterations=None):
    usage = types.SimpleNamespace(input_tokens=10, output_tokens=5,
                                  iterations=iterations)
    return types.SimpleNamespace(content=content, stop_reason=stop_reason,
                                 usage=usage)


def _fake(monkeypatch, plain=(), beta=()):
    """Scripted replies for client.messages and client.beta.messages."""
    calls: list[tuple[str, dict]] = []
    plain, beta = list(plain), list(beta)

    class _M:
        def __init__(self, kind, queue):
            self.kind, self.queue = kind, queue

        def create(self, **kw):
            calls.append((self.kind, kw))
            if not self.queue:
                raise AssertionError(f"unexpected {self.kind} call")
            return self.queue.pop(0)

    class _Client:
        def __init__(self, **_kw):
            self.messages = _M("plain", plain)
            self.beta = types.SimpleNamespace(messages=_M("beta", beta))

    monkeypatch.setitem(__import__("sys").modules, "anthropic",
                        types.SimpleNamespace(Anthropic=_Client,
                                              APIError=Exception))
    return calls


def _cfg(**over):
    base = dict(provider="anthropic", anthropic_api_key="k",
                anthropic_model="claude-sonnet-5-5", anthropic_oauth_token="",
                run_deadline_seconds=120, anthropic_max_tokens=8000,
                anthropic_effort="medium", routing=True, advisor=True,
                database_url="")
    base.update(over)
    return types.SimpleNamespace(**base)


TOOLS = [{"name": "get_labs", "description": "d",
          "parameters": {"type": "object", "properties": {}}},
         {"name": "check_form_status", "description": "d",
          "parameters": {"type": "object", "properties": {}}}]


def test_routing_off_is_the_single_model_call_unchanged(monkeypatch):
    """MUTATION: route when routing is off -> beta/advisor appear -> red."""
    calls = _fake(monkeypatch, plain=[_resp([_Text("hello")])])
    llm.complete(_cfg(routing=False), "sys",
                 [{"role": "user", "content": "hi"}], TOOLS)
    [(kind, kw)] = calls
    assert kind == "plain"
    assert kw["model"] == "claude-sonnet-5-5"
    # main's request since #930, field for field.
    assert set(kw) == {"model", "max_tokens", "output_config", "system",
                       "messages", "tools"}
    assert kw["max_tokens"] == 8000
    assert kw["output_config"] == {"effort": "medium"}
    assert all("type" not in t for t in kw["tools"])


def test_the_budget_nudge_does_not_restart_the_turn():
    """agent.run_turn's tool-budget note is a user message the loop wrote;
    the turn, its tier and its advisor count still start at the person's."""
    history = _hist(
        {"role": "user", "content": "How is my A1c?"},
        {"role": "assistant", "content": "", "advisor_calls": 1,
         "tool_calls": [{"id": "1", "name": "get_labs", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"},
        {"role": "user", "content": "(system: tool budget reached)",
         "loop_note": True})
    assert routing.turn_start(history) == 0
    assert routing.advisor_used(history) == 1
    assert routing.tier_for(history) == routing.TIER_SONNET


def test_haiku_sees_only_the_current_turn(monkeypatch):
    calls = _fake(monkeypatch, plain=[_resp([_Text("You're welcome!")])])
    llm.complete(_cfg(), "sys", [
        {"role": "user", "content": "How is my A1c?"},
        {"role": "assistant", "content": "It was 8.1%."},
        {"role": "user", "content": "thanks!"}], TOOLS)
    [(_kind, kw)] = calls
    assert kw["model"] == routing.HAIKU_MODEL
    assert kw["messages"] == [{"role": "user", "content": "thanks!"}]


def test_a_greeting_runs_on_haiku_at_low_effort_without_advisor(monkeypatch):
    calls = _fake(monkeypatch, plain=[_resp([_Text("Hi!")])])
    turn = llm.complete(_cfg(), "sys", [{"role": "user", "content": "hi"}],
                        TOOLS)
    [(kind, kw)] = calls
    assert kind == "plain"
    assert kw["model"] == routing.HAIKU_MODEL
    assert kw["output_config"] == {"effort": "low"}
    # No fallbacks on Haiku: there is no server-side fallback for it.
    assert "extra_body" not in kw and "betas" not in kw
    assert all(t.get("type") != routing.ADVISOR_TOOL["type"]
               for t in kw["tools"])
    assert turn.text == "Hi!"


def test_a_clinical_question_runs_on_sonnet_with_advisor_and_fallback(
        monkeypatch):
    calls = _fake(monkeypatch, beta=[_resp([_Text("Your A1c rose.")])])
    llm.complete(_cfg(), "sys",
                 [{"role": "user", "content": "How is my A1c?"}], TOOLS)
    [(kind, kw)] = calls
    assert kind == "beta"
    assert kw["model"] == routing.SONNET_MODEL
    assert kw["output_config"] == {"effort": "medium"}
    assert set(kw["betas"]) == {"advisor-tool-2026-03-01",
                                "server-side-fallback-2026-07-01"}
    assert kw["extra_body"] == {"fallbacks": "default"}
    assert kw["tools"][-1] == routing.ADVISOR_TOOL
    assert kw["tools"][-1]["model"] == "claude-opus-5-5"
    # Nudged from the prompt, never forced: forced tool_choice 400s here.
    assert "tool_choice" not in kw
    assert routing.ADVISOR_NOTE in kw["system"]


def test_the_advisor_stays_offered_on_the_no_tools_final_call(monkeypatch):
    calls = _fake(monkeypatch, beta=[_resp([_Text("Answer.")])])
    llm.complete(_cfg(), "sys",
                 [{"role": "user", "content": "How is my A1c?"}], [])
    [(_kind, kw)] = calls
    assert kw["tools"] == [routing.ADVISOR_TOOL]


def test_advisor_off_keeps_sonnet_and_the_fallback(monkeypatch):
    calls = _fake(monkeypatch, beta=[_resp([_Text("Answer.")])])
    llm.complete(_cfg(advisor=False), "sys",
                 [{"role": "user", "content": "How is my A1c?"}], TOOLS)
    [(_kind, kw)] = calls
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert all(t.get("type") != routing.ADVISOR_TOOL["type"]
               for t in kw["tools"])
    assert routing.ADVISOR_NOTE not in kw["system"]


def test_haiku_reaching_for_a_clinical_tool_is_rerun_on_sonnet(monkeypatch):
    """MUTATION: return Haiku's turn as-is -> get_labs runs on Haiku -> red."""
    calls = _fake(monkeypatch,
                  plain=[_resp([_ToolUse("get_labs", "h1")], "tool_use")],
                  beta=[_resp([_ToolUse("get_labs", "s1")], "tool_use")])
    turn = llm.complete(_cfg(), "sys", [{"role": "user", "content": "hi"}],
                        TOOLS)
    assert [k for k, _ in calls] == ["plain", "beta"]
    # Haiku's call is discarded: only Sonnet's id reaches the history.
    assert [c.id for c in turn.tool_calls] == ["s1"]
    assert turn.tier == routing.TIER_SONNET
    assert turn.escalated is True


def test_every_api_call_gets_its_own_log_line(monkeypatch, caplog):
    """QA #931 Q3: the discarded Haiku call is logged too, so the cost of an
    escalation can be read from the logs. MUTATION: drop the Haiku
    _log_call -> one line -> red."""
    import logging
    _fake(monkeypatch,
          plain=[_resp([_ToolUse("get_labs", "h1")], "tool_use")],
          beta=[_resp([_Text("Your A1c rose.")])])
    with caplog.at_level(logging.INFO, logger="careagents.llm"):
        llm.complete(_cfg(), "sys", [{"role": "user", "content": "hi"}],
                     TOOLS)
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("model call")]
    assert len(lines) == 2
    assert "kind=haiku" in lines[0] and "outcome=discarded" in lines[0]
    assert "kind=advised" in lines[1] and "outcome=ok" in lines[1]
    assert all("hi" not in line.split("kind=")[0] for line in lines)


def test_haiku_may_check_a_form_without_escalating(monkeypatch):
    calls = _fake(monkeypatch, plain=[
        _resp([_ToolUse("check_form_status", "h1")], "tool_use")])
    turn = llm.complete(_cfg(), "sys", [{"role": "user", "content": "hi"}],
                        TOOLS)
    assert [k for k, _ in calls] == ["plain"]
    assert turn.tool_calls[0].name == "check_form_status"


def test_the_advisor_is_never_read_and_its_preamble_never_shown(monkeypatch):
    """Opus 5.5 advice is encrypted; the executor's 'I'll consult the advisor
    first' line fused onto the answer in the live probe (2026-10-12)."""
    advice = {"type": "advisor_redacted_result", "encrypted_content": "x",
              "stop_reason": "end_turn"}
    content = [_Text("I'll consult the advisor first."),
               _Raw("server_tool_use"),
               _Raw("advisor_tool_result", advice),
               _Text("Your A1c rose a little.")]
    _fake(monkeypatch, beta=[_resp(content)])
    turn = llm.complete(_cfg(), "sys",
                        [{"role": "user", "content": "How is my A1c?"}], TOOLS)
    assert turn.text == "Your A1c rose a little."
    assert turn.advisor_calls == 1


def test_an_advisor_error_is_not_a_failed_turn(monkeypatch):
    err = {"type": "advisor_tool_result_error", "error_code": "overloaded"}
    _fake(monkeypatch, beta=[_resp([_Raw("server_tool_use"),
                                    _Raw("advisor_tool_result", err),
                                    _Text("Your A1c rose.")])])
    turn = llm.complete(_cfg(), "sys",
                        [{"role": "user", "content": "How is my A1c?"}], TOOLS)
    assert turn.text == "Your A1c rose."
    assert turn.advisor_calls == 0
    assert turn.advisor_errors == 1


def test_a_refusal_on_sonnet_still_says_the_refusal_text(monkeypatch):
    _fake(monkeypatch, beta=[_resp([], "refusal")])
    turn = llm.complete(_cfg(), "sys",
                        [{"role": "user", "content": "How is my A1c?"}], TOOLS)
    assert turn.text == llm.REFUSAL_TEXT


def test_tool_history_reaches_sonnet_without_advisor_blocks(monkeypatch):
    """The loop's history is rebuilt provider-neutrally: only text,
    tool_use and tool_result go back (accepted live 2026-10-12 with the
    advisor still offered)."""
    calls = _fake(monkeypatch, beta=[_resp([_Text("Done.")])])
    history = [
        {"role": "user", "content": "How is my A1c?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "1", "name": "get_labs", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"}]
    llm.complete(_cfg(), "sys", history, TOOLS)
    [(_kind, kw)] = calls
    kinds = {b["type"] for m in kw["messages"] if isinstance(m["content"], list)
             for b in m["content"]}
    assert kinds == {"tool_use", "tool_result"}


# --- flags ----------------------------------------------------------------------

def test_routing_is_off_by_default_and_the_advisor_on_with_it():
    from careagents.config import Config
    base = {"OPENAI_API_KEY": "k"}
    assert Config(env=base).routing is False
    assert Config(env={**base, "CARE_ROUTING": "on"}).routing is True
    assert Config(env={**base, "CARE_ROUTING": "on"}).advisor is True
    assert Config(env={**base, "CARE_ROUTING": "on",
                       "CARE_ADVISOR": "off"}).advisor is False
    assert Config(env={**base, "CARE_ROUTING": "yes"}).routing is False


# --- counts ---------------------------------------------------------------------

def test_daily_counts_are_integers_by_metric_only(tmp_path):
    from careagents.models import make_engine, make_session_factory
    from contextlib import contextmanager
    engine = make_engine(f"sqlite:///{tmp_path}/c.db")
    factory = make_session_factory(engine)

    @contextmanager
    def scope():
        s = factory()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    routing.record(scope, {"turns_haiku": 1, "advisor_consults": 2})
    routing.record(scope, {"turns_haiku": 1, "refusals": 0})
    got = {m: n for _day, m, n in routing.counts(scope)}
    assert got == {"turns_haiku": 2, "advisor_consults": 2}
    with pytest.raises(ValueError):
        routing.record(scope, {"How is my A1c?": 1})
