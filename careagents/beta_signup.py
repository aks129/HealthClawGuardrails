"""The beta-tester sign-up: /beta, its request form, the confirmation and
"Remove my request" links, and the `beta-requests` operator commands
(docs/briefs/2026-10-06-beta-onboarding.md).

Double opt-in. A submit only creates (or refreshes) a `pending` request and
sends one confirmation email, at most one per mailbox per CONFIRM_CAP
seconds. The cap is recorded in the database, keyed on the normalized
mailbox (`mailbox`), so it survives restarts, holds across workers, and
counts `name+tag@` and Gmail dot variants as the one inbox they are.
Nothing else happens until the mailbox's owner confirms: no spot, no owner
notice, no "You're in". A submit for an address that is already confirmed
never overwrites it; it asks that address to confirm the change, and a
change nobody confirms is cleared once its link lapses.

What a request keeps is account data, not PHI: a first name, an email, an
optional mobile for iMessage, and a `ref`. Retention is what the page says
(`flask beta-requests purge`): the mobile goes once `added`, or after
MOBILE_DAYS; a number nobody confirmed goes when its link lapses; a request
whose email never became an account after REQUEST_DAYS; an unconfirmed one
after PENDING_DAYS. `delete_account` removes the row too.

The tables live here, not in models.py, so this feature stays in one file.
accounts.py imports it, which puts them in `Base.metadata` before any
engine is built.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import secrets
import time
import unicodedata
from collections import OrderedDict, deque

import click
# A dependency of `requests`, so always installed; the stdlib codec is
# IDNA 2003 and has no UTS-46 mapping.
import idna
from flask import jsonify, make_response, render_template, request
from markupsafe import escape
from sqlalchemy import Column, Float, String, update
from sqlalchemy.exc import IntegrityError

from careagents import mail
from careagents.models import Base, now

logger = logging.getLogger("careagents.beta_signup")

#: `pending` is not confirmed yet. The rest are the owner's (brief 6).
STATUSES = ("pending", "new", "added", "waitlist", "active", "removed")
_CONFIRMED = ("new", "added", "waitlist", "active")

#: The Sendblue sandbox answers ten contacts. A confirmed request with a
#: mobile past the tenth spot waits, first come first served, and so does
#: one sharing a mailbox or a mobile with a request that holds a spot.
IMESSAGE_SPOTS = 10
#: Statuses that hold an iMessage spot.
_HOLDS_A_SPOT = ("new", "added", "active")
#: Statuses whose account may see the "Text your assistant" tile: the
#: owner added the number to the sandbox, so the line answers it.
_CAN_TEXT = ("added", "active")

MOBILE_DAYS = 30
REQUEST_DAYS = 60
PENDING_DAYS = 7

#: One confirmation email per mailbox per day, and its link lasts as long.
CONFIRM_CAP = 86400
CONFIRM_SECONDS = 86400

#: Form submits per client address per window, and how many addresses the
#: limiter remembers before it forgets the oldest.
SUBMITS_PER_WINDOW = 5
WINDOW_SECONDS = 600
LIMITER_KEYS = 512

FEEDBACK_MAILTO = "mailto:contactus@healthclaw.io?subject=tester"
DEMO_VIDEOS_URL = ("https://github.com/aks129/HealthClawGuardrails/releases/"
                   "tag/demo-videos-2026-10")

_REF = re.compile(r"[a-z0-9-]{1,32}")
_EMAIL = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")
_TOKEN_MAX = 64
_DAY = 86400
_GMAIL = ("gmail.com", "googlemail.com")


class BetaRequest(Base):
    """One person asking to test (brief section 2). One row per email."""
    __tablename__ = "ca_beta_requests"
    email = Column(String(255), primary_key=True)
    first_name = Column(String(40), nullable=False)
    mobile = Column(String(16), nullable=True)
    # When a mobile was last given. Kept after the number is deleted, so
    # the iMessage spot it took is still counted.
    mobile_given_at = Column(Float, nullable=True)
    ref = Column(String(32), nullable=True)
    status = Column(String(16), nullable=False, default="pending")
    created_at = Column(Float, nullable=False, default=now)
    updated_at = Column(Float, nullable=False, default=now)
    # When the address's owner confirmed. Waitlist order.
    confirmed_at = Column(Float, nullable=True)
    # sha256 of the open confirmation token, and when it lapses.
    confirm_hash = Column(String(64), nullable=True)
    confirm_expires_at = Column(Float, nullable=True)
    # A change asked for on a confirmed request, applied on confirmation
    # and cleared when its link lapses.
    pending_first_name = Column(String(40), nullable=True)
    pending_mobile = Column(String(16), nullable=True)
    # sha256 of the newest "Remove my request" token. Never the token.
    removal_hash = Column(String(64), nullable=True)
    # Why a request waits besides a full line: it shares a mailbox or a
    # mobile with one that holds a spot. Our words, never a visitor's.
    note = Column(String(64), nullable=True)
    # Promoted from the waitlist and not yet told; cleared once emailed.
    promoted_at = Column(Float, nullable=True)


class BetaMailCap(Base):
    """When a confirmation was last emailed to a mailbox, keyed by the
    sha256 of the normalized mailbox. Outlives a removed request, so
    removing yourself does not reopen your inbox to a stranger's submits."""
    __tablename__ = "ca_beta_mail_caps"
    email_hash = Column(String(64), primary_key=True)
    sent_at = Column(Float, nullable=False)


