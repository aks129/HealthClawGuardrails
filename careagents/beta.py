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


#: The first sentence of what a turn answers when the day's allowance is
#: spent: at admission (the 429, the texted reply) and when the worker
#: charges it (#856 sign-off F4). daily_limit_text adds when it comes back.
DAILY_LIMIT_TEXT = "You've used today's messages."


def daily_limit_text(now: _dt.datetime | None = None) -> str:
    """DAILY_LIMIT_TEXT and how long until more, in plain hours (#864).
    The allowance is counted per UTC day (accounts.claim_daily_turn), so
    it comes back at the next UTC midnight. "Overnight" was false for US
    testers, whose reset falls in the afternoon or evening, and a UTC time
    means nothing to them, so the sentence says only how long."""
    moment = (now or _dt.datetime.now(_dt.timezone.utc)).astimezone(
        _dt.timezone.utc)
    midnight = (moment + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    minutes = (midnight - moment).total_seconds() / 60
    if minutes < 45:
        when = "in under an hour"
    else:
        hours = int((minutes + 30) // 60)
        when = f"in about {hours} hour{'' if hours == 1 else 's'}"
    return f"{DAILY_LIMIT_TEXT} More {when}."


#: The sample connection's one line: the banner on its chat page, and the
#: opening line of every answer the worker finishes on it, web and text
#: alike. A tester read a sample creatinine rise, dated today and worded
#: "your", as her own result and stopped (2026-10-08 patient walk).
SAMPLE_FRAME = ("These are made-up records, not yours. Nothing here is about "
                "you.")


def sample_frame(who: str = "") -> str:
    """The sample line for these records. `who` is hub.relation_of the
    person they stand in for ("your parent"), empty for the holder's own:
    Mom's chat said "not yours" to someone asking about Mom (FHI-44). The
    line opens every answer, so the model reads it back as history: the
    relationship only, never the nickname."""
    if not who:
        return SAMPLE_FRAME
    return (f"These are made-up records. Nothing here is about {who}, or "
            "about you.")


def under_banner(text: str, frame: str = SAMPLE_FRAME) -> str:
    """A sample answer as the web chat shows it under its sticky banner:
    without the worker's leading copy of `frame` (sample_frame), so the
    line shows once per view. Only that exact copy on its own line, and
    never down to nothing. Called only where the banner is rendered on the
    page.

    The holder's SAMPLE_FRAME goes by the same rule in every chat: answers
    stored before sample_frame named the person open with it, and it would
    show under Mom's banner (QA and security review of #952, L1)."""
    for line in dict.fromkeys((frame, SAMPLE_FRAME)):
        lead = line + "\n\n"
        rest = text[len(lead):] if text.startswith(lead) else ""
        if rest.strip():
            return rest
    return text


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
    external gateway, not by this worker, so this check does NOT cover it;
    instead CareAgents refuses to mint a code for, or bind a chat to, an
    agent on anything but the sample (app.py, `_is_real`).
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
