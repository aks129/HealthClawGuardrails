"""QA review of #931 (model routing) at 2ecdf65.

Two rows pin classifier guards that mutation testing showed no test held
(the digit rule and the stated crisis rule). One strict-xfail row pins a
live finding: a small-talk reply straight after a crisis message goes to
Haiku, which sees only that reply. It XPASSes (goes red) once fixed.
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


@pytest.mark.xfail(strict=True, reason=(
    "QA #931: a farewell after a crisis turn goes to Haiku, which sees only "
    "the farewell. Live 3/3: Haiku said goodbye with no 911/988; routing "
    "off, Sonnet re-checked safety 3/3."))
@pytest.mark.parametrize("follow", ["ok. bye", "thanks", "good night"])
def test_a_reply_after_a_crisis_message_stays_on_sonnet(follow):
    messages = _CRISIS_TURN + [{"role": "user", "content": follow}]
    assert routing.tier_for(messages) == routing.TIER_SONNET