# --- pure rules ---------------------------------------------------------------

def clean_ref(value) -> str | None:
    """`ref` as stored: [a-z0-9-]{1,32}, or nothing. Never a refusal."""
    if isinstance(value, str) and _REF.fullmatch(value):
        return value
    return None


def has_control(value: str) -> bool:
    """Any control or format character (Unicode Cc, Cf), NUL included."""
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in value)


def valid_email(value: str) -> bool:
    return (len(value) <= 254 and value.count("@") == 1
            and not has_control(value) and bool(_EMAIL.fullmatch(value)))


def ascii_email(value: str) -> str | None:
    """The address with its domain in ASCII, or None. The domain is
    encoded with IDNA/UTS-46 and the encoded form is kept, so a lookalike
    (`ｇｍａｉｌ.com`) is the host it resolves to (`gmail.com`) before
    `mailbox` folds it. A trailing dot (`gmail.com.`, the same host in
    DNS) and a domain that does not encode are refused."""
    if not valid_email(value):
        return None
    local, domain = value.split("@")
    if domain.endswith("."):
        return None
    try:
        encoded = idna.encode(domain, uts46=True).decode("ascii")
    except (idna.IDNAError, UnicodeError):
        return None
    email = f"{local}@{encoded.lower()}"
    return email if valid_email(email) else None


def mailbox(email: str) -> str:
    """The inbox an address delivers to: lowercased, `+tag` dropped on
    every domain, and for Gmail the dots dropped and googlemail folded in.
    Several addresses, one person's inbox: the cap and the spots count it
    once."""
    local, _, domain = email.strip().lower().rpartition("@")
    local = local.split("+", 1)[0]
    if domain in _GMAIL:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def clean_mobile(value: str) -> str | None:
    """A typed number as E.164, the way iMessage handles are read (ten
    digits are +1). An email-shaped handle is not a mobile."""
    from careagents.imessage import normalize_handle
    handle = normalize_handle(value)
    if handle is None or "@" in handle:
        return None
    return handle


def show_number(e164: str) -> str:
    """`+15550109999` -> `+1 555-010-9999`; any other number as stored."""
    m = re.fullmatch(r"\+1(\d{3})(\d{3})(\d{4})", e164 or "")
    return f"+1 {m[1]}-{m[2]}-{m[3]}" if m else (e164 or "")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_domain(email: str) -> str:
    """`avery@example.com` -> `e***.com`: enough to spot a pattern, not
    enough to name anyone."""
    domain = email.rsplit("@", 1)[-1]
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    return f"{domain[:1]}***.{tld}" if tld else f"{domain[:1]}***"


def _mask_mobile(mobile: str | None) -> str:
    return f"***{mobile[-4:]}" if mobile else "-"


def _safe(value) -> str:
    """A visitor's text for the operator's terminal: anything that is not
    printable (newlines, ESC sequences) is shown escaped, never acted on."""
    return "".join(c if c.isprintable() else repr(c)[1:-1]
                   for c in str(value))


def text_number(cfg) -> str:
    """The number testers text: the Sendblue line when it is on."""
    return cfg.sendblue_from_number if cfg.sendblue_enabled else ""


# --- storage ------------------------------------------------------------------

