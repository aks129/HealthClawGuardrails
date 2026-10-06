"""Every CareAgents feature, over a text.

The lab chart, the visit brief, approvals, the intake form and long answers
each have a text-shaped form: words and a link back to the web app, never a
card the phone cannot show. Driven through the Sendblue webhook and the
worker's deliverer, with the Sendblue client faked.

Synthetic handles only (555 numbers).
"""

from __future__ import annotations

import json

import pytest

from careagents import imessage, sendblue_surface
from careagents.agent import TOOLS, _execute_tool
from careagents.personas import system_prompt
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _interpret_payload, cfg)
from tests.test_careagents_sendblue import (  # noqa: F401
    PHONE, _app, _deliverer, _hook, _pair, _run, sb_cfg, sb_svc)

ORIGIN = "http://localhost"


class _Call:
    def __init__(self, i, name, arguments=None):
        self.id, self.name, self.arguments = f"c{i}", name, arguments or {}


class _Turn:
    def __init__(self, text, calls=()):
        self.text, self.tool_calls, self.raw_tool_calls = text, list(calls), []


def _model(monkeypatch, calls=(), answer="Done.", seen=None):
    """A model that calls `calls` once, then answers `answer`."""
    def complete(_cfg, system, history, tools):
        if seen is not None:
            seen.append({"system": system, "history": list(history)})
        if calls and not any(m.get("role") == "tool" for m in history):
            return _Turn("", [_Call(i, n, a) for i, (n, a) in
                              enumerate(calls)])
        return _Turn(answer)
    monkeypatch.setattr("careagents.worker.llm.complete", complete)


def _ask(app, c, fake, text, handle):
    _hook(c, text, handle=handle)
    _run(app)
    _deliverer(app, fake).once()


# --- 1. the lab chart -------------------------------------------------------

class _LabsHC:
    def interpret_labs(self, _tenant):
        return _interpret_payload()


def test_on_text_the_timeline_tool_says_no_chart_can_be_shown():
    events: list = []
    out = json.loads(_execute_tool(_LabsHC(), "t", "show_lab_timeline",
                                   {"topic": "cholesterol"}, events,
                                   surface="imessage"))
    assert events == [{"type": "card", "kind": "lab-timeline",
                       "topic": "cholesterol"}]
    assert out["chart_shown"] is False
    assert "chart is now visible" not in out["note"]
    assert "cannot be shown" in out["note"] and "link" in out["note"]
    [series] = out["series"]
    # Words need something to say: the first and latest reading and the
    # direction, worked out here rather than by the model.
    assert series["first"] == {"date": "2025-01-01", "value": 210,
                               "unit": "mg/dL"}
    assert series["latest"]["value"] == 244
    assert series["direction"] == "higher"


def test_a_single_reading_on_text_has_no_direction():
    events: list = []
    out = json.loads(_execute_tool(_LabsHC(), "t", "show_lab_timeline",
                                   {"topic": "a1c"}, events,
                                   surface="imessage"))
    [series] = out["series"]
    assert series["trend_plottable"] is False
    assert "direction" not in series and "first" not in series


def test_on_the_web_the_timeline_tool_is_unchanged():
    events: list = []
    out = json.loads(_execute_tool(_LabsHC(), "t", "show_lab_timeline",
                                   {"topic": "cholesterol"}, events))
    assert out["chart_shown"] is True
    assert "chart is now visible" in out["note"]
    assert "244" not in json.dumps(out)


def test_a_timeline_card_becomes_a_link_to_the_chart():
    events = [{"type": "agent.text", "payload": {"text": "It went up."}},
              {"type": "agent.card", "payload": {
                  "type": "card", "kind": "lab-timeline",
                  "topic": "LDL cholesterol"}}]
    reply = imessage.run_reply(events, ORIGIN, "ag_1")
    assert reply.startswith("It went up.")
    assert f"{ORIGIN}/chat?agent=ag_1&chart=LDL%20cholesterol" in reply


