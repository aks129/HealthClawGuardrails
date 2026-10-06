"""The beta-tester sign-up: /beta, its request form, the "Remove my
request" link, and the `beta-requests` operator commands
(docs/briefs/2026-10-06-beta-onboarding.md).

What a request keeps is account data, not PHI: a first name, an email, an
optional mobile for iMessage, and a `ref`. Retention is what the page says:
the mobile goes once its status is `added`, or after MOBILE_DAYS; a request
whose email never became an account goes after REQUEST_DAYS
(`flask beta-requests purge`). `delete_account` removes the row too.

The table lives here, not in models.py, so this feature stays in one file.
accounts.py imports it, which puts it in `Base.metadata` before any engine
is built.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from collections import defaultdict, deque

import click
from flask import jsonify, make_response, render_template, request
from markupsafe import escape
from sqlalchemy import Column, Float, String

from careagents import mail
from careagents.models import Base, now

logger = logging.getLogger("careagents.beta_signup")

STATUSES = ("new", "added", "waitlist", "active", "removed")

#: The Sendblue sandbox answers ten contacts. A request with a mobile past
#: the tenth that still holds a spot goes on the waitlist.
IMESSAGE_SPOTS = 10
#: Statuses that hold an iMessage spot.
_HOLDS_A_SPOT = ("new", "added", "active")

MOBILE_DAYS = 30
REQUEST_DAYS = 60

#: Form submits per client address per window. The page is public, and a
#: submit sends an email to the address typed in it.
SUBMITS_PER_WINDOW = 5
WINDOW_SECONDS = 600

FEEDBACK_MAILTO = "mailto:contactus@healthclaw.io?subject=tester"
DEMO_VIDEOS_URL = ("https://github.com/aks129/HealthClawGuardrails/releases/"
                   "tag/demo-videos-2026-10")

_REF = re.compile(r"[a-z0-9-]{1,32}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_TOKEN_MAX = 64
_DAY = 86400


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
    status = Column(String(16), nullable=False, default="new")
    created_at = Column(Float, nullable=False, default=now)
    updated_at = Column(Float, nullable=False, default=now)
    # sha256 of the newest "Remove my request" token. Never the token.
    removal_hash = Column(String(64), nullable=True)


# --- pure rules ---------------------------------------------------------------

def clean_ref(value) -> str | None:
    """`ref` as stored: [a-z0-9-]{1,32}, or nothing. Never a refusal."""
    if isinstance(value, str) and _REF.fullmatch(value):
        return value
    return None


def clean_mobile(value: str) -> str | None:
    """A typed number as +digits, or None when it isn't one."""
    if not re.fullmatch(r"[0-9+()\-. ]+", value):
        return None
    digits = re.sub(r"\D", "", value)
    if not 7 <= len(digits) <= 15:
        return None
    return "+" + digits


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


# --- storage ------------------------------------------------------------------

def submit(session_scope, first_name: str, email: str, mobile: str | None,
           ref: str | None) -> tuple[str, str]:
    """Create or update the request. Returns (status, removal token).

    A second submit with the same email updates the row and replaces the
    removal token, so only the newest email's link works. A row the owner
    marked `removed` starts again as `new`.
    """
    token = secrets.token_urlsafe(32)
    with session_scope() as s:
        row = s.get(BetaRequest, email)
        if row is None:
            row = BetaRequest(email=email, status="new", created_at=now())
            s.add(row)
        elif row.status == "removed":
            row.status = "new"
        row.first_name = first_name
        if ref:
            row.ref = ref
        if mobile:
            if row.mobile_given_at is None and row.status == "new":
                taken = (s.query(BetaRequest)
                         .filter(BetaRequest.email != email,
                                 BetaRequest.mobile_given_at.isnot(None),
                                 BetaRequest.status.in_(_HOLDS_A_SPOT))
                         .count())
                if taken >= IMESSAGE_SPOTS:
                    row.status = "waitlist"
            row.mobile = mobile
            row.mobile_given_at = now()
        row.updated_at = now()
        row.removal_hash = hash_token(token)
        return row.status, token


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
        return True


