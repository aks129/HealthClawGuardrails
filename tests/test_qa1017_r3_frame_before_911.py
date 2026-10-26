"""QA on #1017 round 3: the sample frame must never come before 911.

worker._finish puts the frame after the answer's FIRST paragraph on a
crisis message. That is after the 911 guidance only when the 911 guidance
is the first paragraph. The default persona ("calm") is told to
"acknowledge feelings before facts", so an answer that opens with one line
of sympathy and gives 911 in its second paragraph is a natural shape. Then
the stored answer, which iMessage and SMS send as is, reads "Nothing here
is about you." before "call 911": the #1015 live failure, one paragraph
later. Shown on a running stack at a6fee38f with a scripted reply.

Red on a6fee38f by design: the reproduction for Dev. Synthetic text only.
"""

from __future__ import annotations

import pytest

from careagents import beta
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)
from tests.test_careagents_crisis_fixed_replies import (
    _answers, _queue, _work)

ANSWERS = (
    ("Lo siento mucho, eso suena muy serio.\n\n"
     "Llame al 911 ahora mismo.\n\n"
     "No espere: el dolor de pecho con falta de aire puede ser grave."),
    ("I'm so sorry you're going through this.\n\n"
     "Call 911 now, or 988 if you are thinking of hurting yourself."),
)


@pytest.mark.parametrize("answer", ANSWERS, ids=["es", "en"])
def test_the_frame_never_precedes_the_911_paragraph(
        cfg, svc, monkeypatch, answer):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply=answer)
    _queue(c, agent_id, "me duele mucho el pecho", "qa-r3-frame")
    _work(cfg, svc, fake)
    [said] = _answers(fake)
    assert said.index("911") < said.index(beta.SAMPLE_FRAME), said
