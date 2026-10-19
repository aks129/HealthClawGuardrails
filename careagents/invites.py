"""Personal invite links: "Invite friends and family".

Each account has exactly one link, `/i?c=<code>`, made the first time its
owner opens Settings after a full sign-in. The code is 128 random bits and
travels in the query string, which the access log drops (#928 F5); opens
are rate-limited per client address on top. A link admits
CARE_INVITE_LINK_CAP sign-ups (default 5), or its own cap when the operator
set one; the operator can also revoke it (`flask invite-links`).

Opening a live link shows the one-step sign-up (/beta) with the code in a
hidden field. A submit notes which link the request came through
(`ca_invite_pending`); nothing is spent until the address confirms. On
that confirm the mailbox's one join row is written first, then the link's
seat is taken with one conditional UPDATE, so concurrent joins cannot pass
the cap and one inbox (+tags, Gmail dots) takes one seat, once, even after
removing its request and asking again (#928 F4).

Real records come with a join only when the inviter has real-record access
now: the environment allowlist, an owner address, or a live
RealRecordInvite (owner ruling on #928 F3). Then the joiner gets a fresh
RealRecordInvite, `invited_by` the inviter's account id, within the stage 1
cap (atomic, accounts.py) and once the tester terms are approved. An
invite that already exists, live or revoked by the operator, is never
touched (#928 F2). Otherwise they join on made-up records, and the hub says
so in one line.

The invitee sees the inviter's first name, if the inviter gave one on
their own beta request, and nothing else about them.

Account data only: codes, counts, timestamps, account ids, and a SHA-256
of the joiner's mailbox. No health data.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import secrets
import time
from collections import OrderedDict, deque

import click
from flask import make_response, render_template, request
from sqlalchemy import (Column, Float, ForeignKey, Integer, String,
                        UniqueConstraint, func, update)
from sqlalchemy.exc import IntegrityError

from careagents import funnel, tester_terms
from careagents.models import Account, Base, _uid, now

logger = logging.getLogger(__name__)

#: Link opens per client address per window, and how many addresses the
#: limiter remembers.
OPENS_PER_WINDOW = 30
WINDOW_SECONDS = 600
LIMITER_KEYS = 512

#: A pending note older than this belongs to a request whose confirm link
#: (24 hours) and unconfirmed retention (7 days) are both long gone.
PENDING_SECONDS = 8 * 86400

_CODE = re.compile(r"[0-9a-f]{32}")

#: What the joiner was given, for the hub's one line. NONE: the inviter
#: has no real-record access, or the joiner's invite was revoked.
GRANTED, FULL, CLOSED, HAD, NONE = ("granted", "full", "closed", "had",
                                    "none")


class InviteLink(Base):
    """An account's one invite link."""
    __tablename__ = "ca_invite_links"
    __table_args__ = (UniqueConstraint("account_id",
                                       name="uq_ca_invite_links_account"),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("inv"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"),
                        nullable=False, index=True)
    code = Column(String(32), nullable=False, unique=True, index=True)
    # NULL: the deployment's CARE_INVITE_LINK_CAP.
    cap = Column(Integer, nullable=True)
    used = Column(Integer, nullable=False, default=0)
    created_at = Column(Float, nullable=False, default=now)
    revoked_at = Column(Float, nullable=True)


def mailbox_hash(email: str) -> str:
    """SHA-256 of the inbox an address delivers to (beta_signup's folding:
    case, +tags, Gmail dots). The key of the two tables below."""
    from careagents import beta_signup
    return hashlib.sha256(
        beta_signup.account_key(email).encode("utf-8")).hexdigest()


def _undotted(email) -> str:
    """The address lowercased, with a trailing dot on its domain dropped
    (`corp.example.` is the same host in DNS) (#993 F2)."""
    typed = (email or "").strip().lower()
    local, at, domain = typed.rpartition("@")
    return f"{local}@{domain.rstrip('.')}" if at else typed


def invite_key(email) -> str:
    """What a real-record invite GRANTS on, and what `ca_real_record_invites`
    is keyed by. "" for no address.

    The grant fails closed, so this folds only what is known to reach one
    inbox: the address lowercased with its domain in ASCII
    (`beta_signup.ascii_email`) and no trailing dot, and on Gmail and
    googlemail the dots and a +tag dropped (`beta_signup.mailbox`). On any
    other domain a +tag is kept: where `+` is an ordinary character,
    `bob+x@` and `bob@` are two people. What DENIES uses the wider
    `inbox_key` (#993 F1)."""
    from careagents import beta_signup
    typed = _undotted(email)
    if not typed:
        return ""
    address = beta_signup.ascii_email(typed) or typed
    if address.rpartition("@")[2] in beta_signup._GMAIL:
        return beta_signup.mailbox(address)
    return address


def inbox_key(email) -> str:
    """What a real-record invite DENIES on: `beta_signup.account_key`,
    which folds +tags on every domain, with no trailing dot. A revoke, an
    invite already held, a link join and the stage 1 place all count the
    inbox this wide, so `carol+x@` cannot undo the revoke of `carol@` or
    take a second place for it (#993 F1). "" for no address."""
    from careagents import beta_signup
    typed = _undotted(email)
    return beta_signup.account_key(typed) if typed else ""


def alias_invite(rows, email) -> str | None:
    """Of `rows` ((key, revoked_at) pairs from ca_real_record_invites), is
    there an invite on the same inbox (`inbox_key`) under another key?
    "revoked" when any such is revoked, else "live", else None."""
    key, inbox = invite_key(email), inbox_key(email)
    states = {"revoked" if revoked_at is not None else "live"
              for other, revoked_at in rows
              if other != key and inbox_key(other) == inbox}
    return ("revoked" if "revoked" in states
            else "live" if states else None)


class InvitePending(Base):
    """A beta request made through a link, waiting for its confirm."""
    __tablename__ = "ca_invite_pending"
    mailbox_hash = Column(String(64), primary_key=True)
    link_id = Column(String(32), nullable=False)
    created_at = Column(Float, nullable=False, default=now)


class InviteJoin(Base):
    """One mailbox that joined through a link: which link, whose, when, and
    what the real-record invite came to (`granted`, `had`, `full`,
    `closed`, `none`). One row per mailbox, ever: it is written before the
    seat is taken, so a second alias or a second request takes none. Keyed
    by a hash, not an address, so it can outlive the account as a count.
    `link_id` and `inviter_id` are plain pointers."""
    __tablename__ = "ca_invite_joins"
    mailbox_hash = Column(String(64), primary_key=True)
    link_id = Column(String(32), nullable=False, index=True)
    inviter_id = Column(String(32), nullable=False, index=True)
    joined_at = Column(Float, nullable=False, default=now)
    real_records = Column(String(8), nullable=False)


# --- links --------------------------------------------------------------------

def cap_of(link, default_cap: int) -> int:
    return default_cap if link.cap is None else link.cap


def _view(link, default_cap: int) -> dict:
    return {"id": link.id, "code": link.code, "used": link.used or 0,
            "cap": cap_of(link, default_cap),
            "revoked": link.revoked_at is not None}


def link_for(session_scope, account_id: str, default_cap: int) -> dict:
    """The account's link, made on the first call. Two first views at once
    both try to insert; the unique account_id lets one win and the other
    reads the winner's row."""
    with session_scope() as s:
        row = s.query(InviteLink).filter_by(account_id=account_id).first()
        if row is not None:
            return _view(row, default_cap)
    try:
        with session_scope() as s:
            row = InviteLink(account_id=account_id,
                             code=secrets.token_hex(16), used=0,
                             created_at=now())
            s.add(row)
            s.flush()
            return _view(row, default_cap)
    except IntegrityError:
        with session_scope() as s:
            row = s.query(InviteLink).filter_by(account_id=account_id).one()
            return _view(row, default_cap)


def _live(s, code: str, default_cap: int):
    """The link a code names, if it can still admit someone; else None.
    Unknown, revoked and full are all None: the page says one thing."""
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        return None
    row = s.query(InviteLink).filter_by(code=code).first()
    if (row is None or row.revoked_at is not None
            or (row.used or 0) >= cap_of(row, default_cap)):
        return None
    return row


def inviter_first_name(s, account_id: str) -> str | None:
    """The first name the inviter gave on their own confirmed beta request,
    matched by mailbox. Never the email."""
    from careagents import beta_signup
    acct = s.get(Account, account_id)
    if acct is None:
        return None
    key = beta_signup.account_key(acct.email)
    for req in s.query(beta_signup.BetaRequest).filter(
            beta_signup.BetaRequest.confirmed_at.isnot(None)):
        if beta_signup.account_key(req.email) == key and req.first_name:
            return req.first_name
    return None


def inviter_name_for_code(session_scope, code, default_cap: int) -> str | None:
    """The inviter's first name for a live code (`inviter_first_name`), or
    None: unknown, revoked and full codes name nobody."""
    with session_scope() as s:
        link = _live(s, code, default_cap)
        return (inviter_first_name(s, link.account_id)
                if link is not None else None)


def note_pending(session_scope, email: str, code, default_cap: int) -> bool:
    """Remember that the request for `email` came through `code`. Ignored
    (False) when the code is not a live link, or is the address's own link.
    Old notes are cleared on the way."""
    from careagents import beta_signup
    with session_scope() as s:
        (s.query(InvitePending)
         .filter(InvitePending.created_at < now() - PENDING_SECONDS)
         .delete())
        link = _live(s, code, default_cap)
        if link is None:
            return False
        owner = s.get(Account, link.account_id)
        if owner is not None and (beta_signup.account_key(owner.email)
                                  == beta_signup.account_key(email)):
            return False
        key = mailbox_hash(email)
        row = s.get(InvitePending, key)
        if row is None:
            s.add(InvitePending(mailbox_hash=key, link_id=link.id,
                                created_at=now()))
        else:
            row.link_id, row.created_at = link.id, now()
        return True


def claim_seat(session_scope, link_id: str, default_cap: int) -> bool:
    """Take one seat on the link, or say there is none. One conditional
    UPDATE: under concurrent joins the database admits exactly `cap`."""
    stmt = (update(InviteLink)
            .where(InviteLink.id == link_id,
                   InviteLink.revoked_at.is_(None),
                   InviteLink.used < func.coalesce(InviteLink.cap,
                                                   default_cap))
            .values(used=InviteLink.used + 1))
    with session_scope() as s:
        return s.execute(stmt).rowcount == 1


def inviter_has_access(svc, cfg, account) -> bool:
    """May this account's link pass on real records (#928 F3, owner
    ruling)? Only while it has real-record access itself: its email on the
    environment allowlist or the owner list, or a live invite. Never while
    paused."""
    if account is None or svc.is_paused(account.id):
        return False
    email = (account.email or "").strip().lower()
    return (email in cfg.real_records_allowlist
            or email in cfg.owner_emails
            or svc.real_records_invited(email))


def _real_records_for(svc, cfg, email: str, inviter_id: str) -> str:
    """The real-record outcome of one join. Only ever a fresh invite."""
    from careagents.models import RealRecordInvite
    if not tester_terms.approved():
        return CLOSED
    if not inviter_has_access(svc, cfg, svc.get_account(inviter_id)):
        return NONE
    with svc.session() as s:
        rows = s.query(RealRecordInvite.email,
                       RealRecordInvite.revoked_at).all()
    if alias_invite(rows, email) is not None:
        return NONE         # the inbox holds one under another alias
    # The same check runs again inside the write, under its lock.
    outcome = svc.grant_join_invite(email, inviter_id)
    return {"created": GRANTED, "live": HAD, "full": FULL}.get(outcome, NONE)


def on_join(svc, cfg, email: str) -> dict | None:
    """A beta request just confirmed as a new join. If it came through a
    link, the mailbox has never joined through one, and a seat is free:
    record the join, settle real records (`_real_records_for`), and record
    the inviter on the account's funnel row if the account exists. None
    when no link is involved."""
    key = mailbox_hash(email)
    with svc.session() as s:
        pending = s.get(InvitePending, key)
        if pending is None:
            return None
        link_id = pending.link_id
        s.delete(pending)
        link = s.get(InviteLink, link_id)
        inviter_id = link.account_id if link is not None else None
    if inviter_id is None:
        return {"joined": False}
    try:
        with svc.session() as s:
            s.add(InviteJoin(mailbox_hash=key, link_id=link_id,
                             inviter_id=inviter_id, joined_at=now(),
                             real_records=NONE))
    except IntegrityError:
        # This inbox joined through a link before: no second seat.
        return {"joined": False}
    if not claim_seat(svc.session, link_id, cfg.invite_link_cap):
        with svc.session() as s:
            s.query(InviteJoin).filter_by(mailbox_hash=key).delete()
        return {"joined": False}
    outcome = _real_records_for(svc, cfg, email, inviter_id)
    with svc.session() as s:
        s.get(InviteJoin, key).real_records = outcome
    with svc.session() as s:
        acct = s.query(Account).filter_by(email=email).first()
        account_id = acct.id if acct is not None else None
    if account_id:
        funnel.safely(funnel.set_inviter, svc.session, account_id,
                      inviter_id)
    logger.info("invite link %s admitted a sign-up (%s)", link_id, outcome)
    return {"joined": True, "real_records": outcome}


def inviter_for_email(s, email: str) -> str | None:
    row = s.get(InviteJoin, mailbox_hash(email))
    return row.inviter_id if row is not None else None


def join_outcome(session_scope, email: str) -> str | None:
    """What the real-record invite came to for an address whose mailbox
    joined through a link, or None."""
    with session_scope() as s:
        row = s.get(InviteJoin, mailbox_hash(email))
        return row.real_records if row is not None else None


def per_inviter(s) -> list[dict]:
    """Joins per inviter account id, most first."""
    rows = (s.query(InviteJoin.inviter_id,
                    func.count(InviteJoin.mailbox_hash))
            .group_by(InviteJoin.inviter_id).all())
    return sorted(({"inviter_id": i, "joined": n} for i, n in rows),
                  key=lambda r: (-r["joined"], r["inviter_id"]))


def delete_for_account(s, account) -> None:
    """With the account: its link and its pending note. Join rows stay, its
    own and the ones its link admitted: they hold a hash and ids, not an
    address, and keep a returning inbox from taking a second seat."""
    s.query(InviteLink).filter_by(account_id=account.id).delete()
    s.query(InvitePending).filter_by(
        mailbox_hash=mailbox_hash(account.email or "")).delete()


# --- operator -----------------------------------------------------------------

def _find(s, which: str):
    """A link by its code or by its owner's email."""
    which = (which or "").strip().lower()
    if "@" in which:
        acct = s.query(Account).filter_by(email=which).first()
        return (s.query(InviteLink).filter_by(account_id=acct.id).first()
                if acct is not None else None)
    return s.query(InviteLink).filter_by(code=which).first()


def list_links(session_scope, default_cap: int) -> list[dict]:
    with session_scope() as s:
        out = []
        for link in s.query(InviteLink).order_by(InviteLink.created_at):
            owner = s.get(Account, link.account_id)
            out.append({**_view(link, default_cap),
                        "email": owner.email if owner else "-",
                        "account_id": link.account_id})
        return out


def revoke(session_scope, which: str) -> bool:
    with session_scope() as s:
        link = _find(s, which)
        if link is None or link.revoked_at is not None:
            return False
        link.revoked_at = now()
        return True


def set_cap(session_scope, which: str, cap: int | None) -> bool:
    with session_scope() as s:
        link = _find(s, which)
        if link is None:
            return False
        link.cap = cap
        return True


# --- routes and commands ------------------------------------------------------

def register(app, svc, cfg, beta_context) -> None:
    """`/i?c=<code>` and the `invite-links` commands. `beta_context()` is
    the sign-up page's own template context (careagents/beta_signup.py)."""
    opens: OrderedDict[str, deque] = OrderedDict()

    def _client_key() -> str:
        # As beta_signup: Railway's edge sets X-Real-IP; X-Forwarded-For is
        # the client's own words and is not read.
        raw = (request.headers.get("X-Real-IP") or "").strip()
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            return request.remote_addr or ""

    def _allow_open(key: str) -> bool:
        moment = time.time()
        window = opens.pop(key, None) or deque()
        while window and moment - window[0] > WINDOW_SECONDS:
            window.popleft()
        allowed = len(window) < OPENS_PER_WINDOW
        if allowed:
            window.append(moment)
        opens[key] = window
        while len(opens) > LIMITER_KEYS:
            opens.popitem(last=False)
        return allowed

    def _closed(status: int):
        resp = make_response(render_template("invite_closed.html"), status)
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.get("/i")
    def invite_open():
        # In the query string, never the path: the access log keeps the
        # path and drops the query (#928 F5). There is no /i/<code> route.
        code = str(request.args.get("c") or "")
        if not _allow_open(_client_key()):
            funnel.safely(funnel.count_issue, svc.session, "rate_limited")
            return _closed(429)
        with svc.session() as s:
            link = _live(s, code, cfg.invite_link_cap)
            inviter_id = link.account_id if link is not None else None
            name = (inviter_first_name(s, inviter_id)
                    if link is not None else None)
        if link is None:
            # Unknown, revoked and full: the same page and status.
            return _closed(404)
        # Whether a join through this link would carry real records today,
        # so the page does not say "made-up only" to someone it would not
        # be true for (#934 QA). The grant itself is decided at confirm.
        real = (cfg.real_records != "off" and tester_terms.approved()
                and inviter_has_access(svc, cfg, svc.get_account(inviter_id)))
        resp = make_response(render_template(
            "beta.html", **beta_context(), invite={"code": code,
                                               "first_name": name,
                                               "real": real},
            # The banner says the same as the page about whose records.
            banner_records=("made-up records first, then your own"
                            if real else "made-up records only")))
        # The code is in this page's address: it goes to no other site.
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.cli.group("invite-links")
    def invite_links():
        """Personal invite links: list, revoke, set a cap."""

    @invite_links.command("list")
    def invite_links_list():
        """Every link: its owner, seats used of its cap, and state."""
        rows = list_links(svc.session, cfg.invite_link_cap)
        if not rows:
            click.echo("no invite links")
            return
        for r in rows:
            state = "revoked" if r["revoked"] else "live"
            click.echo(f"{r['code']}  {r['email']:<36} "
                       f"{r['used']}/{r['cap']}  {state}")

    @invite_links.command("revoke")
    @click.argument("which")
    def invite_links_revoke(which):
        """Revoke a link, named by its code or its owner's email. A revoked
        link admits nobody, including requests already made through it."""
        if not revoke(svc.session, which):
            raise click.ClickException("no live link by that code or email")
        click.echo("revoked")

    @invite_links.command("cap")
    @click.argument("which")
    @click.argument("cap")
    def invite_links_cap(which, cap):
        """Set one link's cap (a whole number), or `default` to follow
        CARE_INVITE_LINK_CAP again."""
        if cap == "default":
            value = None
        else:
            try:
                value = int(cap)
            except ValueError:
                raise click.ClickException(
                    "cap must be a whole number or 'default'") from None
            if value < 0:
                raise click.ClickException("cap must not be negative")
        if not set_cap(svc.session, which, value):
            raise click.ClickException("no link by that code or email")
        click.echo(f"cap set to {cap}")


def real_record_rows(session_scope) -> list[dict]:
    """The real-record invites for the owner's list (`flask invites list`):
    the address as typed, the inbox it matches, and whether an account on
    that inbox exists yet, so "invited but not joined" shows."""
    from careagents.models import RealRecordInvite
    with session_scope() as s:
        joined = {invite_key(e) for (e,) in s.query(Account.email)}
        return [{"display": r.display_email or r.email, "key": r.email,
                 "invited_at": r.invited_at, "invited_by": r.invited_by,
                 "revoked_at": r.revoked_at, "signed_up": r.email in joined}
                for r in s.query(RealRecordInvite).order_by(
                    RealRecordInvite.invited_at)]


def settings_row(svc, cfg, account_id: str) -> dict:
    """What Settings shows: the link, seats used of the cap, and state."""
    link = link_for(svc.session, account_id, cfg.invite_link_cap)
    return {**link, "url": f"{cfg.origin}/i?c={link['code']}"}

