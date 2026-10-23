"""The daily funnel and the issues count: timestamps and integers, no PHI.

Two tables, both written by us from fixed names:

- `ca_funnel_accounts`, one row per account: when each milestone first
  happened (a UTC epoch float, or NULL), and the inviter's account id when
  the account joined through a personal invite link. No message, no record,
  no provider, no action kind.
- `ca_issue_days`, one row per UTC day per issue kind: a count. No message,
  no account, no error text. A chat_error is also counted under its cause,
  one of the fixed codes in careagents/causes.py (#926).
- `ca_owner_alerts`, one row per UTC hour in which the owner was emailed
  that the model key is refused or out of credit. A time, nothing else.
- `ca_issue_detail_days`, the same count split one step further for two
  kinds: a server error by the route's endpoint name (never its URL), and
  a flagged answer by its reason code. Both come from fixed lists.

What each milestone means:

- joined: the account row was made. Accounts are made only once an address
  is proven (an email code, or a confirmed beta request), so this is also
  the first sign-in.
- confirmed: the address confirmed a beta request (the double opt-in). An
  account made with an email code alone, never through the sign-up form,
  has none. On the sign-up path joined and confirmed fall in the same
  second; the requests still waiting to be confirmed are counted from
  `ca_beta_requests`, so the drop-off before an account exists shows too.
- first_answer: the assistant finished a model answer for the account.
- connect_started: a real-record connection passed the gate and consent.
- records_connected: records landed on a real-record connection.
- imessage_linked: a phone was bound to the account.
- action_proposed: the assistant proposed an action for approval.
- action_approved: the person approved one.
- feedback_sent: the person sent the feedback form while signed in.

Every write here is wrapped by its caller, or wraps itself: a count is
never a reason a page, a sign-in or a turn fails.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from functools import wraps

import click
from flask import abort, render_template, session
from sqlalchemy import (Column, Float, Integer, String, UniqueConstraint,
                        update)
from sqlalchemy.exc import IntegrityError

from careagents import causes, mail
from careagents.models import Base, _uid, now

logger = logging.getLogger(__name__)

MILESTONES = ("joined", "confirmed", "first_answer", "connect_started",
              "records_connected", "imessage_linked", "action_proposed",
              "action_approved", "feedback_sent")

ISSUE_KINDS = ("chat_error", "run_replay_error", "fasten_failed",
               "sign_in_failed", "mail_failed", "rate_limited",
               "server_error", "answer_flagged")

#: Why a person flagged an answer ("This wasn't right", static/chat.js),
#: as the code the page sends and the words it shows (a product ruling on
#: #980). "unsafe" stays: it is the safety signal most worth counting.
FLAG_LABELS = (("wrong", "Wrong"), ("confusing", "Confusing"),
               ("not_what_asked", "Didn't do what I asked"),
               ("unsafe", "Felt unsafe or worrying"),
               ("other", "Something else"))
FLAG_REASONS = tuple(code for code, _ in FLAG_LABELS)

#: The kinds counted one step further, in `ca_issue_detail_days`.
DETAIL_KINDS = ("server_error", "answer_flagged")

#: A Flask endpoint name, as the code names it. The caller also checks it
#: is one of the app's own endpoints; this is the shape the table accepts.
_ENDPOINT = re.compile(r"\A[A-Za-z0-9_.]{1,64}\Z")

#: What each chat_error was, counted beside it (careagents/causes.py).
CHAT_ERROR_CAUSES = causes.CAUSES

#: How often the worker's digest thread looks at the clock.
DIGEST_TICK_SECONDS = 900


class FunnelAccount(Base):
    """When each milestone first happened for one account. Timestamps and
    one account id (the inviter's), nothing else. `account_id` is a plain
    pointer, not a foreign key: the row goes with the account
    (`delete_for_account`), and an inviter's id outlives the inviter."""
    __tablename__ = "ca_funnel_accounts"
    account_id = Column(String(32), primary_key=True)
    joined = Column(Float, nullable=True)
    confirmed = Column(Float, nullable=True)
    first_answer = Column(Float, nullable=True)
    connect_started = Column(Float, nullable=True)
    records_connected = Column(Float, nullable=True)
    imessage_linked = Column(Float, nullable=True)
    action_proposed = Column(Float, nullable=True)
    action_approved = Column(Float, nullable=True)
    feedback_sent = Column(Float, nullable=True)
    invited_by = Column(String(32), nullable=True)


class IssueDay(Base):
    """How many times one kind of issue happened on one UTC day."""
    __tablename__ = "ca_issue_days"
    __table_args__ = (UniqueConstraint("day", "kind",
                                       name="uq_ca_issue_days_day_kind"),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("iss"))
    day = Column(String(10), nullable=False, index=True)   # UTC "YYYY-MM-DD"
    kind = Column(String(24), nullable=False)
    count = Column(Integer, nullable=False, default=0)


class IssueDetailDay(Base):
    """One kind of issue on one UTC day, split by a detail from a fixed
    list: the endpoint name of a route that failed, or a flag's reason."""
    __tablename__ = "ca_issue_detail_days"
    __table_args__ = (UniqueConstraint(
        "day", "kind", "detail", name="uq_ca_issue_detail_days"),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("isd"))
    day = Column(String(10), nullable=False, index=True)   # UTC "YYYY-MM-DD"
    kind = Column(String(24), nullable=False)
    detail = Column(String(64), nullable=False)
    count = Column(Integer, nullable=False, default=0)


class DigestDay(Base):
    """The days whose digest has been sent: the row is taken before the
    send, so two workers cannot both send one day."""
    __tablename__ = "ca_digest_days"
    day = Column(String(10), primary_key=True)             # UTC "YYYY-MM-DD"
    sent_at = Column(Float, nullable=False)


class OwnerAlert(Base):
    """The UTC hours in which the owner was emailed about the model key:
    the row is taken before the send, so there is at most one an hour
    across every worker."""
    __tablename__ = "ca_owner_alerts"
    hour = Column(String(13), primary_key=True)            # "YYYY-MM-DDTHH"
    sent_at = Column(Float, nullable=False)


def utc_day(moment: _dt.datetime | None = None) -> str:
    moment = moment or _dt.datetime.now(_dt.timezone.utc)
    return moment.strftime("%Y-%m-%d")


def _day_of(ts: float) -> str:
    return utc_day(_dt.datetime.fromtimestamp(ts, _dt.timezone.utc))


# --- writing ------------------------------------------------------------------

def mark(session_scope, account_id: str, milestone: str,
         at: float | None = None) -> bool:
    """Stamp `milestone` for the account if it has never happened. Returns
    whether this call stamped it. Safe to call on every occurrence: only
    the first one lands, and a racing insert falls back to the update."""
    if milestone not in MILESTONES:
        raise ValueError(f"unknown milestone {milestone!r}")
    if not account_id:
        return False
    moment = at if at is not None else now()
    col = getattr(FunnelAccount, milestone)
    stmt = (update(FunnelAccount)
            .where(FunnelAccount.account_id == account_id, col.is_(None))
            .values({milestone: moment}))
    with session_scope() as s:
        if s.execute(stmt).rowcount:
            return True
        if s.get(FunnelAccount, account_id) is not None:
            return False                    # already stamped
    try:
        with session_scope() as s:
            s.add(_new_row(s, account_id, **{milestone: moment}))
        return True
    except IntegrityError:
        with session_scope() as s:
            return bool(s.execute(stmt).rowcount)


def _new_row(s, account_id: str, **fields) -> FunnelAccount:
    """A first row for the account. An account from before this table has
    its `joined` filled from when the account was made."""
    from careagents.models import Account
    if "joined" not in fields:
        acct = s.get(Account, account_id)
        if acct is not None and acct.created_at:
            fields["joined"] = acct.created_at
    return FunnelAccount(account_id=account_id, **fields)


def set_inviter(session_scope, account_id: str, inviter_id: str) -> None:
    """Record who invited the account, once."""
    if not account_id or not inviter_id:
        return
    stmt = (update(FunnelAccount)
            .where(FunnelAccount.account_id == account_id,
                   FunnelAccount.invited_by.is_(None))
            .values(invited_by=inviter_id))
    with session_scope() as s:
        if s.execute(stmt).rowcount or s.get(FunnelAccount, account_id):
            return
    try:
        with session_scope() as s:
            s.add(_new_row(s, account_id, invited_by=inviter_id))
    except IntegrityError:
        with session_scope() as s:
            s.execute(stmt)


def new_account(session_scope, account_id: str) -> None:
    """Just after an account is made: stamp joined, confirmed when its
    mailbox has a confirmed beta request, and the inviter when it joined
    through a link. Its own transaction, after the account's, so a failure
    here never undoes a sign-in."""
    from careagents import beta_signup, invites
    from careagents.models import Account
    with session_scope() as s:
        acct = s.get(Account, account_id)
        if acct is None or s.get(FunnelAccount, account_id) is not None:
            return
        t = acct.created_at or now()
        row = FunnelAccount(account_id=account_id, joined=t)
        key = beta_signup.account_key(acct.email)
        for req in s.query(beta_signup.BetaRequest).filter(
                beta_signup.BetaRequest.confirmed_at.isnot(None)):
            if beta_signup.account_key(req.email) == key:
                row.confirmed = t
                break
        row.invited_by = invites.inviter_for_email(s, acct.email)
        s.add(row)


def mark_for_agent(session_scope, agent_id: str, milestone: str) -> bool:
    """`mark` for the account that owns `agent_id` (the run worker knows
    the agent, not always the account)."""
    from careagents.models import Agent
    with session_scope() as s:
        agent = s.get(Agent, agent_id) if agent_id else None
        account_id = agent.account_id if agent is not None else None
    return mark(session_scope, account_id, milestone) if account_id else False


def mark_for_email(session_scope, email: str, milestone: str) -> bool:
    """`mark` for the account with this email, if there is one."""
    from careagents.models import Account
    with session_scope() as s:
        acct = s.query(Account).filter_by(
            email=(email or "").strip().lower()).first()
        account_id = acct.id if acct is not None else None
    return mark(session_scope, account_id, milestone) if account_id else False


def reached(session_scope, account_id: str, milestone: str) -> bool:
    """Has the account reached `milestone`?"""
    if milestone not in MILESTONES:
        raise ValueError(f"unknown milestone {milestone!r}")
    with session_scope() as s:
        row = s.get(FunnelAccount, account_id) if account_id else None
        return row is not None and getattr(row, milestone) is not None


def count_issue(session_scope, kind: str,
                moment: _dt.datetime | None = None) -> None:
    """Add one to today's count of `kind`. Increment first, insert if there
    was no row, and on a racing insert increment the row the other writer
    made (the `count_activity` pattern)."""
    if kind not in ISSUE_KINDS and kind not in CHAT_ERROR_CAUSES:
        raise ValueError(f"unknown issue kind {kind!r}")
    day = utc_day(moment)
    stmt = (update(IssueDay)
            .where(IssueDay.day == day, IssueDay.kind == kind)
            .values(count=IssueDay.count + 1))
    with session_scope() as s:
        if s.execute(stmt).rowcount:
            return
    try:
        with session_scope() as s:
            s.add(IssueDay(day=day, kind=kind, count=1))
    except IntegrityError:
        with session_scope() as s:
            s.execute(stmt)


def count_chat_error(session_scope, cause: str) -> None:
    """A failed chat turn: one chat_error, and one under its cause. Each
    count is its own, so a failure on one never loses the other."""
    if cause not in CHAT_ERROR_CAUSES:
        raise ValueError(f"unknown chat error cause {cause!r}")
    safely(count_issue, session_scope, "chat_error")
    safely(count_issue, session_scope, cause)


def alert_owner(session_scope, cfg, cause: str,
                moment: _dt.datetime | None = None) -> str | None:
    """Email CARE_OWNER_EMAILS that chat turns fail on the model key, at
    most once a UTC hour. Returns the mail outcome, or None when there is
    nobody to tell or this hour's email is already taken. The cause code is
    the whole content: no account, no message."""
    if not cfg.owner_emails:
        return None
    moment = moment or _dt.datetime.now(_dt.timezone.utc)
    hour = moment.strftime("%Y-%m-%dT%H")
    try:
        with session_scope() as s:
            if s.get(OwnerAlert, hour) is not None:
                return None
            s.add(OwnerAlert(hour=hour, sent_at=now()))
    except IntegrityError:
        return None
    line = (f"Chat turns are failing on the model setup ({cause}): the key "
            f"was refused or is out of credit, or the model host is not "
            f"approved for real records. Testers are being told the "
            f"assistant is unavailable. Check the provider account and the "
            f"worker's model settings; the counts are on /admin/funnel.")
    outcome = mail.NOT_SENT
    for to in sorted(cfg.owner_emails):
        try:
            sent = mail.send_notice(cfg, to, "Assistant unavailable", line)
        except Exception:  # noqa: BLE001 - the claim is released below
            logger.warning("owner alert send raised")
            sent = mail.NOT_SENT
        if sent != mail.NOT_SENT:
            outcome = sent
    if outcome == mail.NOT_SENT:
        # Nobody was told, so a later failure this hour may try again.
        with session_scope() as s:
            s.query(OwnerAlert).filter_by(hour=hour).delete()
        note_mail(session_scope, cfg, outcome)
    return outcome


def count_detail(session_scope, kind: str, detail: str,
                 moment: _dt.datetime | None = None) -> None:
    """Add one to today's `kind`, and to its count split by `detail`. A
    detail that is not an endpoint name (server_error) or a reason code
    (answer_flagged) is refused before anything is written."""
    if kind not in DETAIL_KINDS:
        raise ValueError(f"unknown detail kind {kind!r}")
    if kind == "answer_flagged" and detail not in FLAG_REASONS:
        raise ValueError("unknown flag reason")
    if kind == "server_error" and not _ENDPOINT.match(detail or ""):
        raise ValueError("not an endpoint name")
    count_issue(session_scope, kind, moment)
    day = utc_day(moment)
    stmt = (update(IssueDetailDay)
            .where(IssueDetailDay.day == day, IssueDetailDay.kind == kind,
                   IssueDetailDay.detail == detail)
            .values(count=IssueDetailDay.count + 1))
    with session_scope() as s:
        if s.execute(stmt).rowcount:
            return
    try:
        with session_scope() as s:
            s.add(IssueDetailDay(day=day, kind=kind, detail=detail, count=1))
    except IntegrityError:
        with session_scope() as s:
            s.execute(stmt)


def safely(fn, *args, **kwargs) -> None:
    """Call a counter and never let it fail the caller."""
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - a count never fails a request
        logger.warning("funnel count failed: %s", getattr(fn, "__name__", fn))


def note_mail(session_scope, cfg, outcome: str) -> None:
    """Count a refused send as mail_failed. Only where mail is really sent:
    with no provider key, or outside production, NOT_SENT is the design."""
    if (outcome == mail.NOT_SENT and cfg.resend_api_key
            and mail.real_mail_allowed(cfg)):
        safely(count_issue, session_scope, "mail_failed")


def delete_for_account(s, account_id: str) -> None:
    """The account's own row goes with it (`delete_account`)."""
    s.query(FunnelAccount).filter_by(account_id=account_id).delete()


# --- reading ------------------------------------------------------------------

def daily(session_scope, days: int = 7,
          moment: _dt.datetime | None = None) -> dict:
    """Counts per UTC day, newest first: how many accounts reached each
    milestone that day, how many joined through an invite, and each kind of
    issue. Plus the totals for the window, conversion between neighbouring
    steps over all time (`confirmed` left out: it is how an account joined,
    not a step after joining), and invites per inviter. Integers and account ids
    only. Reads whole small tables: the beta is tens of accounts.

    `details` is, per day, each detail kind split by its detail (endpoint
    name or reason code) with its count."""
    from careagents import beta_signup, invites
    moment = moment or _dt.datetime.now(_dt.timezone.utc)
    days = max(1, int(days))
    labels = [utc_day(moment - _dt.timedelta(days=i)) for i in range(days)]
    rows = {d: {"day": d, **{m: 0 for m in MILESTONES}, "via_invite": 0,
                **{k: 0 for k in (*ISSUE_KINDS, *CHAT_ERROR_CAUSES)}}
            for d in labels}
    reached = {m: 0 for m in MILESTONES}
    # `confirmed` is how an account joined (a beta request confirmed at or
    # before the join), never a step after it: an email-code account has
    # none, so joined -> confirmed read 0% for every one of them (patient
    # walk 2026-10-18). It keeps its row in the daily table only.
    chain = [m for m in MILESTONES if m != "confirmed"]
    both = {(a, b): 0 for a, b in zip(chain, chain[1:])}
    with session_scope() as s:
        for f in s.query(FunnelAccount).all():
            for m in MILESTONES:
                ts = getattr(f, m)
                if ts is None:
                    continue
                reached[m] += 1
                d = _day_of(ts)
                if d in rows:
                    rows[d][m] += 1
                    if m == "joined" and f.invited_by:
                        rows[d]["via_invite"] += 1
            for a, b in both:
                if getattr(f, a) is not None and getattr(f, b) is not None:
                    both[(a, b)] += 1
        for i in s.query(IssueDay).filter(IssueDay.day >= labels[-1]):
            if i.day in rows and (i.kind in ISSUE_KINDS
                                  or i.kind in CHAT_ERROR_CAUSES):
                rows[i.day][i.kind] += i.count or 0
        details = {d: {k: {} for k in DETAIL_KINDS} for d in labels}
        for i in s.query(IssueDetailDay).filter(
                IssueDetailDay.day >= labels[-1]):
            if i.day in details and i.kind in DETAIL_KINDS:
                split = details[i.day][i.kind]
                split[i.detail] = split.get(i.detail, 0) + (i.count or 0)
        waiting = (s.query(beta_signup.BetaRequest)
                   .filter_by(status="pending").count())
        per_inviter = invites.per_inviter(s)
    window = {k: sum(r[k] for r in rows.values())
              for k in (*MILESTONES, "via_invite", *ISSUE_KINDS,
                        *CHAT_ERROR_CAUSES)}
    # Of the accounts that reached one step, how many also reached the
    # next. The steps are not strictly ordered (a phone can be linked on
    # made-up records), so "both" is counted, never "the later one".
    conversion = []
    for a, b in both:
        rate = (round(100 * both[(a, b)] / reached[a])
                if reached[a] else None)
        conversion.append({"from": a, "to": b, "from_n": reached[a],
                           "to_n": both[(a, b)], "percent": rate})
    return {"days": [rows[d] for d in labels], "window": window,
            "reached": reached, "conversion": conversion,
            "requests_waiting": waiting, "per_inviter": per_inviter,
            "details": details}


def digest_text(report: dict, day: str) -> str:
    """The digest body: the named day, the last 7 days, and the issues.
    Counts only."""
    today = next((r for r in report["days"] if r["day"] == day),
                 report["days"][0])
    lines = [f"CareAgents funnel for {day} (UTC)", ""]
    for m in MILESTONES:
        lines.append(f"{m}: {today[m]}  (7 days: {report['window'][m]})")
    lines.append(f"joined via invite: {today['via_invite']}  "
                 f"(7 days: {report['window']['via_invite']})")
    lines.append(f"beta requests waiting to be confirmed: "
                 f"{report['requests_waiting']}")
    lines += ["", "Issues"]
    for k in ISSUE_KINDS:
        lines.append(f"{k}: {today[k]}  (7 days: {report['window'][k]})")
    lines += ["", "chat_error by cause"]
    for k in CHAT_ERROR_CAUSES:
        lines.append(f"  {k}: {today[k]}  (7 days: {report['window'][k]})")
    split = report.get("details", {}).get(day, {})
    errors = split.get("server_error", {})
    lines += ["", "Server errors by route"]
    lines += ([f"{route}: {n}" for route, n in sorted(
        errors.items(), key=lambda kv: (-kv[1], kv[0]))] or ["none"])
    flags = split.get("answer_flagged", {})
    lines += ["", "Flagged answers by reason"]
    lines += [f"{label}: {flags.get(code, 0)}"
              for code, label in FLAG_LABELS]
    lines += ["", "Details: /admin/funnel, or flask funnel daily"]
    return "\n".join(lines)


def send_digest_if_due(session_scope, cfg,
                       moment: _dt.datetime | None = None) -> str | None:
    """Send yesterday's digest (the last whole UTC day) once. Returns the
    mail outcome, or None when off or already sent. The day is claimed
    before the send, so two workers cannot both send it; a send the
    provider refused releases the claim so a later tick tries again."""
    if not cfg.digest_to:
        return None
    moment = moment or _dt.datetime.now(_dt.timezone.utc)
    day = utc_day(moment - _dt.timedelta(days=1))
    try:
        with session_scope() as s:
            if s.get(DigestDay, day) is not None:
                return None
            s.add(DigestDay(day=day, sent_at=now()))
    except IntegrityError:
        return None
    report = daily(session_scope, days=8, moment=moment)
    # The window is the 7 days ending with `day`, not with today.
    report["days"] = [r for r in report["days"] if r["day"] <= day][:7]
    report["window"] = {k: sum(r[k] for r in report["days"])
                        for k in report["window"]}
    text = digest_text(report, day)
    # Sign-ins for the day, counts only: never an address (signins.py).
    from careagents import signins
    text += "\n" + "\n".join(signins.digest_lines(
        signins.day_counts(session_scope, day)))
    html = "<pre style='font-family:monospace'>" + text.replace(
        "&", "&amp;").replace("<", "&lt;") + "</pre>"
    try:
        outcome = mail.send_message(cfg, cfg.digest_to,
                                    f"Funnel for {day}", html, text)
    except Exception:  # noqa: BLE001 - the claim is released below
        logger.warning("funnel digest send raised")
        outcome = mail.NOT_SENT
    if outcome == mail.NOT_SENT:
        with session_scope() as s:
            s.query(DigestDay).filter_by(day=day).delete()
        note_mail(session_scope, cfg, outcome)
    return outcome


def run_digest(session_scope, cfg, stop) -> None:
    """The worker's digest thread: look every DIGEST_TICK_SECONDS, send
    when a new UTC day has begun. Never raises."""
    while not stop.is_set():
        try:
            send_digest_if_due(session_scope, cfg)
        except Exception:  # noqa: BLE001 - the thread keeps going
            logger.warning("funnel digest tick failed", exc_info=True)
        stop.wait(DIGEST_TICK_SECONDS)


# --- surfaces -----------------------------------------------------------------

def is_owner(cfg, email: str | None) -> bool:
    return bool(email) and email.strip().lower() in cfg.owner_emails


def register(app, svc, cfg, full_session=lambda: True) -> None:
    """The owner page /admin/funnel and the `funnel` command group.
    `full_session()` says the session came from a code or passkey sign-in,
    not a beta confirm link (careagents/app.py, full_sign_in_required)."""
    # Imported here: signins builds on this module.
    from careagents import signins

    def owner_only(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            # 404, not 403 or a redirect to sign in: nobody else learns the
            # page exists. Unset CARE_OWNER_EMAILS closes it for everyone.
            aid = session.get("account_id")
            acct = svc.get_account(aid) if aid else None
            if (acct is None or not is_owner(cfg, acct.email)
                    or not full_session()):
                abort(404)
            return fn(*a, **k)
        return wrapper

    @app.get("/admin/funnel")
    @owner_only
    def admin_funnel():
        report = daily(svc.session, days=7)
        testers = signins.testers(svc.session,
                                  owner_emails=cfg.owner_emails)
        resp = app.make_response(render_template(
            "admin_funnel.html", report=report, milestones=MILESTONES,
            issue_kinds=ISSUE_KINDS, chat_error_causes=CHAT_ERROR_CAUSES,
            today=report["days"][0],
            testers=testers, testers_cap=signins.TESTERS_CAP,
            morning_loop=signins.loop_counts(testers),
            needs_you=signins.needs_you(
                testers, signins.pending_requests(svc.session),
                can_connect=lambda e: cfg.real_records_open_for(
                    e, invited=svc.real_records_invited))))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/admin/funnel/testers.csv")
    @owner_only
    def admin_testers_csv():
        """The testers table as a file. It holds addresses: owner only,
        never cached."""
        resp = app.make_response(signins.testers_csv(
            signins.testers(svc.session)))
        resp.mimetype = "text/csv"
        resp.headers["Content-Disposition"] = (
            "attachment; filename=careagents-testers.csv")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    signins.register_cli(app, svc)

    @app.cli.group("funnel")
    def funnel_group():
        """The daily funnel and issues count (counts only)."""

    @funnel_group.command("daily")
    @click.option("--days", default=7, show_default=True,
                  help="How many UTC days back to print.")
    def funnel_daily(days):
        """One row per UTC day: accounts reaching each step, and issues."""
        report = daily(svc.session, days=days)
        short = {"joined": "join", "confirmed": "conf",
                 "first_answer": "ans", "connect_started": "c_start",
                 "records_connected": "c_done", "imessage_linked": "imsg",
                 "action_proposed": "a_prop", "action_approved": "a_ok",
                 "feedback_sent": "fdbk"}
        cols = [*MILESTONES, "via_invite"]
        heads = [short.get(c, "invite") for c in cols]
        click.echo("day         " + " ".join(f"{h:>7}" for h in heads))
        for r in report["days"]:
            click.echo(f"{r['day']}  " + " ".join(
                f"{r[c]:>7}" for c in cols))
        click.echo("")
        click.echo("issues      " + " ".join(f"{k}" for k in ISSUE_KINDS))
        for r in report["days"]:
            click.echo(f"{r['day']}  " + " ".join(
                f"{r[k]:>{len(k)}}" for k in ISSUE_KINDS))
        click.echo("")
        click.echo("conversion (all time)")
        for c in report["conversion"]:
            pct = "-" if c["percent"] is None else f"{c['percent']}%"
            click.echo(f"  {c['from']} -> {c['to']}: "
                       f"{c['to_n']}/{c['from_n']} ({pct})")
        click.echo(f"beta requests waiting to be confirmed: "
                   f"{report['requests_waiting']}")
        if report["per_inviter"]:
            click.echo("invites per inviter (account id: joined)")
            for row in report["per_inviter"]:
                click.echo(f"  {row['inviter_id']}: {row['joined']}")