def mark(session_scope, email: str, status: str) -> bool:
    """Set the status. `added` and `removed` delete the mobile."""
    with session_scope() as s:
        row = s.get(BetaRequest, email.strip().lower())
        if row is None:
            return False
        row.status = status
        if status in ("added", "removed"):
            row.mobile = None
        row.updated_at = now()
        return True


def list_requests(session_scope) -> list[dict]:
    with session_scope() as s:
        rows = s.query(BetaRequest).order_by(BetaRequest.created_at).all()
        return [{"created_at": r.created_at, "first_name": r.first_name,
                 "email": r.email, "mobile": r.mobile, "ref": r.ref,
                 "status": r.status} for r in rows]


def purge(session_scope, at: float | None = None) -> tuple[int, int]:
    """Apply the retention the page states. Returns (requests deleted,
    mobiles deleted)."""
    from careagents.models import Account
    moment = at if at is not None else time.time()
    with session_scope() as s:
        accounts = {e for (e,) in s.query(Account.email)}
        gone = 0
        for row in (s.query(BetaRequest)
                    .filter(BetaRequest.created_at
                            < moment - REQUEST_DAYS * _DAY)):
            if row.email not in accounts:
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
        return gone, cleared


# --- email --------------------------------------------------------------------

def _tester_email(cfg, email: str, subject: str, lines: list[str],
                  token: str) -> str:
    """One email to a tester: plain sentences, the feedback link, and the
    single-use removal link. `lines` are our own words; nothing a visitor
    typed is put in them."""
    remove_url = f"{cfg.origin}/beta/remove?t={token}"
    text = "\n\n".join(
        lines + ["Something broke, or a question? Write to "
                 "contactus@healthclaw.io with the subject \"tester\".",
                 f"Remove my request: {remove_url}"])
    body = "".join(f"<p>{escape(x)}</p>" for x in lines)
    html = (f"<div style='font-family:system-ui,sans-serif;max-width:420px'>"
            f"<h2 style='color:#22190E'>CareAgents</h2>{body}"
            f"<p>Something broke, or a question? "
            f"<a href='{FEEDBACK_MAILTO}'>Tell us</a>.</p>"
            f"<p style='color:#5E5240;font-size:13px'>"
            f"<a href='{escape(remove_url)}'>Remove my request</a></p></div>")
    return mail.send_message(cfg, email, subject, html, text)


def _notify_owner(cfg, email: str, gave_mobile: bool, ref: str | None,
                  status: str) -> None:
    if not cfg.beta_notify_email:
        return
    line = (f"New beta request. Email domain: {mask_domain(email)}; "
            f"mobile: {'yes' if gave_mobile else 'no'}; "
            f"ref: {ref or '-'}; status: {status}. "
            f"Details: flask beta-requests list")
    mail.send_notice(cfg, cfg.beta_notify_email, "New beta request",
                     str(escape(line)))


# --- routes and commands ------------------------------------------------------