def _expire_lapsed(s, moment: float) -> None:
    """A link nobody used takes what it was asking about with it. On a
    confirmed row that is the change (name and number); on an unconfirmed
    one, the number. So a number a stranger typed lasts at most a day."""
    lapsed = (BetaRequest.confirm_expires_at.isnot(None)
              & (BetaRequest.confirm_expires_at <= moment))
    s.execute(update(BetaRequest)
              .where(lapsed, BetaRequest.status != "pending")
              .values(pending_first_name=None, pending_mobile=None,
                      confirm_hash=None, confirm_expires_at=None))
    s.execute(update(BetaRequest)
              .where(lapsed, BetaRequest.status == "pending")
              .values(mobile=None, mobile_given_at=None,
                      confirm_hash=None, confirm_expires_at=None))


def claim_mail(session_scope, email: str, at: float | None = None) -> bool:
    """Take the mailbox's one confirmation email for the day, or say it
    is taken. A conditional update and a keyed insert, so two workers
    racing on the same mailbox cannot both win."""
    moment = at if at is not None else time.time()
    key = hash_token(mailbox(email))
    try:
        with session_scope() as s:
            done = s.execute(
                update(BetaMailCap)
                .where(BetaMailCap.email_hash == key,
                       BetaMailCap.sent_at <= moment - CONFIRM_CAP)
                .values(sent_at=moment)).rowcount
            if done:
                return True
            if s.get(BetaMailCap, key) is not None:
                return False
            s.add(BetaMailCap(email_hash=key, sent_at=moment))
        return True
    except IntegrityError:
        return False


def _spot_holders(s, but: str):
    return (s.query(BetaRequest)
            .filter(BetaRequest.email != but,
                    BetaRequest.mobile_given_at.isnot(None),
                    BetaRequest.status.in_(_HOLDS_A_SPOT)).all())


def _why_wait(s, row) -> tuple[bool, str | None]:
    """(must wait, note). Full, or a twin already holds a spot: one spot
    per mailbox and one per mobile."""
    holders = _spot_holders(s, row.email)
    box = mailbox(row.email)
    for other in holders:
        if mailbox(other.email) == box:
            return True, "same mailbox as another request"
        if row.mobile and other.mobile == row.mobile:
            return True, "same mobile as another request"
    return len(holders) >= IMESSAGE_SPOTS, None


def _take_a_spot(s, row) -> None:
    wait, note = _why_wait(s, row)
    row.note = note
    if wait:
        row.status = "waitlist"


def _wants_mail(session_scope, email: str, first_name: str,
                mobile: str | None) -> bool:
    """Is there anything to confirm? A confirmed request asked for again
    with nothing new is left alone, and nobody is emailed."""
    with session_scope() as s:
        _expire_lapsed(s, time.time())
        row = s.get(BetaRequest, email)
        if row is None or row.status not in _CONFIRMED:
            return True
        return (first_name != row.first_name
                or (mobile is not None and mobile != row.mobile))


def submit(session_scope, first_name: str, email: str, mobile: str | None,
           ref: str | None) -> dict | None:
    """Record a request and return the tokens for its confirmation email,
    or None when nothing is sent (no change, or the day's email to that
    mailbox is spent). The caller answers the same either way."""
    if not _wants_mail(session_scope, email, first_name, mobile):
        return None
    if not claim_mail(session_scope, email):
        return None
    confirm, removal = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with session_scope() as s:
        row = s.get(BetaRequest, email)
        if row is None:
            row = BetaRequest(email=email, created_at=now())
            s.add(row)
        if row.status in _CONFIRMED:
            # Never overwritten here: the change waits for the address.
            kind = "change"
            row.pending_first_name = first_name
            row.pending_mobile = mobile
        else:
            # New, still pending, or removed by the owner: start again.
            kind = "join"
            row.status = "pending"
            row.first_name = first_name
            row.mobile = mobile
            row.mobile_given_at = now() if mobile else None
            row.confirmed_at = row.note = None
            row.pending_first_name = row.pending_mobile = None
            if ref:
                row.ref = ref
        row.confirm_hash = hash_token(confirm)
        row.confirm_expires_at = now() + CONFIRM_SECONDS
        row.removal_hash = hash_token(removal)
        row.updated_at = now()
        return {"kind": kind, "confirm": confirm, "removal": removal}


def _by_confirm(s, token: str):
    if not 0 < len(token) <= _TOKEN_MAX:
        return None
    return (s.query(BetaRequest)
            .filter(BetaRequest.confirm_hash == hash_token(token),
                    BetaRequest.confirm_expires_at > time.time()).first())


