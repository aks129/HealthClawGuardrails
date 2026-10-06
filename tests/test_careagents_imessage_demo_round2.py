"""#875's QA and patient round: what a demo over a text still tripped on.

A bare menu number, the signed-document text, the CONNECT copy, the
texting-style wording, units in the text trend, the chart link's first
paint and the review page's tab title. Synthetic handles only.
"""

from __future__ import annotations

import json

import pytest

from careagents import imessage
from careagents.personas import system_prompt
from tests.test_careagents import cfg  # noqa: F401  (pytest fixture)
from tests.test_careagents_sendblue import (  # noqa: F401
    PHONE, _app, _hook, _pair, sb_cfg, sb_svc)

ORIGIN = "http://localhost"


def test_the_text_prompt_says_the_link_is_below():
    text = system_prompt("Juniper", "calm", surface="imessage")
    assert "a link is included below" in text
    assert "I'll send a link" not in text


def _queued_texts(hc):
    return [m["content"] for rows in hc.logged.values() for m in rows
            if m.get("role") == "user"]


# --- a bare number from the menu ---------------------------------------------

@pytest.mark.parametrize("reply,question", [
    ("1", "What medications am I on?"), ("2.", "What do my labs say?"),
    ("4)", "Has my cholesterol changed?"), (" 6 ", "Fill out my intake form"),
])
def test_a_number_from_the_menu_asks_that_question(
        sb_cfg, sb_svc, monkeypatch, reply, question):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    _hook(c, reply, handle=f"num-{reply.strip()}")
    assert _queued_texts(hc)[-1] == question


@pytest.mark.parametrize("reply", ["7", "0", "12", "1 and 2", "1.5"])
def test_other_numbers_are_asked_as_typed(
        sb_cfg, sb_svc, monkeypatch, reply):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    _hook(c, reply, handle="num-other")
    assert _queued_texts(hc)[-1] == reply


def test_a_menu_number_is_charged_like_any_turn(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    from careagents import beta
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    deps = app.extensions["careagents_imessage"]
    seen = []
    monkeypatch.setattr(deps, "admission_block",
                        lambda acct, ctx: seen.append(acct)
                        or beta.DAILY_LIMIT_TEXT)
    fake.sent.clear()
    _hook(c, "3", handle="num-cap")
    assert seen and fake.sent == [(PHONE, beta.DAILY_LIMIT_TEXT)]
    assert _queued_texts(hc) == []


def test_a_menu_number_from_a_stranger_gets_the_link(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    _hook(c, "1", handle="num-stranger")
    assert "/link?t=" in fake.sent[0][1]


def test_the_menu_says_a_number_works():
    line = "Just type your question, or reply with a number."
    assert line in imessage.HELP_TEXT and line in imessage.WELCOME_TEXT


# --- the signed document -----------------------------------------------------

def test_a_signed_document_is_texted_as_its_review_page():
    events = [{"type": "agent.card", "payload": {
        "type": "card", "kind": "pdf", "action_id": "act-7",
        "url": "https://engine.example/r6/actions/act-7/pdf?sig=abc"}}]
    reply = imessage.run_reply(events, ORIGIN, "ag_1")
    assert reply == f"Your intake form is ready: {ORIGIN}/review/ag_1/act-7"
    assert "sig=" not in reply and "engine.example" not in reply


def test_a_document_without_its_request_id_is_not_texted_as_a_url():
    events = [{"type": "agent.card", "payload": {
        "type": "card", "kind": "pdf",
        "url": "https://engine.example/x.pdf?sig=abc"}}]
    reply = imessage.run_reply(events, ORIGIN, "ag_1")
    assert "engine.example" not in reply and "ready" in reply


# --- units in the text trend -------------------------------------------------

def _series(key, unit, code=None, system=None):
    def reading(date, value):
        return {"date": date, "value": value, "unit": unit, "code": code,
                "system": system, "flag": "N"}
    return {"key": key, "name": "LDL cholesterol", "unit": unit,
            "trend_plottable": True,
            "readings": [reading("2025-01-01", 160),
                         reading("2026-01-01", 120)]}


def test_the_text_trend_uses_the_ucum_code_not_free_text():
    from careagents.agent import _timeline_in_words
    words = _timeline_in_words(_series(
        "ldl", "mg/dL for Maria Rivera", "mg/dL", "http://unitsofmeasure.org"))
    assert words["first"]["unit"] == words["latest"]["unit"] == "mg/dL"
    assert "Maria" not in json.dumps(words)


def test_without_a_ucum_code_the_unit_comes_from_the_known_map():
    from careagents.agent import _timeline_in_words
    words = _timeline_in_words(_series("ldl", "free text unit"))
    assert words["latest"]["unit"] == "mg/dL"
    words = _timeline_in_words(_series("ldl", "free text", "mg/dL Maria",
                                       "http://example.org/not-ucum"))
    assert words["latest"]["unit"] == "mg/dL"
    words = _timeline_in_words(_series("unknown-key", "free text unit"))
    assert words["latest"]["unit"] == ""
    assert "free text" not in json.dumps(words)


# --- the chart link's first paint, the review tab -----------------------------

def test_a_chart_link_hides_the_history_until_the_chart_is_ready(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    with_chart = c.get(f"/chat?agent={agent_id}&chart=cholesterol"
                       ).get_data(as_text=True)
    plain = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert "data-chart-pending" in with_chart
    assert "data-chart-pending" not in plain
    js = c.get("/static/chat.js").get_data(as_text=True)
    assert "scrollIntoView" in js and "chartPending" in js


def test_the_review_page_is_titled_careagents(sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    hc.fetch_review_page = lambda t, a: (200, (
        "<html><head><title>Review your intake form — HealthClaw Guardrails"
        f"</title></head><body>/r6/actions/{a}/review</body></html>"))
    page = c.get(f"/review/{agent_id}/act-1").get_data(as_text=True)
    assert "<title>Review your intake form — CareAgents</title>" in page
    assert "HealthClaw Guardrails" not in page
