"""The beta-tester sign-up: /beta, its request form, the confirmation and
"Remove my request" links, and the `beta-requests` operator commands
(docs/briefs/2026-10-06-beta-onboarding.md).

Double opt-in. A submit only creates (or refreshes) a `pending` request and
sends one confirmation email, at most one per address per CONFIRM_CAP
seconds, recorded in the database so the cap holds across restarts and
workers. Nothing else happens until the address's owner confirms: no spot,
no owner notice, no "You're in". A submit for an address that is already
confirmed never overwrites it; it asks that address to confirm the change.

What a request keeps is account data, not PHI: a first name, an email, an
optional mobile for iMessage, and a `ref`. Retention is what the page says
(`flask beta-requests purge`): the mobile goes once `added`, or after
MOBILE_DAYS; a request whose email never became an account after
REQUEST_DAYS; an unconfirmed one after PENDING_DAYS. `delete_account`
removes the row too.

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
#: mobile past the tenth spot waits, first come first served.
IMESSAGE_SPOTS = 10
#: Statuses that hold an iMessage spot.
_HOLDS_A_SPOT = ("new", "added", "active")

MOBILE_DAYS = 30
REQUEST_DAYS = 60
PENDING_DAYS = 7

#: One confirmation email per address per day, and its link lasts as long.
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

#: X-Forwarded-For entries that are an internal hop, or made up: Railway's
#: edge writes the public client address, so one of these on the right is
#: not a client and is not a bucket of its own.
_INTERNAL = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    "169.254.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10"))


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
    # A change asked for on a confirmed request, applied on confirmation.
    pending_first_name = Column(String(40), nullable=True)
    pending_mobile = Column(String(16), nullable=True)
    # sha256 of the newest "Remove my request" token. Never the token.
    removal_hash = Column(String(64), nullable=True)


class BetaMailCap(Base):
    """When a confirmation was last emailed to an address, keyed by the
    address's sha256. Outlives a removed request, so removing yourself
    does not reopen your inbox to a stranger's submits."""
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


def clean_mobile(value: str) -> str | None:
    """A typed number as E.164, the way iMessage handles are read (ten
    digits are +1). An email-shaped handle is not a mobile."""
    from careagents.imessage import normalize_handle
    handle = normalize_handle(value)
    if handle is None or "@" in handle:
        return None
    return handle


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

def claim_mail(session_scope, email: str, at: float | None = None) -> bool:
    """Take the address's one confirmation email for the day, or say it
    is taken. A conditional update and a keyed insert, so two workers
    racing on the same address cannot both win."""
    moment = at if at is not None else time.time()
    key = hash_token(email)
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


def _spots_taken(s, but: str) -> int:
    return (s.query(BetaRequest)
            .filter(BetaRequest.email != but,
                    BetaRequest.mobile_given_at.isnot(None),
                    BetaRequest.status.in_(_HOLDS_A_SPOT)).count())


def _wants_mail(session_scope, email: str, first_name: str,
                mobile: str | None) -> bool:
    """Is there anything to confirm? A confirmed request asked for again
    with nothing new is left alone, and nobody is emailed."""
    with session_scope() as s:
        row = s.get(BetaRequest, email)
        if row is None or row.status not in _CONFIRMED:
            return True
        return (first_name != row.first_name
                or (mobile is not None and mobile != row.mobile))


def submit(session_scope, first_name: str, email: str, mobile: str | None,
           ref: str | None) -> dict | None:
    """Record a request and return the tokens for its confirmation email,
    or None when nothing is sent (no change, or the day's email to that
    address is spent). The caller answers the same either way."""
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
            row.confirmed_at = None
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


