"""Rules for the invited-tester stage (beta spec, stage 1).

Storage lives in accounts.py. This module holds the rules that are not
storage: the cohort cap, the two sentences a refused turn answers with, and
the weekly counts.
"""

from __future__ import annotations

#: Stage 1 is "up to 25" invited testers (spec section 2). Counted over
#: active invites. The environment allowlist is not counted: it is the
#: operator's own short list and predates the table.
STAGE1_INVITE_CAP = 25

#: What a paused account's assistant answers (spec section 4.6). Fixed
#: text: no record was read to produce it.
PAUSED_TEXT = ("Your records are paused, so I can't answer right now. If "
               "you didn't expect this, write to contactus@healthclaw.io.")


def turn_block(connection: dict, paused: bool,
               consent_version: str) -> str | None:
    """The sentence a turn answers instead of reaching a model, or None.

    Checked in the run worker, the only caller of llm.complete, so it holds
    for web, iMessage and Telegram, and for runs queued before the change.
    """
    if paused:
        return PAUSED_TEXT
    return None