def confirm_preview(session_scope, token: str) -> dict | None:
    """What the confirm page shows: new or change, the first name, and the
    last four of the mobile. None when the link is spent or lapsed."""
    with session_scope() as s:
        _expire_lapsed(s, time.time())
        row = _by_confirm(s, token)
        if row is None:
            return None
        change = row.status in _CONFIRMED
        name = (row.pending_first_name or row.first_name) if change \
            else row.first_name
        number = row.pending_mobile if change else row.mobile
        return {"kind": "change" if change else "join", "first_name": name,
                "tail": number[-4:] if number else None}


def confirm(session_scope, token: str) -> dict | None:
    """Spend a confirmation token. A pending request joins (and takes an
    iMessage spot or a place in the queue); a confirmed one takes the
    change it asked for. None when the link is spent or lapsed."""
    with session_scope() as s:
        row = _by_confirm(s, token)
        if row is None:
            return None
        row.confirm_hash = row.confirm_expires_at = None
        row.updated_at = now()
        if row.status in _CONFIRMED:
            kind = "change"
            if row.pending_first_name:
                row.first_name = row.pending_first_name
            # Never a mobile on an `added` row: the page promises it goes.
            if row.pending_mobile and row.status != "added":
                row.mobile = row.pending_mobile
                if row.mobile_given_at is None:
                    row.mobile_given_at = now()
                    if row.status == "new":
                        _take_a_spot(s, row)
            row.pending_first_name = row.pending_mobile = None
        else:
            kind = "join"
            row.confirmed_at = now()
            row.status = "new"
            if row.mobile_given_at is not None:
                _take_a_spot(s, row)
        return {"kind": kind, "email": row.email,
                "first_name": row.first_name, "status": row.status,
                "gave_mobile": row.mobile_given_at is not None,
                "ref": row.ref, "note": row.note}


def promote(s) -> list[str]:
    """Fill free iMessage spots from the waitlist, oldest confirmation
    first, skipping a request whose twin (same mailbox or mobile) holds a
    spot. Called wherever a spot can free up. The promoted are marked to
    be told (`send_promotions`) and logged masked."""
    s.flush()
    promoted = []
    while len(_spot_holders(s, "")) < IMESSAGE_SPOTS:
        waiting = (s.query(BetaRequest).filter_by(status="waitlist")
                   .order_by(BetaRequest.confirmed_at,
                             BetaRequest.created_at).all())
        nxt = next((w for w in waiting if not _why_wait(s, w)[1]), None)
        if nxt is None:
            break
        nxt.status, nxt.note = "new", None
        nxt.promoted_at = nxt.updated_at = now()
        s.flush()
        promoted.append(nxt.email)
        logger.info("beta waitlist: promoted a request from %s",
                    mask_domain(nxt.email))
    return promoted


def new_removal_token(session_scope, email: str) -> str | None:
    """A fresh token for the next email to this request."""
    token = secrets.token_urlsafe(32)
    with session_scope() as s:
        row = s.get(BetaRequest, email)
        if row is None:
            return None
        row.removal_hash = hash_token(token)
        return token


def _by_token(s, token: str):
    if not 0 < len(token) <= _TOKEN_MAX:
        return None
    return (s.query(BetaRequest)
            .filter_by(removal_hash=hash_token(token)).first())


def token_is_live(session_scope, token: str) -> bool:
    with session_scope() as s:
        return _by_token(s, token) is not None


def remove(session_scope, token: str) -> bool:
    """Spend the token: the request is deleted, so the link works once."""
    with session_scope() as s:
        row = _by_token(s, token)
        if row is None:
            return False
        s.delete(row)
        promote(s)
        return True


def why_not(session_scope, email: str, status: str) -> str | None:
    """Why `mark` should refuse without --force, or None. A request
    nobody confirmed is not the owner's to set (double opt-in), and `new`
    must fit the ten spots and the one-per-mailbox-or-mobile rule."""
    with session_scope() as s:
        row = s.get(BetaRequest, email)
        if row is None:
            return None
        if row.status == "pending":
            return "this request is not confirmed yet"
        if (status != "new" or row.mobile_given_at is None
                or row.status in _HOLDS_A_SPOT):
            return None
        wait, note = _why_wait(s, row)
        if note:
            return f"it has the {note}"
        if wait:
            return f"all {IMESSAGE_SPOTS} iMessage spots are taken"
        return None


