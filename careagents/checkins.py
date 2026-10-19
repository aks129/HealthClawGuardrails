"""Tester check-in emails: a short note on day 2 and day 7 after joining.

"How's it going? What's confusing?" with a one-tap link to /feedback that
says which email it came from, and a link to stop these emails. The copy is
fixed and carries nothing about the account or its records. Reply-to is the
team inbox and the owner is CC'd, so a reply reaches both.

Off unless CARE_CHECKIN_EMAILS is on, and never sent without a CC address
(CARE_CHECKIN_CC, else CARE_OWNER_EMAILS). The worker looks every
`funnel.DIGEST_TICK_SECONDS`.

One table, `ca_checkin_mail`: one row per account per kind, an account id,
a fixed kind and a time. No address, no message. A row is taken before the
send, so two workers cannot both send one email; a send the provider
refused gives the row back, so a later tick tries again. The opt-out is a
row of its own kind. Rows go with the account (`delete_for_account`).

An email is due while the account is between N and N + WINDOW_DAYS days
old, so turning the flag on does not send a "day 2" note to an account
that joined a month ago. A worker down for the whole window misses it.

Addresses are never logged: the worker logs counts, the preview prints
account ids unless the owner passes --show-emails.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re

import click
from flask import render_template, request
from markupsafe import escape
from sqlalchemy import Column, Float, String
from sqlalchemy.exc import IntegrityError

from careagents import funnel, mail
from careagents.models import Account, Base, now

logger = logging.getLogger("careagents.checkins")

#: (kind, the day it is due). The kind is also the `screen` its feedback
#: link carries.
KINDS = (("checkin_day2", 2), ("checkin_day7", 7))
#: The row that says the account asked to stop.
STOPPED = "stopped"
#: How many days after its day an email is still sent.
WINDOW_DAYS = 3

SUBJECT = "How's it going?"
LINE = "How's it going? What's confusing?"
REPLY_TO = "contactus@healthclaw.io"

_DAY = 86400
_TOKEN = re.compile(r"\A([A-Za-z0-9_-]{1,32})\.([0-9a-f]{64})\Z")


class CheckinMail(Base):
    """One check-in sent (or claimed) for one account, or its opt-out."""
    __tablename__ = "ca_checkin_mail"
    account_id = Column(String(32), primary_key=True)
    kind = Column(String(16), primary_key=True)
    at = Column(Float, nullable=False)


# --- the stop link ------------------------------------------------------------

def _key(cfg) -> bytes:
    # Derived, so a stop token is never a session signature or vice versa.
    return hmac.new(cfg.session_secret.encode("utf-8"),
                    b"careagents-checkin-stop-v1", hashlib.sha256).digest()


def stop_token(cfg, account_id: str) -> str:
    tag = hmac.new(_key(cfg), account_id.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{account_id}.{tag}"


def read_token(cfg, token: str) -> str | None:
    """The account id a stop token was made for, or None."""
    m = _TOKEN.match(token or "")
    if not m or not cfg.session_secret:
        return None
    from careagents.accounts import secret_matches
    if not secret_matches(m.group(0), stop_token(cfg, m.group(1))):
        return None
    return m.group(1)


def opt_out(session_scope, account_id: str) -> bool:
    """Record the opt-out. Returns False when the account is gone."""
    try:
        with session_scope() as s:
            if s.get(Account, account_id) is None:
                return False
            if s.get(CheckinMail, (account_id, STOPPED)) is None:
                s.add(CheckinMail(account_id=account_id, kind=STOPPED,
                                  at=now()))
    except IntegrityError:
        pass                                # a double tap: already stopped
    return True


def delete_for_account(s, account_id: str) -> None:
    """The account's rows go with it (`delete_account`)."""
    s.query(CheckinMail).filter_by(account_id=account_id).delete()


# --- who is due -----------------------------------------------------------------

def cc_for(cfg) -> list[str]:
    return sorted(cfg.checkin_cc or cfg.owner_emails)


def due(session_scope, at: float | None = None) -> list[dict]:
    """[{account_id, email, kind}] for every email due now: in its window,
    not sent, not stopped. Oldest account first."""
    at = now() if at is None else at
    oldest = at - (max(d for _, d in KINDS) + WINDOW_DAYS) * _DAY
    newest = at - min(d for _, d in KINDS) * _DAY
    out = []
    with session_scope() as s:
        accts = (s.query(Account.id, Account.email, Account.created_at)
                 .filter(Account.created_at.isnot(None),
                         Account.created_at > oldest,
                         Account.created_at <= newest)
                 .order_by(Account.created_at).all())
        ids = [a.id for a in accts]
        done = set()
        if ids:
            done = {(r.account_id, r.kind) for r in s.query(CheckinMail)
                    .filter(CheckinMail.account_id.in_(ids))}
    for a in accts:
        if (a.id, STOPPED) in done:
            continue
        age = at - a.created_at
        for kind, day in KINDS:
            if (day * _DAY <= age < (day + WINDOW_DAYS) * _DAY
                    and (a.id, kind) not in done):
                out.append({"account_id": a.id, "email": a.email,
                            "kind": kind})
    return out


