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
            limit: int = TESTERS_CAP,
            owner_emails: frozenset = frozenset()) -> list[dict]:
    """One row per account, newest first, at most `limit`. Owner only: the
    rows carry the email. A row whose email is in `owner_emails`
    (CARE_OWNER_EMAILS) is marked `is_owner`: the owner's own account is
    not a tester, and the Morning block leaves it out of every count.

    `ref` is the `?ref=` the /beta request came with, else the inviter's
    first name, else empty. `returned`: active on a later UTC day than the
    one the account joined on.

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
        # The owner's mailbox in every spelling (a +tag, dots on gmail),
        # so the "you" mark and the Morning counts agree. The page gate
        # (`funnel.is_owner`) stays an exact match.
        owner_keys = {beta_signup.account_key(e) for e in owner_emails}
        confirmed, refs = set(), {}
        for r in s.query(beta_signup.BetaRequest):
            key = beta_signup.account_key(r.email)
            if r.confirmed_at is not None:
                confirmed.add(key)
            # Only a confirmed request names a tester's ref: anyone can
            # POST /beta with any address, so an unconfirmed one (a tag
            # variant of someone's mailbox included) never sets or
            # replaces it (security review of #1009).
            if r.ref and r.confirmed_at is not None:
                refs[key] = r.ref
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
            joined_day = (_dt.datetime.fromtimestamp(
                a.created_at, _dt.timezone.utc).date()
                if a.created_at else None)
            if last_active:
                idle = (today - _dt.date.fromisoformat(last_active)).days
            elif joined_day:
                idle = (today - joined_day).days
            else:
                idle = None
            returned = bool(last_active and joined_day
                            and _dt.date.fromisoformat(last_active)
                            > joined_day)
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
                "stuck": bool(reason), "stuck_reason": reason,
                "ref": refs.get(beta_signup.account_key(a.email))
                or inviter or "",
                "returned": returned,
                "is_owner": beta_signup.account_key(a.email)
                in owner_keys})
    return out


# --- reading: the Morning block (owner only) ---------------------------------

#: The Morning line's chain, in order: (milestone, words). A tester counts
#: at a step only after reaching every step before it, so the numbers never
#: go up along the arrows. Connecting their own records is not in the chain:
#: every account starts on the made-up sample records and can ask and act
#: there, so "connected" would read as a gate it is not (owner review of
#: the first screenshot: 3 connected, then 4 answered).
CHAIN = (("joined", "signed in"), ("first_answer", "first answer"),
         ("action_approved", "first action"))

#: Shown beside the chain, not after it: (key, words).
SIDE = (("records_connected", "connected their own records"),
        ("returned", "came back another day"))

#: The nudges, fixed words chosen by fixed rules (`needs_you`). No model
#: writes them, and none says anything about a record.
NUDGES = {
    "never_asked": "Try asking: what's due for me this year?",
    "not_connected": "Want me to walk you through connecting? 2 minutes.",
    "never_acted": "Ask it to write your next-visit prep list.",
    "quiet": ("Did something get in the way? 1 confusing, 2 no time, "
              "3 not useful, 4 other."),
    "not_confirmed": ("Your CareAgents sign-up link is in your email. "
                      "Check spam, or reply and I'll send a new one."),
}

#: Where each rule says the person is stuck, for the owner.
STUCK_AT = {"never_asked": "joined, never asked",
            "not_connected": "asked, never connected their records",
            "never_acted": "answered, no action yet",
            "quiet": "quiet", "not_confirmed": "beta request not confirmed"}


def loop_counts(rows: list[dict]) -> dict:
    """`chain`: how many testers reached each CHAIN step and every step
    before it. `side`: how many connected their own records, and how many
    came back on a later day. The owner's own account is not a tester and
    is not counted. Signing in is every account row, funnel row or not, so
    it matches the table below."""
    people = [r for r in rows if not r["is_owner"]]
    chain, still = [], people
    for key, words in CHAIN:
        if key != "joined":
            still = [r for r in still if key in r["milestones"]]
        chain.append({"key": key, "label": words, "count": len(still)})
    side = [{"key": "records_connected", "label": SIDE[0][1],
             "count": sum(1 for r in people
                          if "records_connected" in r["milestones"])},
            {"key": "returned", "label": SIDE[1][1],
             "count": sum(1 for r in people if r["returned"])}]
    return {"chain": chain, "side": side}


def _rule(r: dict, can_connect: bool) -> str | None:
    """The first rule a tester matches, in loop order, or None."""
    m, idle = r["milestones"], r["idle_days"] or 0
    asked = "first_answer" in m or r["last_active"] is not None
    if not asked:
        return "never_asked" if idle >= 1 else None
    if "records_connected" not in m and can_connect and idle >= 2:
        return "not_connected"
    if "action_approved" not in m and idle >= 2:
        return "never_acted"
    if idle >= AWAY_DAYS:
        return "quiet"
    return None


def needs_you(rows: list[dict], pending: list[dict],
              can_connect=lambda email: False) -> list[dict]:
    """One row per stuck tester: email, where stuck, days since last
    active, and the nudge its rule picks. `can_connect(email)` says the
    tester may connect real records (an invite or the allowlist); the
    connect nudge goes only to them. Then the /beta requests nobody
    confirmed (`pending_requests`)."""
    out = []
    for r in rows:
        if r["is_owner"]:
            continue
        rule = _rule(r, bool(can_connect(r["email"])))
        if rule:
            out.append({"name": "", "email": r["email"], "rule": rule,
                        "stuck_at": STUCK_AT[rule], "days": r["idle_days"],
                        "since": ("last active" if r["last_active"]
                                  else "joined"),
                        "nudge": NUDGES[rule]})
    from careagents import beta_signup
    have = {beta_signup.account_key(r["email"]) for r in rows}
    for p in pending:
        # Asked on /beta, then came in another way: not waiting on a link.
        if beta_signup.account_key(p["email"]) in have:
            continue
        out.append({"name": p["first_name"], "email": p["email"],
                    "rule": "not_confirmed",
                    "stuck_at": STUCK_AT["not_confirmed"],
                    "days": p["days"], "since": "asked",
                    "nudge": NUDGES["not_confirmed"]})
    return out


def pending_requests(session_scope, moment: _dt.datetime | None = None,
                     limit: int = TESTERS_CAP) -> list[dict]:
    """/beta requests still waiting for the confirm link, newest first:
    the first name and email typed on /beta, and days since asked. Owner
    only, like the testers list."""
    from careagents import beta_signup
    moment = moment or _dt.datetime.now(_dt.timezone.utc)
    today = moment.date()
    with session_scope() as s:
        rows = (s.query(beta_signup.BetaRequest)
                .filter_by(status="pending")
                .order_by(beta_signup.BetaRequest.created_at.desc())
                .limit(max(1, int(limit))).all())
        return [{"first_name": r.first_name, "email": r.email,
                 "days": (today - _dt.datetime.fromtimestamp(
                     r.created_at, _dt.timezone.utc).date()).days
                 if r.created_at else None} for r in rows]


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
