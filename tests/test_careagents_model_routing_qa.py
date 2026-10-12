"""QA review of #931 (model routing) at 2ecdf65.

Two rows pin classifier guards that mutation testing showed no test held
(the digit rule and the stated crisis rule). A strict-xfail row pinned a
live finding: a small-talk reply straight after a crisis message went to
Haiku, which saw only that reply. Fixed in routing.tier_for; the marker is
off.
Synthetic text only, no network.
"""
from __future__ import annotations

import pytest

from careagents import routing


@pytest.mark.parametrize("text", [
    "thanks 45", "hi 300", "ok 911", "bye 2", "good night 0",
])
def test_a_number_never_reaches_haiku(text):
    # Removing the digit rule let "thanks 45" through: findall drops the
    # digits and the rest is small talk.
    assert routing.classify(text) == routing.TIER_SONNET


@pytest.mark.parametrize("phrase", [
    "suicide", "faint", "overdose", "bleeding", "seizure", "stroke",
    "crisis", "ambulance",
])
def test_the_crisis_rule_holds_even_if_the_allowlist_admits_the_word(
        monkeypatch, phrase):
    # The module says _CRISIS is "the rule stated out loud, so a later
    # template cannot quietly admit one". Admit the word and check it.
    monkeypatch.setattr(routing, "_SMALL_TALK",
                        routing._SMALL_TALK | {phrase})
    assert routing.classify(f"thanks {phrase}") == routing.TIER_SONNET


_CRISIS_TURN = [
    {"role": "user",
     "content": "thanks. honestly I don't want to be here anymore"},
    {"role": "assistant",
     "content": "I'm glad you told me. Please call 911 or call or text 988. "
                "Are you safe right now?"},
]


@pytest.mark.parametrize("follow", ["ok. bye", "thanks", "good night"])
def test_a_reply_after_a_crisis_message_stays_on_sonnet(follow):
    # Was a strict xfail: live 3/3, Haiku said goodbye with no 911/988;
    # with routing off, Sonnet re-checked safety 3/3. Fixed in tier_for.
    messages = _CRISIS_TURN + [{"role": "user", "content": follow}]
    assert routing.tier_for(messages) == routing.TIER_SONNET


def test_an_answer_that_gave_crisis_lines_keeps_the_conversation_on_sonnet():
    """The person's words may not match; the answer that gave 911/988 does."""
    messages = [
        {"role": "user", "content": "everything feels pointless lately"},
        {"role": "assistant",
         "content": "If you might act on that, call or text 988 now."},
        {"role": "user", "content": "thanks"}]
    assert routing.tier_for(messages) == routing.TIER_SONNET


def test_small_talk_without_a_crisis_in_the_window_still_goes_to_haiku():
    messages = [
        {"role": "user", "content": "How is my A1c?"},
        {"role": "assistant", "content": "The sample A1c is 8.1%."},
        {"role": "user", "content": "thanks"}]
    assert routing.tier_for(messages) == routing.TIER_HAIKU


def test_record_content_in_a_tool_result_does_not_pin_sonnet():
    messages = [
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": "check_form_status", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "1",
         "content": '{"note": "emergency contact on file"}'}]
    assert routing.tier_for(messages) == routing.TIER_HAIKU
