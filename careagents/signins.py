"""Sign-ins counted by method, and the owner's list of testers.

One table, `ca_sign_in_days`: one row per account per UTC day per method,
a count. No address, no agent string, no device, no time of day. The
account row keeps the latest method next to `last_login_at`.

The methods are a fixed set, written at the points that stamp
`last_login_at` (careagents/accounts.py):

- signup_confirm: the beta confirm link (`first_run_account`), the first
  sign-in of a tester who asked on /beta. A personal invite link arrives
  this way too: the link opens /beta and the sign-in is the confirm, so it
  is not a method of its own. How an account joined is on the testers list,
  read from the funnel row.
- email_code: an emailed code (`verify_email_code`).
- passkey: a passkey (`finish_authentication`). The consent screen's fresh
  passkey check is not a sign-in and is not counted.

The testers list is for the owner only (/admin/funnel, its CSV, and
`flask testers list`). Owner decision 2026-10-16: tester emails may appear
there. They never appear in the digest, which gets `day_counts` (integers),
and nothing in this module logs one.

A count is never a reason a sign-in fails: callers wrap `count` in
`funnel.safely`.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io

import click
from sqlalchemy import (Column, Integer, String, UniqueConstraint, func,
                        update)
from sqlalchemy.exc import IntegrityError

from careagents import funnel
from careagents.models import Account, ActivityDay, Base, UsageDay, _uid

METHODS = ("signup_confirm", "email_code", "passkey")

#: How the list names each method, for people.
METHOD_LABELS = {"signup_confirm": "confirm link", "email_code": "email code",
                 "passkey": "passkey"}

#: The most rows the list shows: the beta is tens of testers.
TESTERS_CAP = 200

#: Days with no chat turn or approval before a tester counts as away.
AWAY_DAYS = 3

#: The steps the "stuck at" column walks, in funnel order. feedback_sent is
#: left out: sending feedback is not a step further along.
_STEPS = tuple(m for m in funnel.MILESTONES if m != "feedback_sent")


class SignInDay(Base):
    """How many times one account signed in by one method on one UTC day.
    `account_id` is a plain pointer, not a foreign key, as on the funnel
    row: the rows go with the account (`delete_for_account`), and a count
    never blocks removing an account row."""
    __tablename__ = "ca_sign_in_days"
    __table_args__ = (UniqueConstraint(
        "account_id", "day", "method", name="uq_ca_sign_in_days"),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("sin"))
    account_id = Column(String(32), nullable=False, index=True)
    day = Column(String(10), nullable=False, index=True)   # UTC "YYYY-MM-DD"
    method = Column(String(16), nullable=False)
    count = Column(Integer, nullable=False, default=0)


# --- writing ------------------------------------------------------------------

def count(session_scope, account_id: str, method: str,
          moment: _dt.datetime | None = None) -> None:
    """Add one to today's count for the account and method. Increment
    first, insert if there was no row, and on a racing insert increment the
    row the other writer made (the `funnel.count_issue` pattern)."""
    if method not in METHODS:
        raise ValueError(f"unknown sign-in method {method!r}")
    if not account_id:
        return
    day = funnel.utc_day(moment)
    stmt = (update(SignInDay)
            .where(SignInDay.account_id == account_id, SignInDay.day == day,
                   SignInDay.method == method)
            .values(count=SignInDay.count + 1))
    with session_scope() as s:
        if s.execute(stmt).rowcount:
            return
    try:
        with session_scope() as s:
            s.add(SignInDay(account_id=account_id, day=day, method=method,
                            count=1))
    except IntegrityError:
        with session_scope() as s:
            s.execute(stmt)


def delete_for_account(s, account_id: str) -> None:
    """The account's rows go with it (`delete_account`)."""
    s.query(SignInDay).filter_by(account_id=account_id).delete()


# --- reading: counts (the digest) ------------------------------------------

def day_counts(session_scope, day: str) -> dict:
    """Sign-ins on one UTC day by method, and how many distinct accounts
    signed in. Integers only: this is what the digest gets."""
    by_method = {m: 0 for m in METHODS}
    accounts = set()
    with session_scope() as s:
        for r in s.query(SignInDay).filter_by(day=day):
            if r.method in by_method:
                by_method[r.method] += r.count or 0
                accounts.add(r.account_id)
    return {"by_method": by_method, "accounts": len(accounts)}


def digest_lines(counts: dict) -> list[str]:
    lines = ["", "Sign-ins"]
    for m in METHODS:
        lines.append(f"{m}: {counts['by_method'][m]}")
    lines.append(f"accounts signed in: {counts['accounts']}")
    return lines


# --- reading: the testers list (owner only) ----------------------------------

def _fmt(ts: float | None) -> str:
    if not ts:
        return ""
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M")