def test_the_chart_link_opens_the_chart_in_the_web_chat(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    assert c.get(f"/chat?agent={agent_id}&chart=cholesterol"
                 ).status_code == 200
    js = c.get("/static/chat.js").get_data(as_text=True)
    assert 'get("chart")' in js and "addLabTimelineCard(" in js


def test_a_trend_question_by_text_gets_words_and_a_link(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    hc.interpret_labs = lambda t: _interpret_payload()
    _pair(c, agent_id)
    seen: list = []
    _model(monkeypatch, [("show_lab_timeline", {"topic": "cholesterol"})],
           "Your cholesterol went from 210 to 244.", seen)
    fake.sent.clear()
    _ask(app, c, fake, "has my cholesterol changed?", "trend-1")
    [(_, text)] = fake.sent
    assert text.startswith("Your cholesterol went from 210 to 244.")
    assert f"/chat?agent={agent_id}&chart=cholesterol" in text
    tool_result = next(m["content"] for m in seen[-1]["history"]
                       if m.get("role") == "tool")
    assert "chart is now visible" not in tool_result


# --- 2. texting style ---------------------------------------------------------

def test_the_text_prompt_asks_for_short_plain_text_and_links():
    text = system_prompt("Juniper", "calm", surface="imessage")
    for phrase in ("plain text", "no markdown", "600",
                   "a link is included below"):
        assert phrase in text, phrase
    assert "review card will appear" not in text


def test_the_web_prompt_is_unchanged():
    web = system_prompt("Juniper", "calm")
    assert web == system_prompt("Juniper", "calm", surface="web")
    assert "review card will appear" in web
    assert "plain text" not in web


def test_the_worker_hands_the_text_prompt_to_a_texted_turn(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    seen: list = []
    _model(monkeypatch, answer="Hi.", seen=seen)
    _ask(app, c, fake, "hello", "style-1")
    assert "a link is included below" in seen[0]["system"]


def test_the_intake_form_on_text_promises_a_link_not_a_card():
    class _HC:
        def start_form_action(self, _t):
            return "act-9"
    events: list = []
    out = json.loads(_execute_tool(_HC(), "t", "start_intake_form", {},
                                   events, agent_id="ag_1",
                                   surface="imessage"))
    assert "card" not in out["note"].lower() and "link" in out["note"]
    web = json.loads(_execute_tool(_HC(), "t", "start_intake_form", {},
                                   [], agent_id="ag_1"))
    assert "card is now visible" in web["note"]


@pytest.mark.parametrize("raw,want", [
    ("**Good news:** your A1c is *steady*.",
     "Good news: your A1c is steady."),
    ("# Summary\n## Labs\nAll fine.", "Summary\nLabs\nAll fine."),
    ("* one\n* two", "- one\n- two"),
    ("Open http://localhost/home#connect-section now.",
     "Open http://localhost/home#connect-section now."),
    ("Agent ag_care_123 and 2*3 stay.", "Agent ag_care_123 and 2*3 stay."),
])
def test_markdown_is_stripped_from_a_texted_answer(raw, want):
    events = [{"type": "agent.text", "payload": {"text": raw}}]
    assert imessage.run_reply(events, ORIGIN, "ag_1") == want


# --- 3. HELP and the welcome are a menu ---------------------------------------

MENU = ("What medications am I on?", "What do my labs say?",
        "Any screenings due?", "Has my cholesterol changed?",
        "Get me ready for my visit", "Fill out my intake form")


@pytest.mark.parametrize("text", [imessage.HELP_TEXT, imessage.WELCOME_TEXT])
def test_help_and_the_welcome_list_what_to_ask(text):
    for i, item in enumerate(MENU, start=1):
        assert f"{i}. {item}" in text, item
    assert "STOP" in text


def test_help_keeps_the_contact_and_start_lines():
    assert imessage.CONTACT in imessage.HELP_TEXT
    assert "START" in imessage.HELP_TEXT


# --- 4. the visit brief ------------------------------------------------------

_PREFIX = "https://healthclaw.io/fhir/StructureDefinition/brief-section-"


def _brief_resource():
    def section(name, *fields):
        return {"url": _PREFIX + name, "extension": [
            {"url": "field", "valueString": json.dumps(f)} for f in fields]}
    return {"resourceType": "Basic", "extension": [
        section("problems", {"label": "Hypertension", "value": "active",
                             "sourceType": "Condition", "sourceId": "c1"}),
        section("medications", {"label": "Lisinopril", "value": "10 mg",
                                "sourceType": "MedicationRequest",
                                "sourceId": "m1"}),
    ]}


def test_the_brief_tool_is_offered():
    assert "appointment_brief" in {t["name"] for t in TOOLS}


def test_the_brief_tool_summarizes_and_emits_a_brief_card():
    class _HC:
        def fetch_appointment_brief(self, _t):
            return _brief_resource()
    events: list = []
    out = json.loads(_execute_tool(_HC(), "t", "appointment_brief", {},
                                   events, surface="imessage"))
    assert events == [{"type": "card", "kind": "brief"}]
    assert out["sections"]["problems"] == [
        {"label": "Hypertension", "value": "active"}]
    assert "c1" not in json.dumps(out)          # source ids stay out
    assert "link" in out["note"]


def test_no_brief_is_not_reported_as_no_visit():
    class _HC:
        def fetch_appointment_brief(self, _t):
            return None
    events: list = []
    out = json.loads(_execute_tool(_HC(), "t", "appointment_brief", {},
                                   events, surface="imessage"))
    assert events == []
    assert out["brief"] is None and "not" in out["note"]


def test_a_brief_card_becomes_a_link_to_the_brief():
    events = [{"type": "agent.text", "payload": {"text": "Here's a short "
                                                 "version."}},
              {"type": "agent.card", "payload": {"type": "card",
                                                 "kind": "brief"}}]
    reply = imessage.run_reply(events, ORIGIN, "ag_1")
    assert f"{ORIGIN}/brief?agent=ag_1" in reply


# --- 5. approvals ------------------------------------------------------------

def test_approvals_texts_the_count_and_the_link(sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    hc.pending_actions = lambda t: [{"id": "a"}, {"id": "b"}]
    fake.sent.clear()
    _hook(c, "Approvals", handle="ap-1")
    [(_, text)] = fake.sent
    assert "2 requests" in text
    assert f"{ORIGIN}/agents/{agent_id}/approvals" in text
    assert fake.typing == []                    # no run


def test_one_approval_is_singular(sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    fake.sent.clear()
    _hook(c, "APPROVALS", handle="ap-2")
    assert "1 request " in fake.sent[0][1]


def test_approvals_that_cannot_be_checked_never_read_as_zero(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    from careagents.healthclaw import HealthClawError
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)

    def _down(_t):
        raise HealthClawError("pending actions failed (503)", 503)
    hc.pending_actions = _down
    fake.sent.clear()
    _hook(c, "approvals", handle="ap-3")
    [(_, text)] = fake.sent
    assert text == imessage.APPROVALS_UNCHECKABLE_TEXT
    assert "0" not in text and "no requests" not in text.lower()


def test_approvals_from_a_stranger_gets_the_sign_in_link(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    _hook(c, "approvals", handle="ap-4")
    assert "/link?t=" in fake.sent[0][1]


def test_a_form_proposed_by_text_comes_back_with_its_review_link(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    _model(monkeypatch, [("start_intake_form", {})],
           "I've started your form. I'll send a link.")
    fake.sent.clear()
    _ask(app, c, fake, "fill out my intake form", "form-1")
    [(_, text)] = fake.sent
    assert f"{ORIGIN}/review/{agent_id}/act-1" in text


# --- 6. long answers ---------------------------------------------------------

def test_a_short_answer_is_one_text():
    assert imessage.split_reply("Hi there.", ORIGIN) == ["Hi there."]


def test_paragraphs_are_packed_into_texts_in_order():
    paras = [f"P{i} " + "x" * 590 for i in range(3)]
    parts = imessage.split_reply("\n\n".join(paras), ORIGIN)
    assert len(parts) == 3
    assert all(len(p) <= imessage.TEXT_PART_LIMIT for p in parts)
    assert [p[:2] for p in parts] == ["P0", "P1", "P2"]


def test_small_paragraphs_share_a_text():
    parts = imessage.split_reply("a\n\nb\n\nc", ORIGIN)
    assert parts == ["a\n\nb\n\nc"]


def test_an_oversized_paragraph_breaks_between_words():
    text = " ".join(["word"] * 600)               # ~3000 characters
    parts = imessage.split_reply(text, ORIGIN)
    assert all(len(p) <= imessage.TEXT_PART_LIMIT for p in parts)
    assert all(not p.startswith(" ") and not p.endswith(" ") for p in parts)
    assert " ".join(parts) == text


def test_past_four_texts_the_rest_is_in_the_chat():
    paras = [f"P{i} " + "y" * 900 for i in range(10)]
    parts = imessage.split_reply("\n\n".join(paras), ORIGIN, "ag_1")
    assert len(parts) == imessage.MAX_TEXT_PARTS == 4
    assert parts[-1].endswith(
        f"The rest is in your chat: {ORIGIN}/chat?agent=ag_1")
    assert all(len(p) <= imessage.TEXT_PART_LIMIT for p in parts)


def test_the_deliverer_sends_the_parts_in_order(sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    answer = "\n\n".join(f"Part {i}. " + "z" * 700 for i in range(3))
    _model(monkeypatch, answer=answer)
    fake.sent.clear()
    _ask(app, c, fake, "tell me everything", "long-1")
    assert [t[:6] for _, t in fake.sent] == ["Part 0", "Part 1", "Part 2"]
    _deliverer(app, fake).once()
    assert len(fake.sent) == 3


# --- 7. a photo with words ----------------------------------------------------

def test_a_photo_with_a_caption_is_noticed_and_the_caption_answered(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    _model(monkeypatch, answer="Your labs look steady.")
    fake.sent.clear()
    _hook(c, "what do my labs say?", handle="photo-1",
          media_url="https://example.com/p.jpg")
    assert fake.sent == [(PHONE, sendblue_surface.PHOTO_TEXT)]
    assert fake.typing == [PHONE]
    _run(app)
    _deliverer(app, fake).once()
    assert fake.sent[-1] == (PHONE, "Your labs look steady.")


def test_a_photo_alone_gets_only_the_notice(sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    _hook(c, "", handle="photo-2", media_url="https://example.com/p.jpg")
    assert fake.sent == [(PHONE, sendblue_surface.PHOTO_TEXT)]
    assert sendblue_surface.PHOTO_TEXT == (
        "I can only read text for now, so I didn't see the photo.")


def test_a_stranger_photo_with_words_gets_the_notice_then_the_link(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    _hook(c, "hi", handle="photo-3", media_url="https://example.com/p.jpg")
    assert fake.sent[0] == (PHONE, sendblue_surface.PHOTO_TEXT)
    assert "/link?t=" in fake.sent[1][1]


# --- 8. CONNECT ---------------------------------------------------------------

def test_connect_points_to_add_records_on_the_web(sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    fake.sent.clear()
    _hook(c, "Connect", handle="conn-1")
    assert fake.sent == [(PHONE, imessage.connect_text(ORIGIN))]
    assert imessage.connect_text("https://careagents.cloud") == (
        "Connecting your own records is open to invited testers for now. "
        "You can see where it will be at https://careagents.cloud/home under "
        "Add records. Texting works with sample records.")
    assert fake.typing == []


def test_connect_from_a_stranger_gets_the_sign_in_link(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    _hook(c, "connect", handle="conn-2")
    assert "/link?t=" in fake.sent[0][1]


def test_a_photo_from_a_number_that_said_stop_gets_nothing(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    _hook(c, "STOP", handle="photo-4")
    fake.sent.clear()
    _hook(c, "", handle="photo-5", media_url="https://example.com/p.jpg")
    _hook(c, "hi", handle="photo-6", media_url="https://example.com/p.jpg")
    assert fake.sent == []


def test_stop_with_a_photo_is_one_confirmation(sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, *_ = _app(sb_cfg, sb_svc, monkeypatch)
    _hook(c, "STOP", handle="photo-7", media_url="https://example.com/p.jpg")
    assert fake.sent == [(PHONE, imessage.STOP_TEXT)]
