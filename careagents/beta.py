"""Rules for the invited-tester stage (beta spec, stage 1).

Storage lives in accounts.py. This module holds the rules that are not
storage: the cohort cap, the two sentences a refused turn answers with, and
the weekly counts.
"""

from __future__ import annotations

import datetime as _dt

#: Stage 1 is "up to 25" invited testers (spec section 2). Counted over
#: active invites. The environment allowlist is not counted: it is the
#: operator's own short list and predates the table.
STAGE1_INVITE_CAP = 25

#: What a paused account's assistant answers (spec section 4.6). Fixed
#: text: no record was read to produce it.
PAUSED_TEXT = ("Your records are paused, so I can't answer right now. If "
               "you didn't expect this, write to contactus@healthclaw.io.")

#: The same state where no assistant is speaking: a refresh or an upload.
PAUSED_RECORDS_TEXT = ("Your records are paused, so new records can't be "
                       "added right now. If you didn't expect this, write to "
                       "contactus@healthclaw.io.")

#: The hub's line while paused, and the answer to an approval.
PAUSED_HUB_TEXT = ("Your records are paused for now. Your assistant won't "
                   "answer, and nothing new can be added or approved. If you "
                   "didn't expect this, write to contactus@healthclaw.io.")


#: What a real-record assistant answers while its connection's consent is
#: older than the current terms (spec section 4.3).
#: True before and after #565 is approved: a connection made before the
#: consent column existed holds NULL and is asked on the first deploy,
#: when no terms have changed yet.
TERMS_TEXT = ("Before we go on, please review and accept the current terms "
              "on your hub, then ask me again.")


def turn_block(connection: dict, paused: bool,
               consent_version: str) -> str | None:
    """The sentence a turn answers instead of reaching a model, or None.

    Checked in the run worker, the only caller of llm.complete here, so it
    holds for every turn CareAgents runs: web chat and the iMessage relay,
    including runs queued before the change. Telegram is answered by an
    external gateway, not by this worker, so it is NOT covered.
    Anything that is not the sample needs consent at the current version,
    so an older or unknown kind fails closed.
    """
    if paused:
        return PAUSED_TEXT
    if (connection.get("kind") != "sample"
            and connection.get("consent_version") != consent_version):
        return TERMS_TEXT
    return None


def _iso_week(moment: _dt.datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def weekly_counts(session_scope, weeks: int = 4,
                  now: _dt.datetime | None = None) -> list[dict]:
    """The spec's weekly number (section 4.5), newest week first.

    Integers only, one row per ISO week (UTC), nothing about any person.
    Reads whole small tables: stage 1 is at most 25 testers.

    - signed_up: accounts created that week.
    - real_connected: distinct accounts with a non-sample connection
      consented that week.
    - asked: distinct accounts with a real-record turn that week.
    - approved: distinct accounts with a real-record approval that week.
      An approval that ends unconfirmed is not counted (an undercount).
    """
    from careagents.models import Account, ActivityDay, Connection
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    labels = [_iso_week(moment - _dt.timedelta(weeks=i))
              for i in range(weeks)]
    rows = {w: {"week": w, "signed_up": set(), "real_connected": set(),
                "asked": set(), "approved": set()} for w in labels}

    def week_of_ts(ts):
        return _iso_week(_dt.datetime.fromtimestamp(ts, _dt.timezone.utc))

    with session_scope() as s:
        for a in s.query(Account.id, Account.created_at):
            if a.created_at and week_of_ts(a.created_at) in rows:
                rows[week_of_ts(a.created_at)]["signed_up"].add(a.id)
        for c in s.query(Connection.account_id, Connection.kind,
                         Connection.consented_at):
            if (c.kind != "sample" and c.consented_at
                    and week_of_ts(c.consented_at) in rows):
                rows[week_of_ts(c.consented_at)]["real_connected"].add(
                    c.account_id)
        for d in s.query(ActivityDay.account_id, ActivityDay.day,
                         ActivityDay.asked, ActivityDay.approved):
            w = _iso_week(_dt.datetime.strptime(d.day, "%Y-%m-%d"))
            if w not in rows:
                continue
            if d.asked:
                rows[w]["asked"].add(d.account_id)
            if d.approved:
                rows[w]["approved"].add(d.account_id)
    return [{k: (v if k == "week" else len(v)) for k, v in rows[w].items()}
            for w in labels]