def testers(session_scope, moment: _dt.datetime | None = None,
            limit: int = TESTERS_CAP) -> list[dict]:
    """One row per account, newest first, at most `limit`. Owner only: the
    rows carry the email.

    `joined` is also the first sign-in: an account is made only at a
    proven sign-in (a code or a confirm link), so there is no separate
    column for it. Last active is the latest UTC day
    with a chat turn (`ca_usage_days`, any assistant, sample included) or
    an approval (`ca_activity_days`)."""
    from careagents import beta_signup, invites
    moment = moment or _dt.datetime.now(_dt.timezone.utc)
    today = moment.date()
    week_start = funnel.utc_day(moment - _dt.timedelta(days=6))
    limit = max(1, int(limit))
    with session_scope() as s:
        accts = (s.query(Account)
                 .order_by(Account.created_at.desc(), Account.id)
                 .limit(limit).all())
        ids = [a.id for a in accts]
        if not ids:
            return []
        frows = {f.account_id: f for f in s.query(funnel.FunnelAccount)
                 .filter(funnel.FunnelAccount.account_id.in_(ids))}
        week = dict(s.query(SignInDay.account_id, func.sum(SignInDay.count))
                    .filter(SignInDay.account_id.in_(ids),
                            SignInDay.day >= week_start)
                    .group_by(SignInDay.account_id).all())
        turned = dict(s.query(UsageDay.account_id, func.max(UsageDay.day))
                      .filter(UsageDay.account_id.in_(ids),
                              UsageDay.turns > 0)
                      .group_by(UsageDay.account_id).all())
        acted = dict(s.query(ActivityDay.account_id,
                             func.max(ActivityDay.day))
                     .filter(ActivityDay.account_id.in_(ids),
                             (ActivityDay.asked > 0)
                             | (ActivityDay.approved > 0))
                     .group_by(ActivityDay.account_id).all())
        confirmed = {beta_signup.account_key(r.email) for r in s.query(
            beta_signup.BetaRequest).filter(
                beta_signup.BetaRequest.confirmed_at.isnot(None))}
        names = {}
        for f in frows.values():
            if f.invited_by and f.invited_by not in names:
                names[f.invited_by] = invites.inviter_first_name(
                    s, f.invited_by)
        out = []
        for a in accts:
            f = frows.get(a.id)
            reached = [m for m in funnel.MILESTONES
                       if f is not None and getattr(f, m) is not None]
            furthest = next((m for m in reversed(_STEPS) if m in reached),
                            None)
            if f is not None and f.invited_by:
                via, inviter = "invite", names.get(f.invited_by)
                label = (f"Invite from {inviter}" if inviter
                         else "Invite link")
            elif (f is not None and f.confirmed) or (
                    beta_signup.account_key(a.email) in confirmed):
                via, inviter, label = "beta", None, "/beta request"
            else:
                via, inviter, label = "get_started", None, "Get started"
            last_turn = turned.get(a.id)
            last_active = max((d for d in (last_turn, acted.get(a.id)) if d),
                              default=None)
            asked = last_turn is not None or (
                f is not None and f.first_answer is not None)
            if last_active:
                idle = (today - _dt.date.fromisoformat(last_active)).days
            elif a.created_at:
                idle = (today - _dt.datetime.fromtimestamp(
                    a.created_at, _dt.timezone.utc).date()).days
            else:
                idle = None
            if not asked:
                reason = "never asked"
            elif idle is not None and idle >= AWAY_DAYS:
                reason = f"{idle} days away"
            else:
                reason = ""
            out.append({
                "account_id": a.id, "email": a.email,
                "joined": _fmt(a.created_at), "joined_via": via,
                "inviter": inviter, "joined_label": label,
                "last_sign_in": _fmt(a.last_login_at),
                "last_method": a.last_login_method or "",
                "last_method_label": METHOD_LABELS.get(
                    a.last_login_method or "", ""),
                "sign_ins_7d": int(week.get(a.id) or 0),
                "last_active": last_active, "milestones": reached,
                "furthest": furthest, "idle_days": idle,
                "stuck": bool(reason), "stuck_reason": reason})
    return out


#: The CSV's columns, in order.
CSV_FIELDS = ("email", "joined", "joined_via", "inviter_first_name",
              "last_sign_in", "last_method", "sign_ins_7d",
              "last_active", "milestones", "furthest", "idle_days", "stuck")


def _cell(value) -> str:
    """A spreadsheet must not run a cell as a formula: a first name is
    typed by a tester."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def testers_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_FIELDS)
    for r in rows:
        w.writerow([_cell(v) for v in (
            r["email"], r["joined"], r["joined_via"], r["inviter"],
            r["last_sign_in"], r["last_method"],
            r["sign_ins_7d"], r["last_active"], ";".join(r["milestones"]),
            r["furthest"], r["idle_days"], "yes" if r["stuck"] else "")])
    return buf.getvalue()


# --- the owner's terminal -----------------------------------------------------

def register_cli(app, svc) -> None:
    """`flask testers list`: the owner page's table, in the terminal."""

    @app.cli.group("testers")
    def testers_group():
        """Beta testers: sign-ins and where each one is stuck (owner only:
        prints email addresses)."""

    @testers_group.command("list")
    @click.option("--limit", default=TESTERS_CAP, show_default=True,
                  help="How many accounts, newest first.")
    def testers_list(limit):
        """One row per account, newest first."""
        rows = testers(svc.session, limit=limit)
        if not rows:
            click.echo("No accounts yet.")
            return
        head = (f"{'email':<32} {'joined':<16} {'how':<22} "
                f"{'last sign-in':<16} {'method':<14} {'7d':>3} "
                f"{'active':<10} {'furthest':<17} {'idle':>4}  stuck")
        click.echo(head)
        for r in rows:
            idle = "-" if r["idle_days"] is None else r["idle_days"]
            click.echo(
                f"{r['email'][:32]:<32} {r['joined']:<16} "
                f"{r['joined_label'][:22]:<22} {r['last_sign_in']:<16} "
                f"{r['last_method']:<14} {r['sign_ins_7d']:>3} "
                f"{r['last_active'] or '-':<10} "
                f"{r['furthest'] or '-':<17} {idle:>4}  "
                f"{r['stuck_reason']}")