def mark(session_scope, email: str, status: str) -> list[str] | None:
    """Set the status. `added` and `removed` delete the mobile; `removed`
    frees a spot for the queue. None when there is no such request, else
    whoever was promoted."""
    with session_scope() as s:
        row = s.get(BetaRequest, email.strip().lower())
        if row is None:
            return None
        row.status = status
        if status in ("added", "removed"):
            row.mobile = None
            row.pending_mobile = None
        if status != "waitlist":
            row.note = None
        if status != "pending" and row.confirmed_at is None:
            row.confirmed_at = now()
        row.updated_at = now()
        return promote(s) if status == "removed" else []


def list_requests(session_scope, pending: bool = False) -> list[dict]:
    """Confirmed requests (or only the pending ones), the waitlist in
    queue order."""
    with session_scope() as s:
        _expire_lapsed(s, time.time())
        q = s.query(BetaRequest)
        q = (q.filter_by(status="pending") if pending
             else q.filter(BetaRequest.status != "pending"))
        rows = q.order_by(BetaRequest.created_at).all()
        out = [{"created_at": r.created_at, "first_name": r.first_name,
                "email": r.email, "mobile": r.mobile, "ref": r.ref,
                "status": r.status, "confirmed_at": r.confirmed_at,
                "note": r.note}
               for r in rows]
    waiting = sorted((r for r in out if r["status"] == "waitlist"),
                     key=lambda r: r["confirmed_at"] or 0)
    for i, r in enumerate(waiting, 1):
        r["queue"] = i
    return out


def purge(session_scope, at: float | None = None) -> tuple[int, int]:
    """Apply the retention the page states. Returns (requests deleted,
    mobiles deleted)."""
    from careagents.models import Account
    moment = at if at is not None else time.time()
    with session_scope() as s:
        _expire_lapsed(s, moment)
        accounts = {e for (e,) in s.query(Account.email)}
        gone = 0
        for row in s.query(BetaRequest).all():
            stale_pending = (row.status == "pending" and row.updated_at
                             < moment - PENDING_DAYS * _DAY)
            stale = (row.created_at < moment - REQUEST_DAYS * _DAY
                     and row.email not in accounts)
            if stale_pending or stale:
                s.delete(row)
                gone += 1
        s.flush()
        cleared = 0
        for row in (s.query(BetaRequest)
                    .filter(BetaRequest.mobile.isnot(None),
                            BetaRequest.mobile_given_at
                            < moment - MOBILE_DAYS * _DAY)):
            row.mobile = None
            cleared += 1
        (s.query(BetaMailCap)
         .filter(BetaMailCap.sent_at < moment - CONFIRM_CAP)
         .delete())
        promote(s)
        return gone, cleared


# --- email --------------------------------------------------------------------

#: A line in a tester email that carries the number as an `sms:` link.
_TEXT_HI = "text_hi"
#: A line whose web app address becomes a link in the HTML.
_WEB_LINK = "web_link"
_WEB = "https://careagents.cloud"


def text_hi_line(number: str) -> str:
    return (f"Text hi to {show_number(number)}. You'll get a link back to "
            f"sign in, then your assistant answers there.")


TEXTING_COMPANY_LINE = ("Your texts pass through a texting company we use, "
                        "so only use the made-up records here.")


def _line(x) -> tuple[str, str]:
    """(text, html) for one line. Every line is escaped; the texting line
    puts the number in an `sms:` link."""
    if isinstance(x, tuple) and x[0] == _TEXT_HI:
        text, shown = text_hi_line(x[1]), show_number(x[1])
        link = f"<a href='sms:{escape(x[1])}'>{escape(shown)}</a>"
        return text, str(escape(text)).replace(str(escape(shown)), link, 1)
    if isinstance(x, tuple) and x[0] == _WEB_LINK:
        text = x[1]
        return text, str(escape(text)).replace(
            _WEB, f"<a href='{_WEB}'>careagents.cloud</a>", 1)
    return x, str(escape(x))


