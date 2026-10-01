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