def register(app, svc, cfg) -> None:
    submits: dict[str, deque] = defaultdict(deque)

    def _client_key() -> str:
        # The right-hand X-Forwarded-For entry is the one our proxy added;
        # anything left of it came from the client and can be made up.
        route = request.access_route
        return route[-1] if route else (request.remote_addr or "")

    def _allow_submit(key: str) -> bool:
        window = submits[key]
        moment = time.time()
        while window and moment - window[0] > WINDOW_SECONDS:
            window.popleft()
        if len(window) >= SUBMITS_PER_WINDOW:
            return False
        window.append(moment)
        return True

    @app.get("/beta")
    def beta_page():
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
        if not 0 < len(first_name) <= 40:
            return jsonify({"error": "first_name"}), 400
        email = str(body.get("email") or "").strip().lower()
        if len(email) > 255 or not _EMAIL.fullmatch(email):
            return jsonify({"error": "email"}), 400
        mobile = None
        raw_mobile = str(body.get("mobile") or "").strip()
        if raw_mobile:
            mobile = clean_mobile(raw_mobile)
            if mobile is None:
                return jsonify({"error": "mobile"}), 400
        ref = clean_ref(body.get("ref"))
        status, token = submit(svc.session, first_name, email, mobile, ref)
        lines = ["You're in. Open careagents.cloud and sign up with this "
                 "email."]
        if status == "waitlist":
            lines.append("iMessage is full for now. We'll email you when a "
                         "spot opens. The web app works today.")
        try:
            _tester_email(cfg, email, "You're in the beta", lines, token)
            _notify_owner(cfg, email, mobile is not None, ref, status)
        except Exception:                   # pragma: no cover - defensive
            # The request is saved; the owner reads it in the CLI.
            logger.warning("beta request email failed to send")
        return jsonify({"ok": True})

    def _no_referrer(response):
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/beta/remove")
    def beta_remove_ask():
        """Asks, and changes nothing: mail scanners open GET links. The
        token is in the query string, which the access log does not keep."""
        token = str(request.args.get("t") or "")
        if not token_is_live(svc.session, token):
            return _no_referrer(make_response(
                render_template("beta_remove.html", outcome="gone",
                                feedback=FEEDBACK_MAILTO), 410))
        return _no_referrer(make_response(render_template(
            "beta_remove.html", outcome="ask", token=token,
            feedback=FEEDBACK_MAILTO)))

    @app.post("/beta/remove")
    def beta_remove():
        token = str(request.form.get("t") or "")
        if not remove(svc.session, token):
            return _no_referrer(make_response(
                render_template("beta_remove.html", outcome="gone",
                                feedback=FEEDBACK_MAILTO), 410))
        return _no_referrer(make_response(render_template(
            "beta_remove.html", outcome="removed",
            feedback=FEEDBACK_MAILTO)))

    @app.cli.group("beta-requests")
    def beta_requests():
        """Beta-tester requests from /beta."""

    @beta_requests.command("list")
    def beta_requests_list():
        """Every request. The mobile is shown in full only while `new`."""
        rows = list_requests(svc.session)
        if not rows:
            click.echo("no requests")
            return
        click.echo("date        first name  email  mobile  ref  status")
        for r in rows:
            day = time.strftime("%Y-%m-%d", time.gmtime(r["created_at"]))
            mobile = (r["mobile"] if r["status"] == "new" and r["mobile"]
                      else _mask_mobile(r["mobile"]))
            click.echo(f"{day}  {r['first_name']}  {r['email']}  {mobile}  "
                       f"{r['ref'] or '-'}  {r['status']}")

    @beta_requests.command("mark")
    @click.argument("email")
    @click.argument("status", type=click.Choice(STATUSES[1:]))
    def beta_requests_mark(email, status):
        """Set a request's status. `added` deletes the mobile and emails
        the tester the number to text."""
        email = email.strip().lower()
        if not mark(svc.session, email, status):
            raise click.ClickException("no request with that email")
        click.echo(f"{email}: {status}")
        if status != "added":
            return
        # TODO(#868): the Sendblue line's number replaces this handle.
        if not cfg.imessage_handle:
            click.echo("no iMessage number set (CARE_IMESSAGE_HANDLE); "
                       "no email sent")
            return
        token = new_removal_token(svc.session, email)
        outcome = _tester_email(
            cfg, email, "Text your assistant",
            [f"Text hi to {cfg.imessage_handle}. Your assistant answers "
             f"there.",
             "Texts go through an outside messaging service. Use it with "
             "sample records only."], token)
        click.echo(f"email: {outcome}")

    @beta_requests.command("purge")
    def beta_requests_purge():
        """Delete what the page promises to delete: mobiles after 30 days,
        requests with no account after 60."""
        gone, cleared = purge(svc.session)
        click.echo(f"deleted {gone} request(s), {cleared} mobile(s)")


def text_tile(cfg, hub: dict) -> bool:
    """Show "Text your assistant": a sample account, while iMessage is on.

    TODO(#868): gate on cfg.sendblue_enabled once it is on main.
    """
    return bool(cfg.imessage_handle and hub["records"]
                and not hub["has_real"])