def _tester_email(cfg, email: str, subject: str, lines: list,
                  token: str, link: tuple[str, str] | None = None) -> str:
    """One email to a tester: plain sentences, an optional action link,
    the feedback link and the single-use removal link. A name in a line is
    the visitor's own, read back to them, escaped."""
    remove_url = f"{cfg.origin}/beta/remove?t={token}"
    pairs = [_line(x) for x in lines]
    parts = [t for t, _ in pairs]
    if link:
        parts.append(f"{link[0]}: {link[1]}")
    text = "\n\n".join(
        parts + ["Something broke, or a question? Write to "
                 "contactus@healthclaw.io with the subject \"tester\".",
                 f"Remove my request: {remove_url}"])
    body = "".join(f"<p>{h}</p>" for _, h in pairs)
    if link:
        body += (f"<p><a href='{escape(link[1])}'>{escape(link[0])}</a>"
                 f"</p>")
    html = (f"<div style='font-family:system-ui,sans-serif;max-width:420px'>"
            f"<h2 style='color:#22190E'>CareAgents</h2>{body}"
            f"<p>Something broke, or a question? "
            f"<a href='{FEEDBACK_MAILTO}'>Tell us</a>.</p>"
            f"<p style='color:#5E5240;font-size:13px'>"
            f"<a href='{escape(remove_url)}'>Remove my request</a></p></div>")
    return mail.send_message(cfg, email, subject, html, text)


def youre_in_lines(cfg, first_name: str) -> list:
    return [f"Hi {first_name}, thanks for helping test CareAgents.",
            "Open careagents.cloud and sign up with this email. You'll use "
            "made-up records, not your own, and CareAgents is not a doctor.",
            "CareAgents is built by HealthClaw, so questions go to "
            "contactus@healthclaw.io."]


def _texting_lines(cfg) -> list:
    number = text_number(cfg)
    return [(_TEXT_HI, number), TEXTING_COMPANY_LINE] if number else []


def spot_opened_line(first_name: str) -> str:
    return (f"Hi {first_name}, an iMessage spot opened for you. We'll email "
            f"you again within a day once your number is ready to text. "
            f"Meanwhile the web app works: {_WEB}")


def send_promotions(session_scope, cfg) -> int:
    """Email everyone promoted from the waitlist and not yet told, the
    email the waitlist sentence promised. No texting line: the number is
    not in the Sendblue sandbox until the owner marks it `added`, which
    sends the "Text hi" email. Returns how many were sent to."""
    with session_scope() as s:
        rows = s.query(BetaRequest).filter(
            BetaRequest.promoted_at.isnot(None)).all()
        todo = [(r.email, r.first_name) for r in rows]
        for r in rows:
            r.promoted_at = None
    for email, first_name in todo:
        try:
            _tester_email(cfg, email, "An iMessage spot opened",
                          [(_WEB_LINK, spot_opened_line(first_name))],
                          new_removal_token(session_scope, email))
        except Exception:                   # pragma: no cover - defensive
            logger.warning("beta promotion email failed to send")
    return len(todo)


def _notify_owner(cfg, joined: dict) -> None:
    if not cfg.beta_notify_email:
        return
    line = (f"New beta tester: {joined['first_name']}. "
            f"Email domain: {mask_domain(joined['email'])}; "
            f"mobile: {'yes' if joined['gave_mobile'] else 'no'}; "
            f"ref: {joined['ref'] or '-'}; status: {joined['status']}. "
            f"Details: flask beta-requests list")
    mail.send_notice(cfg, cfg.beta_notify_email, "New beta tester",
                     str(escape(line)))


# --- routes and commands ------------------------------------------------------

#: Requests after which someone may have been promoted from the waitlist.
_FREES_A_SPOT = frozenset({"beta_remove", "delete_account"})


