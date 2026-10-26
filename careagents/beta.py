"""Rules for the invited-tester stage (beta spec, stage 1).

Storage lives in accounts.py. This module holds the rules that are not
storage: the cohort cap, the two sentences a refused turn answers with, and
the weekly counts.
"""

from __future__ import annotations

import datetime as _dt

from careagents import routing

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


#: Ahead of a fixed sentence that answers instead of the model (the day's
#: limit, paused, terms, a failed turn) when what the person said reads as
#: a crisis. Without it "chest pain" at the limit got only "You've used
#: today's messages" (QA on #1017). English and Spanish: no model writes it.
EMERGENCY_LINE = ("If this is an emergency, call 911 now. "
                  "Si es una emergencia, llame al 911 ahora.")
#: Added for self-harm or wanting to die (routing.is_self_harm).
SELF_HARM_LINE = ("If you are thinking about hurting yourself, call or text "
                  "988. Si está pensando en hacerse daño, llame o envíe un "
                  "mensaje de texto al 988.")


def emergency_first(reply: str, *said: str) -> str:
    """`reply` led by EMERGENCY_LINE (and SELF_HARM_LINE) when anything in
    `said` is crisis wording; otherwise `reply` exactly. Never instead of
    the reply: the limit, pause or failure still stands."""
    if not any(routing.is_crisis(s) for s in said if s):
        return reply
    lead = EMERGENCY_LINE
    if any(routing.is_self_harm(s) for s in said if s):
        lead += "\n" + SELF_HARM_LINE
    if reply.startswith(lead):
        return reply
    # A failed turn led by this is no longer one of errors.FAILURE_TEXTS,
    # so collapse_failed_turns keeps it on the page: a wall of failures is
    # shown once, but never by hiding the emergency line.
    return f"{lead}\n\n{reply}"


#: After every refusal or failure that is not led by EMERGENCY_LINE
#: (security re-review of #1017): the word list decides only whether the
#: line comes first, never whether there is one.
EMERGENCY_TRAILER = ("If this is an emergency, call 911. "
                     "Si es una emergencia, llame al 911.")


def with_911(reply: str, said: str = "") -> str:
    """A refusal or failure `reply` that always carries a 911 line: led by
    emergency_first's lines when `said`, the person's current message
    only, reads as a crisis, else followed by EMERGENCY_TRAILER. `said` is
    empty where the message is not at hand. Never echoes `said`."""
    led = emergency_first(reply, said)
    if led != reply or reply.startswith(EMERGENCY_LINE):
        return led
    if reply.endswith(EMERGENCY_TRAILER):
        return reply
    return f"{reply}\n\n{EMERGENCY_TRAILER}"


def without_trailer(text: str) -> str:
    """`text` without with_911's trailing line, for code that recognises a
    fixed sentence by its words (errors.collapse_failed_turns)."""
    tail = "\n\n" + EMERGENCY_TRAILER
    return text[:-len(tail)] if text.endswith(tail) else text


#: The sample connection's one line: the banner on its chat page, and the
#: closing line of every answer the worker finishes on it, web and text
#: alike. A tester read a sample creatinine rise, dated today and worded
#: "your", as her own result and stopped (2026-10-08 patient walk).
SAMPLE_FRAME = ("These are made-up records, not yours. Nothing here is about "
                "you.")


def sample_frame(who: str = "") -> str:
    """The sample line for these records. `who` is hub.relation_of the
    person they stand in for ("your parent"), empty for the holder's own:
    Mom's chat said "not yours" to someone asking about Mom (FHI-44). The
    line closes every answer, so the model reads it back as history: the
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
    show under Mom's banner (QA and security review of #952, L1).

    A crisis answer carries the line at its end, never before the 911
    guidance (worker._finish); a copy between paragraphs is dropped too,
    whatever the answer's shape. That copy of this
    chat's own `frame`, a paragraph of its own, is dropped the same way
    (chat.js withoutFrame); the holder's older line only ever led."""
    for line in dict.fromkeys((frame, SAMPLE_FRAME)):
        lead = line + "\n\n"
        rest = text[len(lead):] if text.startswith(lead) else ""
        if rest.strip():
            return rest
    inline, tail = "\n\n" + frame + "\n\n", "\n\n" + frame
    if text.find(inline) > 0:
        rest = text.replace(inline, "\n\n", 1)
    elif text.endswith(tail):
        rest = text[:-len(tail)]
    else:
        rest = ""
    return rest if rest.strip() else text


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
