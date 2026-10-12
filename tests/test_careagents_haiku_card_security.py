"""Security review of #936 (Haiku how-to card): the pass path escalates,
the card carries nothing about the person, and the new templates admit no
crisis or clinical content. Real SDK over httpx.MockTransport; synthetic."""
from __future__ import annotations

import json
import types

import pytest

from careagents import llm, routing

CANARY = "ZZCANARY-936"


def _cfg(**over):
    base = dict(provider="anthropic", anthropic_api_key="k",
                anthropic_model="claude-sonnet-5-5", anthropic_oauth_token="",
                anthropic_oauth_beta="oauth-2025-04-20",
                run_deadline_seconds=120, anthropic_max_tokens=8000,
                anthropic_effort="medium", routing=True, advisor=True,
                database_url="")
    base.update(over)
    return types.SimpleNamespace(**base)


def _sdk(monkeypatch, haiku_text):
    import anthropic
    import httpx
    log: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        log.append(body)
        text = haiku_text if body["model"] == routing.HAIKU_MODEL else "S"
        return httpx.Response(200, json={
            "id": "m", "type": "message", "role": "assistant",
            "model": body["model"], "stop_reason": "end_turn",
            "stop_sequence": None, "content": [{"type": "text",
                                                "text": text}],
            "usage": {"input_tokens": 1, "output_tokens": 1}})

    real = anthropic.Anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: real(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kw))
    return log


HISTORY = [{"role": "user", "content": "How is my A1c?"},
           {"role": "assistant", "content": f"Your A1c was {CANARY}."}]


@pytest.mark.parametrize("reply", [
    routing.HAIKU_PASS,
    "Sorry! " + routing.HAIKU_PASS.replace("'", "’"),
    routing.HAIKU_PASS.upper(),
])
def test_a_pass_reply_is_discarded_and_rerun_on_sonnet(monkeypatch, reply):
    log = _sdk(monkeypatch, reply)
    turn = llm.complete(_cfg(), "SYS", HISTORY + [
        {"role": "user", "content": "how do you keep my data safe"}], [])
    assert [b["model"] for b in log] == [routing.HAIKU_MODEL,
                                         routing.SONNET_MODEL]
    assert turn.text == "S" and turn.escalated
    assert routing.HAIKU_CARD not in log[1]["system"]


def test_the_haiku_request_carries_the_card_and_nothing_about_the_person(
        monkeypatch):
    log = _sdk(monkeypatch, "You're welcome.")
    llm.complete(_cfg(), "SYS", HISTORY + [
        {"role": "user", "content": "how can i invite my family"}], [])
    [b] = log
    assert b["model"] == routing.HAIKU_MODEL
    assert b["system"].startswith(routing.HAIKU_CARD)
    assert CANARY not in json.dumps(b)
    assert [m["role"] for m in b["messages"]] == ["user"]


def test_the_card_holds_no_identifier_link_or_secret():
    card = routing.HAIKU_CARD
    assert "http" not in card and "@" not in card
    assert not any(ch.isdigit() and ch != "5" for ch in card)


@pytest.mark.parametrize("text", [
    "how do i delete a passkey i want to die",
    "how can i invite someone to hurt me",
    "how do you keep me safe from my husband",
    "how do you keep me safe tonight",
    "how do i invite friends overdose",
    "how do i remove a passkey \U0001F48A",
    "how do i invite famiIy",
    "how do u keep me safe",
    "how do you keep me safe when i feel like ending it",
    "how does this keep my data safe from my abuser",
])
def test_the_new_templates_admit_nothing_extra(text):
    assert routing.classify(text) == routing.TIER_SONNET, text