def register(app, svc, cfg) -> None:
    submits: OrderedDict[str, deque] = OrderedDict()

    def _client_key() -> str:
        # X-Real-IP, which Railway's edge sets to the client's address
        # (docs/runbooks/careagents-beta-requests.md). X-Forwarded-For is
        # not read: its entries can be the client's own words. Anything
        # that is not an address falls back to the direct peer.
        raw = (request.headers.get("X-Real-IP") or "").strip()
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            return request.remote_addr or ""

    def _allow_submit(key: str) -> bool:
        moment = time.time()
        window = submits.pop(key, None) or deque()
        while window and moment - window[0] > WINDOW_SECONDS:
            window.popleft()
        allowed = len(window) < SUBMITS_PER_WINDOW
        if allowed:
            window.append(moment)
        submits[key] = window              # newest last
        while len(submits) > LIMITER_KEYS:
            submits.popitem(last=False)    # forget the oldest address
        return allowed

    def _no_referrer(response):
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _page(outcome, status=200, **ctx):
        return _no_referrer(make_response(render_template(
            "beta_remove.html", outcome=outcome, feedback=FEEDBACK_MAILTO,
            **ctx), status))

    @app.after_request
    def _tell_the_promoted(response):
        if (request.endpoint in _FREES_A_SPOT
                and response.status_code == 200):
            try:
                send_promotions(svc.session, cfg)
            except Exception:               # pragma: no cover - defensive
                logger.warning("beta promotions not sent")
        return response

    @app.get("/beta")
    def beta_page():
        # The ref is never shown; that one came at all is the opening line.
        return render_template(
            "beta.html", ref=clean_ref(request.args.get("ref")) or "",
            demo_url=DEMO_VIDEOS_URL, feedback=FEEDBACK_MAILTO,
            mobile_days=MOBILE_DAYS, request_days=REQUEST_DAYS)

    @app.post("/beta")
    def beta_submit():
        # JSON only, as every other form here: a cross-site page cannot
        # send it without a preflight, which is the CSRF guard.
        if not request.is_json:
            return jsonify({"error": "json_required"}), 415
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "json_required"}), 415
        if not _allow_submit(_client_key()):
            return jsonify({"error": "rate_limited"}), 429
        # A bot fills every field. Same answer, nothing kept or sent.
        if body.get("website"):
            return jsonify({"ok": True})
        if body.get("consent") is not True:
            return jsonify({"error": "consent_required"}), 400
        first_name = str(body.get("first_name") or "").strip()
        if not 0 < len(first_name) <= 40 or has_control(first_name):
            return jsonify({"error": "first_name"}), 400
        email = ascii_email(str(body.get("email") or "").strip().lower())
        if email is None:
            return jsonify({"error": "email"}), 400
        mobile = None
        raw_mobile = str(body.get("mobile") or "").strip()
        if raw_mobile:
            mobile = clean_mobile(raw_mobile)
            if mobile is None:
                return jsonify({"error": "mobile"}), 400
        ref = clean_ref(body.get("ref"))
        tokens = submit(svc.session, first_name, email, mobile, ref)
        if tokens:
            url = f"{cfg.origin}/beta/confirm?t={tokens['confirm']}"
            if tokens["kind"] == "join":
                subject = "Confirm you asked to test CareAgents"
                lines = ["Confirm you asked to test CareAgents. The link "
                         "works for 24 hours.",
                         "If you didn't ask, ignore this email and nothing "
                         "more happens."]
            else:
                subject = "Confirm the change to your beta request"
                lines = ["Someone asked to change your CareAgents beta "
                         "request. Confirm it if it was you. The link works "
                         "for 24 hours.",
                         "If it wasn't you, ignore this email and nothing "
                         "changes."]
            try:
                _tester_email(cfg, email, subject, lines, tokens["removal"],
                              link=("Confirm", url))
            except Exception:               # pragma: no cover - defensive
                logger.warning("beta confirmation email failed to send")
        # The same answer whatever happened, so nothing can be learned
        # about an address from it.
        return jsonify({"ok": True})

    @app.get("/beta/confirm")
    def beta_confirm_ask():
        """Shows what would be confirmed, and changes nothing: mail
        scanners open GET links."""
        token = str(request.args.get("t") or "")
        preview = confirm_preview(svc.session, token)
        if preview is None:
            return _page("gone", 410)
        return _page("confirm_ask", token=token, preview=preview)

    @app.post("/beta/confirm")
    def beta_confirm():
        done = confirm(svc.session, str(request.form.get("t") or ""))
        if done is None:
            return _page("gone", 410)
        if done["kind"] == "join":
            try:
                lines = youre_in_lines(cfg, done["first_name"])
                if done["status"] == "waitlist" and done["note"]:
                    # Waiting because a twin holds a spot, not because the
                    # line is full: no promise of an email when one opens.
                    lines.append("Your request is on the waitlist for "
                                 "iMessage. The web app works today.")
                elif done["status"] == "waitlist":
                    lines.append("iMessage is full for now. We'll email you "
                                 "when a spot opens. The web app works "
                                 "today.")
                _tester_email(cfg, done["email"], "You're in the beta",
                              lines,
                              new_removal_token(svc.session, done["email"]),
                              link=("Open careagents.cloud",
                                    "https://careagents.cloud"))
                _notify_owner(cfg, done)
            except Exception:               # pragma: no cover - defensive
                logger.warning("beta welcome email failed to send")
        return _page("confirmed", done=done)

    @app.get("/beta/remove")
    def beta_remove_ask():
        """Asks, and changes nothing: mail scanners open GET links. The
        token is in the query string, which the access log does not keep."""
        token = str(request.args.get("t") or "")
        if not token_is_live(svc.session, token):
            return _page("gone", 410)
        return _page("ask", token=token)

    @app.post("/beta/remove")
    def beta_remove():
        if not remove(svc.session, str(request.form.get("t") or "")):
            return _page("gone", 410)
        return _page("removed")

    @app.get("/beta/kept")
    def beta_kept():
        """Where "Keep my request" lands: nothing changed, say so."""
        return _page("kept")

    @app.cli.group("beta-requests")
    def beta_requests():
        """Beta-tester requests from /beta."""

    @beta_requests.command("list")
    @click.option("--pending", is_flag=True,
                  help="Only requests not confirmed yet.")
    def beta_requests_list(pending):
        """Confirmed requests. The mobile is shown in full while `new` or
        `waitlist`; the waitlist shows its place in the queue and why a
        request waits if a twin holds a spot."""
        rows = list_requests(svc.session, pending=pending)
        if not rows:
            click.echo("no requests")
            return
        click.echo("date        first name  email  mobile  ref  status")
        for r in rows:
            day = time.strftime("%Y-%m-%d", time.gmtime(r["created_at"]))
            mobile = (r["mobile"] if r["status"] in ("new", "waitlist",
                                                     "pending")
                      and r["mobile"] else _mask_mobile(r["mobile"]))
            status = r["status"]
            if "queue" in r:
                status = f"waitlist #{r['queue']}"
            if r["note"]:
                status += f" ({r['note']})"
            click.echo(f"{day}  {_safe(r['first_name'])}  "
                       f"{_safe(r['email'])}  {_safe(mobile or '-')}  "
                       f"{_safe(r['ref'] or '-')}  {status}")

    @beta_requests.command("mark")
    @click.argument("email")
    @click.argument("status", type=click.Choice(STATUSES[1:]))
    @click.option("--force", is_flag=True,
                  help="Mark `new` even past the ten iMessage spots.")
    def beta_requests_mark(email, status, force):
        """Set a request's status. `added` deletes the mobile and emails
        the tester the number to text. `removed` frees a spot, which goes
        to the oldest waitlisted request. A request nobody confirmed is
        refused, and `new` respects the ten spots and one spot per mailbox
        or mobile, unless --force."""
        email = email.strip().lower()
        reason = None if force else why_not(svc.session, email, status)
        if reason:
            raise click.ClickException(
                f"not marked: {reason}. Use --force to mark it anyway")
        promoted = mark(svc.session, email, status)
        if promoted is None:
            raise click.ClickException("no request with that email")
        click.echo(f"{_safe(email)}: {status}")
        for p in promoted:
            click.echo(f"promoted from the waitlist: {_safe(p)}")
        send_promotions(svc.session, cfg)
        if status != "added":
            return
        if not text_number(cfg):
            click.echo("Sendblue is off; no email sent")
            return
        token = new_removal_token(svc.session, email)
        outcome = _tester_email(cfg, email, "Text your assistant",
                                _texting_lines(cfg), token)
        click.echo(f"email: {outcome}")

    @beta_requests.command("purge")
    def beta_requests_purge():
        """Delete what the page promises to delete: a number nobody
        confirmed once its link lapses, unconfirmed requests after 7 days,
        mobiles after 30, requests with no account after 60."""
        gone, cleared = purge(svc.session)
        click.echo(f"deleted {gone} request(s), {cleared} mobile(s)")
        told = send_promotions(svc.session, cfg)
        if told:
            click.echo(f"promoted and emailed: {told}")


def text_tile(cfg, hub: dict, session_scope, email: str) -> dict | None:
    """The number for "Text your assistant", or None to hide the tile.
    Shown to a sample account whose beta request the owner added to the
    Sendblue sandbox, while Sendblue is on: nobody else's text would be
    answered. Matched by mailbox, so an account signed up as an alias of
    the address that asked still sees it."""
    number = text_number(cfg)
    if not number or not hub["records"] or hub["has_real"]:
        return None
    box = mailbox(email or "")
    with session_scope() as s:
        added = (s.query(BetaRequest.email)
                 .filter(BetaRequest.status.in_(_CAN_TEXT)).all())
    if not any(mailbox(e) == box for (e,) in added):
        return None
    return {"number": show_number(number), "sms": f"sms:{number}"}