def compose(cfg, account_id: str, kind: str) -> tuple[str, str]:
    """(html, text). Fixed words and two links, nothing about the account
    but the id inside the signed stop link."""
    feedback_url = f"{cfg.origin}/feedback?screen={kind}"
    stop_url = f"{cfg.origin}/emails/stop?t={stop_token(cfg, account_id)}"
    text = (f"{LINE}\n\nReply to this email or tap here to tell us: "
            f"{feedback_url}\n\nStop these emails: {stop_url}")
    html = (f"<div style='font-family:system-ui,sans-serif;max-width:420px'>"
            f"<h2 style='color:#22190E'>CareAgents</h2>"
            f"<p>{escape(LINE)}</p>"
            f"<p>Reply to this email or "
            f"<a href='{escape(feedback_url)}'>tap here to tell us</a>.</p>"
            f"<p style='color:#5E5240;font-size:13px'>"
            f"<a href='{escape(stop_url)}'>Stop these emails</a></p></div>")
    return html, text


# --- sending --------------------------------------------------------------------

def _claim(session_scope, account_id: str, kind: str) -> str | None:
    """Take the row for this email. Returns the address to send to, or None
    when another worker has it, it was sent, the account asked to stop, or
    the account is gone."""
    try:
        with session_scope() as s:
            acct = s.get(Account, account_id)
            if acct is None:
                return None
            if (s.get(CheckinMail, (account_id, STOPPED)) is not None
                    or s.get(CheckinMail, (account_id, kind)) is not None):
                return None
            s.add(CheckinMail(account_id=account_id, kind=kind, at=now()))
            return acct.email
    except IntegrityError:
        return None


def _release(session_scope, account_id: str, kind: str) -> None:
    with session_scope() as s:
        s.query(CheckinMail).filter_by(account_id=account_id,
                                       kind=kind).delete()


def send_due(session_scope, cfg, at: float | None = None) -> dict:
    """Send every email due now, each once. Returns counts by outcome.
    Off without the flag, a CC address or a signing secret."""
    counts = {"sent": 0, "unconfirmed": 0, "not_sent": 0, "skipped": 0}
    cc = cc_for(cfg)
    if not cfg.checkin_emails or not cc or not cfg.session_secret:
        return counts
    for row in due(session_scope, at):
        email = _claim(session_scope, row["account_id"], row["kind"])
        if email is None:
            counts["skipped"] += 1
            continue
        html, text = compose(cfg, row["account_id"], row["kind"])
        try:
            outcome = mail.send_message(
                cfg, email, SUBJECT, html, text, reply_to=REPLY_TO,
                cc=[x for x in cc if x != email.strip().lower()])
        except Exception:  # noqa: BLE001 - the row is given back below
            logger.warning("checkin send raised")
            outcome = mail.NOT_SENT
        if outcome == mail.NOT_SENT:
            _release(session_scope, row["account_id"], row["kind"])
            funnel.note_mail(session_scope, cfg, outcome)
            counts["not_sent"] += 1
        elif outcome == mail.UNCONFIRMED:
            # It may be in the inbox already: keep the row, never send twice.
            counts["unconfirmed"] += 1
        else:
            counts["sent"] += 1
    return counts


def run(session_scope, cfg, stop_event) -> None:
    """The worker's check-in thread. Never raises."""
    while not stop_event.is_set():
        try:
            counts = send_due(session_scope, cfg)
            if any(counts.values()):
                logger.info("checkins: %s", " ".join(
                    f"{k}={v}" for k, v in counts.items()))
        except Exception:  # noqa: BLE001 - the thread keeps going
            logger.warning("checkin tick failed", exc_info=True)
        stop_event.wait(funnel.DIGEST_TICK_SECONDS)


def totals(session_scope) -> dict:
    """How many rows of each kind: sent (or in flight) and stopped."""
    out = {k: 0 for k, _ in KINDS}
    out[STOPPED] = 0
    with session_scope() as s:
        for (kind,) in s.query(CheckinMail.kind):
            if kind in out:
                out[kind] += 1
    return out


# --- surfaces -------------------------------------------------------------------

def register(app, svc, cfg) -> None:
    """The stop link and the `checkins` command group."""

    @app.get("/emails/stop")
    def checkins_stop_ask():
        """Asks, and changes nothing: mail scanners open GET links (as
        /beta/remove). The token is in the query string, which the access
        log does not keep."""
        token = str(request.args.get("t") or "")
        if read_token(cfg, token) is None:
            return render_template("checkin_stop.html", outcome="gone"), 410
        return render_template("checkin_stop.html", outcome="ask",
                               token=token)

    @app.post("/emails/stop")
    def checkins_stop():
        account_id = read_token(cfg, str(request.form.get("t") or ""))
        if account_id is None or not opt_out(svc.session, account_id):
            return render_template("checkin_stop.html", outcome="gone"), 410
        return render_template("checkin_stop.html", outcome="stopped")

    @app.cli.group("checkins")
    def checkins_group():
        """Tester check-in emails (day 2 and day 7)."""

    @checkins_group.command("preview")
    @click.option("--show-emails", is_flag=True,
                  help="Print each address beside its account id.")
    def checkins_preview(show_emails):
        """Who would get which email on the next tick. Sends nothing."""
        cc = cc_for(cfg)
        click.echo(f"CARE_CHECKIN_EMAILS: "
                   f"{'on' if cfg.checkin_emails else 'off'}")
        click.echo(f"cc: {', '.join(cc) if show_emails else len(cc)}"
                   if cc else "cc: none set, so nothing is sent")
        rows = due(svc.session)
        if not rows:
            click.echo("nothing due")
        for r in rows:
            line = f"{r['account_id']}  {r['kind']}"
            click.echo(f"{line}  {r['email']}" if show_emails else line)
        t = totals(svc.session)
        click.echo("so far: " + " ".join(f"{k}={v}" for k, v in t.items()))