def confirm_is_live(session_scope, token: str) -> bool:
    with session_scope() as s:
        return _by_confirm(s, token) is not None


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
                    if (row.status == "new"
                            and _spots_taken(s, row.email) >= IMESSAGE_SPOTS):
                        row.status = "waitlist"
            row.pending_first_name = row.pending_mobile = None
        else:
            kind = "join"
            row.confirmed_at = now()
            row.status = "new"
            if (row.mobile_given_at is not None
                    and _spots_taken(s, row.email) >= IMESSAGE_SPOTS):
                row.status = "waitlist"
        return {"kind": kind, "email": row.email,
                "first_name": row.first_name, "status": row.status,
                "gave_mobile": row.mobile_given_at is not None,
                "ref": row.ref}


def promote(s) -> list[str]:
    """Fill free iMessage spots from the waitlist, oldest confirmation
    first. Called wherever a spot can free up."""
    s.flush()
    promoted = []
    while _spots_taken(s, "") < IMESSAGE_SPOTS:
        nxt = (s.query(BetaRequest).filter_by(status="waitlist")
               .order_by(BetaRequest.confirmed_at, BetaRequest.created_at)
               .first())
        if nxt is None:
            break
        nxt.status = "new"
        nxt.updated_at = now()
        s.flush()
        promoted.append(nxt.email)
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
        if status != "pending" and row.confirmed_at is None:
            row.confirmed_at = now()
        row.updated_at = now()
        return promote(s) if status == "removed" else []


