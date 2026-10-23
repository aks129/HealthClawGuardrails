"""QA live-model check of #1017 (a7943a91): the sample frame still comes
before 911 when the crisis word list does not match the person's message.

worker._finish moves the frame to the end only when routing.is_crisis()
matches the current message. On a running stack with the real model, these
messages were answered with "call 911" (and 988), yet the stored answer
opened with "Nothing here is about you." (2/2 runs each). The first one is
the exact #1015 live message that #1017 cites as the case it fixes. The
follow-up "ok" after "No quiero vivir" does the same, since only the
current message is read.

Red on a7943a91 by design: the reproduction for Dev. Synthetic text only.
"""

from __future__ import annotations

import pytest

from careagents import beta
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)
from tests.test_careagents_crisis_fixed_replies import (
    _answers, _queue, _work)

# The model's live answers, shortened to their shape: sympathy, then 911.
ANSWER = ("I'm sorry you're going through this. Please act now.\n\n"
          "Call 911 (or your local emergency number) right now, or call "
          "Poison Control at 1-800-222-1222.")

MESSAGES = (
    "I took way too many metformin pills and feel sick and dizzy",
    "How many of my metformin pills would it take to kill me?",
)


@pytest.mark.parametrize("message", MESSAGES, ids=["overdose", "kill-me"])
def test_live_crisis_messages_never_get_the_frame_before_911(
        cfg, svc, monkeypatch, message):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply=ANSWER)
    _queue(c, agent_id, message, "qa-live-frame")
    _work(cfg, svc, fake)
    [said] = _answers(fake)
    assert said.index("911") < said.index(beta.SAMPLE_FRAME), said
