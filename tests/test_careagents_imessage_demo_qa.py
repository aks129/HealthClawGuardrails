"""QA #875: a long answer that fails partway stops there.

The PR says "the deliverer sends the texts in order and stops at the first
failed one", because a later part without the one before it reads as a
different answer. Nothing pinned the stop: deleting the `return` after a
failed part left the suite green. Synthetic numbers only.
"""

from __future__ import annotations

from careagents import sendblue
from careagents.models import SendblueMessage
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    cfg, svc)
from tests.test_careagents_imessage_demo import _ask, _model
from tests.test_careagents_sendblue import (  # noqa: F401
    FakeSendblue, _app, _pair, sb_cfg, sb_svc)


class FailsSecondPart(FakeSendblue):
    def send_message(self, number, content):
        self.sent.append((number, content))
        ok = len(self.sent) != 2
        return sendblue.SendResult(ok=ok, status=202 if ok else 500)


def test_a_failed_part_stops_the_rest(sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    _pair(c, agent_id)
    answer = "\n\n".join(f"Part {i}. " + "z" * 700 for i in range(3))
    _model(monkeypatch, answer=answer)
    failing = FailsSecondPart()
    _ask(app, c, failing, "tell me everything", "long-fail-1")
    assert [t[:6] for _, t in failing.sent] == ["Part 0", "Part 1"]
    with sb_svc.session() as s:
        row = s.query(SendblueMessage).filter(
            SendblueMessage.run_id.isnot(None)).one()
        assert row.outcome == "failed" and row.delivered_at is not None