def list_requests(session_scope, pending: bool = False) -> list[dict]:
    """Confirmed requests (or only the pending ones), the waitlist in
    queue order."""
    with session_scope() as s:
        q = s.query(BetaRequest)
        q = (q.filter_by(status="pending") if pending
             else q.filter(BetaRequest.status != "pending"))
        rows = q.order_by(BetaRequest.created_at).all()
        out = [{"created_at": r.created_at, "first_name": r.first_name,
                "email": r.email, "mobile": r.mobile, "ref": r.ref,
                "status": r.status, "confirmed_at": r.confirmed_at}
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

def _tester_email(cfg, email: str, subject: str, lines: list[str],
                  token: str, link: tuple[str, str] | None = None) -> str:
    """One email to a tester: plain sentences, an optional action link,
    the feedback link and the single-use removal link. Every line is
    escaped; a name in one is the visitor's own, read back to them."""
    remove_url = f"{cfg.origin}/beta/remove?t={token}"
    parts = list(lines)
    if link:
        parts.append(f"{link[0]}: {link[1]}")
    text = "\n\n".join(
        parts + ["Something broke, or a question? Write to "
                 "contactus@healthclaw.io with the subject \"tester\".",
                 f"Remove my request: {remove_url}"])
    body = "".join(f"<p>{escape(x)}</p>" for x in lines)
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


def youre_in_lines(cfg, first_name: str) -> list[str]:
    return [f"Hi {first_name}, thanks for helping test CareAgents.",
            "Open careagents.cloud and sign up with this email. You'll use "
            "made-up records, not your own, and CareAgents is not a doctor.",
            "CareAgents is built by HealthClaw, so questions go to "
            "contactus@healthclaw.io."]


def text_hi_line(number: str) -> str:
    return (f"Text hi to {number}. You'll get a link back to sign in, then "
            f"your assistant answers there.")


TEXTING_COMPANY_LINE = ("Your texts pass through a texting company we use, "
                        "so only use the made-up records here.")


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

def register(app, svc, cfg) -> None:
    submits: OrderedDict[str, deque] = OrderedDict()

    def _client_key() -> str:
        # The right-hand X-Forwarded-For entry is the one Railway's edge
        # wrote; anything left of it came from the client. An internal or
        # private address there is not a client, so it shares the direct
        # peer's bucket rather than opening one of its own.
        route = request.access_route
        key = route[-1] if route else ""
        try:
            internal = any(ipaddress.ip_address(key) in n for n in _INTERNAL)
        except ValueError:
            internal = True
        return (request.remote_addr or "") if internal else key

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

    def _gone(**ctx):
        return _no_referrer(make_response(
            render_template("beta_remove.html", outcome="gone",
                            feedback=FEEDBACK_MAILTO, **ctx), 410))

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
        email = str(body.get("email") or "").strip().lower()
        if not valid_email(email):
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

    def _no_referrer(response):
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/beta/confirm")
    def beta_confirm_ask():
        """Asks, and changes nothing: mail scanners open GET links."""
        token = str(request.args.get("t") or "")
        if not confirm_is_live(svc.session, token):
            return _gone()
        return _no_referrer(make_response(render_template(
            "beta_remove.html", outcome="confirm_ask", token=token,
            feedback=FEEDBACK_MAILTO)))

    @app.post("/beta/confirm")
    def beta_confirm():
        done = confirm(svc.session, str(request.form.get("t") or ""))
        if done is None:
            return _gone()
        if done["kind"] == "join":
            try:
                lines = youre_in_lines(cfg, done["first_name"])
                if done["status"] == "waitlist":
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
        return _no_referrer(make_response(render_template(
            "beta_remove.html", outcome="confirmed", done=done,
            feedback=FEEDBACK_MAILTO)))

    @app.get("/beta/remove")
    def beta_remove_ask():
        """Asks, and changes nothing: mail scanners open GET links. The
        token is in the query string, which the access log does not keep."""
        token = str(request.args.get("t") or "")
        if not token_is_live(svc.session, token):
            return _gone()
        return _no_referrer(make_response(render_template(
            "beta_remove.html", outcome="ask", token=token,
            feedback=FEEDBACK_MAILTO)))

    @app.post("/beta/remove")
    def beta_remove():
        token = str(request.form.get("t") or "")
        if not remove(svc.session, token):
            return _gone()
        return _no_referrer(make_response(render_template(
            "beta_remove.html", outcome="removed",
            feedback=FEEDBACK_MAILTO)))

    @app.cli.group("beta-requests")
    def beta_requests():
        """Beta-tester requests from /beta."""

    @beta_requests.command("list")
    @click.option("--pending", is_flag=True,
                  help="Only requests not confirmed yet.")
    def beta_requests_list(pending):
        """Confirmed requests. The mobile is shown in full while `new` or
        `waitlist`; the waitlist shows its place in the queue."""
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
            click.echo(f"{day}  {_safe(r['first_name'])}  "
                       f"{_safe(r['email'])}  {_safe(mobile or '-')}  "
                       f"{_safe(r['ref'] or '-')}  {status}")

    @beta_requests.command("mark")
    @click.argument("email")
    @click.argument("status", type=click.Choice(STATUSES[1:]))
    def beta_requests_mark(email, status):
        """Set a request's status. `added` deletes the mobile and emails
        the tester the number to text. `removed` frees a spot, which goes
        to the oldest waitlisted request."""
        email = email.strip().lower()
        promoted = mark(svc.session, email, status)
        if promoted is None:
            raise click.ClickException("no request with that email")
        click.echo(f"{_safe(email)}: {status}")
        for p in promoted:
            click.echo(f"promoted from the waitlist: {_safe(p)}")
        if status != "added":
            return
        number = text_number(cfg)
        if not number:
            click.echo("Sendblue is off; no email sent")
            return
        token = new_removal_token(svc.session, email)
        outcome = _tester_email(
            cfg, email, "Text your assistant",
            [text_hi_line(number), TEXTING_COMPANY_LINE], token)
        click.echo(f"email: {outcome}")

    @beta_requests.command("purge")
    def beta_requests_purge():
        """Delete what the page promises to delete: unconfirmed requests
        after 7 days, mobiles after 30, requests with no account after
        60."""
        gone, cleared = purge(svc.session)
        click.echo(f"deleted {gone} request(s), {cleared} mobile(s)")


def text_tile(cfg, hub: dict) -> str:
    """The number for "Text your assistant", or "" to hide the tile: a
    sample account, while Sendblue is on."""
    if cfg.sendblue_enabled and hub["records"] and not hub["has_real"]:
        return text_number(cfg)
    return ""
