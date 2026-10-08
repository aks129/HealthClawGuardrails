"""CareAgents Flask app — accounts, biometric auth, the health hub, and chat.

Identity model: a signed cookie holds `account_id` after passkey/email login.
Everything (connections, agents, surfaces) is account-scoped; a foreign id
reads as 404.

Chat history and run state are DURABLE in HealthClaw (#222/#247/#248). Web
requests enqueue work and replay its event log; inference and tools run only in
the dedicated ``careagents.worker`` process.

No PHI is stored here — health data lives in HealthClaw tenants behind the
guardrail layer; careagents holds identity + pointers only.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote

import click
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for)
from markupsafe import Markup, escape

from careagents.accounts import (AccountService, AuthError, MailError,
                                 MailUnconfirmed, new_binding_code,
                                 secret_matches)
from careagents import advisors, analytics, connectors, consent, mail
from careagents import beta, imessage, operator_cli, tester_terms
from careagents import sendblue_surface
from careagents import brief as brief_mod
from careagents import hub as hub_view
from careagents import intake_state
from careagents import labs_timeline as labs_timeline_mod
from careagents import beta_signup, feedback
from careagents.agent import GENERIC_FAILURE_TEXT
from careagents.config import Config
from careagents.healthclaw import (HealthClawClient, HealthClawError,
                                   HealthClawUnconfirmed)
from careagents.personas import DEFAULT_PERSONA, PERSONAS

logger = logging.getLogger(__name__)

# `/healthz` asks HealthClaw whether a run worker is present. That call gets
# its own budget rather than the client's 25s chat timeout: as a
# `(connect, read)` pair the worst case is 2.0s of network wait, which fits
# inside the 4s the container probe allows (`careagents/healthcheck.py`) and
# the Dockerfile's `HEALTHCHECK --timeout=5s`. The probe measured the old
# shape taking 25.01s to answer at zero load, purely because it inherited the
# chat timeout (docs/evidence/2026-09-03-probe-219-thread-saturation.md §8,
# PR #573).
_HEALTHZ_WORKER_TIMEOUT = (1.0, 1.0)

#: The one address patient-facing copy sends people to.
CONTACT_EMAIL = "contactus@healthclaw.io"


def contact_links(text) -> Markup:
    """Jinja filter: escape `text`, then make the contact address a mailto
    link. Only that fixed address is linked, so nothing in `text` becomes
    markup."""
    return Markup(str(escape(text)).replace(
        CONTACT_EMAIL,
        f'<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>'))


def chat_links(text) -> Markup:
    """Jinja filter for a replayed assistant bubble: contact_links, and
    "your hub" links to the hub, the same two links chat.js adds live."""
    return Markup(str(contact_links(text)).replace(
        "your hub", '<a href="/home">your hub</a>'))

# The same call on the ADMISSION path (`POST /api/chat`, and the iMessage
# relay ingress). It gets its own budget rather than sharing the readiness
# probe's, because the stakes are opposite: `/healthz` must answer inside a
# platform probe window and a wrong answer costs a deploy, while a turn
# refused too eagerly costs a patient their question. A worker-health call
# does two database round-trips on the engine that also serves clinicians, so
# 1s of read is too tight to refuse a real user on.
#
# Left unbounded, this inherits the client's 25s CHAT timeout — measured at
# 25.00s against a HealthClaw that accepts the connection and never answers.
# That is a gunicorn thread held for 25 seconds *to refuse a turn*, in exactly
# the dependency outage `/healthz` now reports as `unknown` and lets deploy,
# and it eats the thread headroom this same change bought. 4s is a ceiling
# chosen to end the 25s hold while leaving room for a loaded engine; it is not
# a measured optimum, and a p99 from production should replace it.
_ADMISSION_WORKER_TIMEOUT = (1.0, 3.0)

# Local hard cap for the file-upload path (#227). The engine's
# `internal/ingest-bundle` is the source of truth for the value; this
# ceiling stops an oversized (or chunked / no-Content-Length) request
# from ever spending more than max_bytes+1 in our process before we
# refuse it. 5 MiB matches the engine default so a request either fails
# here quickly or lands cleanly.
_UPLOAD_MAX_BYTES = 5 * 1024 * 1024

# FHIR R4 §3.2 SHALL support `application/fhir+json`; the two legacy
# variants ride the same accept-list so a real FHIR client, a plain-JSON
# hand-crafted body, and older exporters all land.
_UPLOAD_MIME_TYPES = frozenset({
    "application/fhir+json",
    "application/json",
    "application/json+fhir",
})

# The brief is read in careagents/brief.py, shared with the agent's
# appointment_brief tool; these names are kept for this module's callers.
_BRIEF_SECTION_PREFIX = brief_mod.SECTION_PREFIX
_parse_brief_sections = brief_mod.parse_sections
_care_gaps_marker = brief_mod.care_gaps_marker
_CARE_GAPS_OK = brief_mod.CARE_GAPS_OK


#: What a pending request is called on the approvals page, by engine kind.
_KIND_LABELS = {
    "phone-call": "Phone call", "sms": "Text message",
    "insurance-call": "Call to your insurer", "form-fill": "Intake form",
    "webhook-poster": "Message to a connected service",
    "curatr-fix": "Correction to your health record",
}


def _engine_said_absent(exc: HealthClawError) -> bool:
    """Whether the engine answered "there is no such thing".

    `HealthClawError.status` is 0 for a transport failure and carries the
    engine's status otherwise. Only 404 is an answer about existence: the
    engine's action and run lookups are tenant-scoped
    `filter_by(id=..., tenant_id=...).first()` and answer 404 for both "no
    such id" and "not this tenant's" (r6/actions/routes.py:652-655). A
    refused connection, a timeout, a 5xx, or a rejected credential all mean
    we could not ask — and reporting any of those as "unknown run" or "not
    yours" states something we do not know (#410).
    """
    return exc.status == 404


class OwnershipUnknown(Exception):
    """We could not determine whether an action belongs to this agent.

    Deliberately an exception rather than a third return value. Both callers
    of `_agent_owns_action` guard with `if not tenant`, and any sentinel that
    guard could see would be truthy — silently upgrading "we could not check"
    into "checked, and it's yours". That is the same shape as truthiness-
    testing the `validate_step_up_token` tuple, which is a standing
    non-negotiable in this repo. An exception cannot be mis-read that way.
    """


#: The engine review page's tab title ends "— HealthClaw Guardrails".
_REVIEW_TAB_BRAND = re.compile(
    r"(<title>[^<]*?)HealthClaw Guardrails(\s*</title>)")


def _connection_is_live(ctx: dict) -> bool:
    """Whether an agent context's connection may still reach the tenant's
    requests: not revoked (#215). The one rule for every approval surface,
    the web pages and APPROVALS by text alike."""
    return (ctx.get("connection") or {}).get("status") != "revoked"


def _parse_care_gaps_status(resource: dict | None) -> str:
    """Whether the screening review ran, from the brief's care-gaps section.

    Anything short of an explicit "ok" — no brief, no care-gaps section, no
    marker, an unparseable payload — is not an evaluation, and the page must
    not render it as "nothing due" (#381). Callers get "" for all of those.
    """
    return _care_gaps_marker(resource, "status")


def _parse_care_gaps_reason(resource: dict | None) -> str:
    """The engine's own sentence for why the review is not whole — which
    screenings could not be checked and why. Rendered as-is, never stored:
    it is rule titles and fixed prose from r6/caregaps/report.py."""
    return _care_gaps_marker(resource, "reason")


def _uncounted_note(new_records: int, new_documents: int | None,
                    uncounted: int | None) -> str | None:
    """The one sentence about documents a count leaves out (#226).

    Shared by the refresh poll and the upload card, so the two counters on
    the same page cannot say different things again. Both pass the same
    meanings: `new_records` and `new_documents` are what this sync or upload
    added (None: arrival unknown), `uncounted` is the tenant's document total
    afterwards (None: the probe failed).
    """
    if uncounted is None:
        return "We could not check whether notes or documents were left out."
    if new_records > 0 and uncounted > 0:
        # A standing caveat on a number the patient can act on: what you
        # can read grew, and this excludes notes.
        return "Notes and documents are not yet readable here."
    if new_records == 0 and new_documents:
        # Documents arrived and nothing readable did. Silence here is
        # indistinguishable from a sync that did nothing, so say what
        # happened. The poll gates this on a delta, never on `uncounted > 0`
        # — that is true from the first tick for every tenant that already
        # holds notes, and would fire on every no-op refresh.
        return ("Notes and documents arrived, and they are not readable "
                "here yet.")
    return None


def _documents_landed(bundle: dict, result: dict, landed: int) -> int:
    """How many of an upload's ingested entries are uncounted documents.

    The engine reports one `ingested` total, and an `errors[]` row with the
    `index` of every entry it did not store. Entries without an error row
    landed; of those, count the types `record_count` leaves out. Only the
    resourceType string is read — nothing from the record is kept.
    """
    refused = {e.get("index") for e in (result.get("errors") or [])
               if isinstance(e, dict)}
    n = 0
    for idx, entry in enumerate(bundle.get("entry") or []):
        if idx in refused or not isinstance(entry, dict):
            continue
        res = entry.get("resource")
        if (isinstance(res, dict) and res.get("resourceType")
                in HealthClawClient.UNCOUNTED_TYPES):
            n += 1
    return min(n, landed)


def create_app(config: Config | None = None,
               client: HealthClawClient | None = None,
               accounts: AccountService | None = None) -> Flask:
    cfg = config or Config()
    app = Flask(__name__)
    app.secret_key = cfg.session_secret
    # Every static URL names the build, so a deploy is a new URL and a
    # browser cannot keep running the old home.js against the new server
    # (#909). The deploy stamp when there is one, else this process's start.
    asset_version = (cfg.build_sha if cfg.build_sha != "unknown"
                     else str(int(time.time())))

    @app.url_defaults
    def _versioned_static(endpoint, values):
        if endpoint == "static":
            values.setdefault("v", asset_version)

    app.jinja_env.filters["contact_links"] = contact_links
    app.jinja_env.filters["chat_links"] = chat_links
    app.config.update(SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=(cfg.app_env == "production"),
                      PERMANENT_SESSION_LIFETIME=90 * 24 * 3600)
    hc = client or HealthClawClient(cfg.healthclaw_base, cfg.mint_secret,
                                    public_base=cfg.healthclaw_public_base)
    svc = accounts or AccountService(cfg)
    # Exposed for deterministic integration tests and process diagnostics.
    # Production Gunicorn never executes this worker object; the systemd/OCI
    # worker service constructs its own clients and calls careagents.worker.
    app.extensions["careagents_runtime"] = {
        "config": cfg, "client": hc, "accounts": svc}

    turns: dict[str, deque] = defaultdict(deque)

    def _real_records_open(acct) -> bool:
        """May this account START a real-record connection? The one gate.

        Order matters. `off` closes everything, before the table is read
        (never a back door around `off`). A paused account is closed in
        every mode (beta spec 4.6). Then the config rule: `on` is open,
        `allowlist` is the environment list or an active invite, and the
        invite table is asked only once the tester terms are approved (#565).
        """
        if cfg.real_records == "off":
            return False
        if svc.is_paused(acct.id):
            return False
        return cfg.real_records_open_for(
            acct.email,
            invited=svc.real_records_invited if tester_terms.approved()
            else None)

    # --- canonical host (#264, D7) -------------------------------------------

    @app.before_request
    def _enforce_canonical_host():
        # careagents.cloud is the sole origin. A request arriving under any
        # other Host header (the platform's own *.up.railway.app name) is
        # sent to the same path and query on the canonical host — 308, so a
        # POST is replayed as a POST rather than downgraded to a GET. The
        # target is built from config, never from the request, so a spoofed
        # Host cannot steer it. `/healthz` is exempt: the platform's health
        # check arrives on the internal hostname, and a 308 there would mark
        # every deploy unhealthy. Unset means no redirect (local, CI).
        if not cfg.canonical_host or request.path == "/healthz":
            return None
        # Hostname only. A proxy that appends the default port
        # (`careagents.cloud:443`) is on the canonical host and must not be
        # bounced — the Werkzeug test client normalises that port away, a
        # real request does not, so the strip has to be explicit or every
        # such request pays a redirect. Config forbids a port in
        # canonical_host, so the only `:` here is the request's own.
        host = request.host.lower()
        if ":" in host and not host.endswith("]"):   # not a bare IPv6 literal
            host = host.rsplit(":", 1)[0]
        if host == cfg.canonical_host:
            return None
        # `request.path` arrives percent-DECODED, so it can carry bytes that
        # are illegal in a header — `/%0d%0a...` produced a 500 rather than a
        # redirect. Re-encode it; `safe` is RFC 3986's pchar set, so an
        # ordinary path is unchanged byte for byte.
        target = f"https://{cfg.canonical_host}" + quote(
            request.path, safe="/:@-._~!$&'()*+,;=")
        if request.query_string:
            target += "?" + request.query_string.decode("utf-8", "replace")
        return redirect(target, code=308)

    # --- page-view counting (careagents/analytics.py) ------------------------

    #: Pages on the texted-link path. No Referer leaves them: /link carries a
    #: live token in its query string, and the two pages after it are where
    #: a person on that path clicks out. Scoped, not app-wide: an outbound
    #: connector flow may rely on the default policy.
    _NO_REFERRER_ENDPOINTS = frozenset(
        {"imessage_link", "imessage_link_claim", "auth"})

    @app.after_request
    def _no_referrer_on_the_link_path(response):
        if request.endpoint in _NO_REFERRER_ENDPOINTS:
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.after_request
    def _count_a_public_page_view(response):
        """One integer per day per public page, when the flag says so.

        Deliberately narrow: only a page anyone can open, only a GET that
        actually rendered, and the endpoint name rather than the URL, so
        nothing a request supplies reaches the table. `record_view` refuses
        any endpoint outside its own list, so a later hook in the wrong place
        counts nothing rather than counting a signed-in person's pages.
        """
        if not cfg.analytics_enabled:
            return response
        if request.method != "GET" or response.status_code != 200:
            return response
        try:
            analytics.record_view(svc.session, request.endpoint or "")
        except Exception:                       # pragma: no cover - defensive
            # A counter is never a reason a page fails to render.
            logger.warning("page-view counting failed", exc_info=True)
        return response

    # --- auth plumbing -------------------------------------------------------

    def current_account():
        aid = session.get("account_id")
        return svc.get_account(aid) if aid else None

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            # Resolve the account, not just its id. A session outlives the row
            # it points at whenever someone uses the self-serve delete (#203):
            # the cookie stays in the browser, Back or an older tab replays it,
            # and every handler behind this gate dereferences
            # current_account(). Checking the id alone turned that into a 500
            # at the moment the person is trying to confirm their records are
            # gone (#265). A vanished account is an ended session, and this is
            # the one place all protected routes pass through.
            if current_account() is None:
                if session.get("account_id"):
                    # A texted link parked before this session went stale
                    # survives, so the sign-in that follows still binds it.
                    imessage_link = session.get("imessage_link")
                    session.clear()
                    if isinstance(imessage_link, str):
                        session["imessage_link"] = imessage_link
                if request.path.startswith("/api/") or request.path.startswith(
                        "/webauthn/"):
                    return jsonify({"error": "sign in"}), 401
                return redirect(url_for("auth"))
            return fn(*a, **k)
        return wrapper

    def _login(account):
        # Cleared against session fixation. The one value carried across is
        # a consent handle parked by /authorize before sign-in, and only if it
        # still verifies: it is signed by HealthClaw and names no account,
        # and without it the person lands on the hub, not the request (#846).
        consent_req = session.get("consent_req")
        imessage_link = session.get("imessage_link")
        session.clear()
        session.permanent = True
        session["account_id"] = account.id
        if (isinstance(consent_req, str)
                and consent.parse_handle(consent_req, cfg.mint_secret)):
            session["consent_req"] = consent_req
        # A texted sign-in link parked by /link/<token> (careagents.imessage):
        # an id, not the token, and spent only after this sign-in.
        if isinstance(imessage_link, str):
            session["imessage_link"] = imessage_link

    # --- pages ---------------------------------------------------------------

    @app.get("/")
    def landing():
        return render_template("landing.html", me=current_account())

    @app.get("/auth")
    def auth():
        # `?enroll=1` lets someone already signed in add a passkey. Without it
        # the only enrolment moment was the single screen after first email
        # verification: skip once and the "sign in with your face" promise
        # quietly expired into email codes forever, because this route sent
        # every logged-in visitor straight back to /home (#223).
        enroll = request.args.get("enroll") == "1"
        if session.get("account_id") and not enroll:
            return redirect(url_for("home"))
        return render_template("auth.html", rp_id=cfg.rp_id, enroll=enroll,
                               imessage_pending=bool(
                                   session.get("imessage_link")),
                               terms_url=f"{cfg.healthclaw_public_base}/terms",
                               privacy_url=f"{cfg.healthclaw_public_base}/privacy")

    @app.get("/home")
    @login_required
    def home():
        # A consent request that was waiting on sign-in resumes here, once.
        pending_req = session.pop("consent_req", None)
        if pending_req:
            return redirect(url_for("consent_authorize", req=pending_req))
        # So does a texted sign-in link: bind the handle it came from.
        if session.get("imessage_link"):
            return redirect(url_for("imessage_link_claim"))
        acct = current_account()
        data = svc.list_home(acct.id)
        view = hub_view.build(data, time.time())
        real_open = _real_records_open(acct)
        # Real connections whose consent predates the current terms (beta
        # spec 4.3). The worker refuses their turns until this is answered.
        # One prompt for the account, however many connections it covers.
        stale_consent = svc.stale_consent(data["connections"],
                                          tester_terms.CONSENT_VERSION)
        paused = svc.is_paused(acct.id)
        # Invited, but the invite waits on the tester terms (#565): say so
        # rather than "coming for invited testers" to someone who is one.
        invited = (cfg.real_records == "allowlist"
                   and svc.real_records_invited(acct.email))
        return render_template(
            "home.html", me=acct,
            stale_consent=stale_consent,
            paused=paused, paused_line=beta.PAUSED_HUB_TEXT,
            invited=invited,
            hub=view, text_tile=beta_signup.text_tile(
                cfg, view, svc.session, acct.email),
            # "Connected" only once a real connection is active; has_real
            # also counts one still connecting. "Made-up", as on /beta.
            banner_records=("your records are connected"
                            if any(not r["is_sample"]
                                   for r in view["active_records"])
                            else "made-up records"),
            switch_prompt=(None if svc.switch_prompted_at(acct.id)
                           else hub_view.switch_prompt(data)),
            has_grants=bool(svc.list_grants(acct.id)),
            menu_open=real_open, groups=connectors.GROUPS,
            terms_url=f"{cfg.healthclaw_public_base}/terms",
            privacy_url=f"{cfg.healthclaw_public_base}/privacy",
            tester_terms_approved=tester_terms.approved(),
            consent_version=tester_terms.CONSENT_VERSION,
            terms_change=tester_terms.CHANGE_SUMMARY,
            menu=hub_view.menu_items(
                connectors.catalog(cfg, real_records=real_open), real_open))

    @app.get("/settings")
    @login_required
    def settings():
        acct = current_account()
        data = svc.list_home(acct.id)
        return render_template(
            "settings.html", me=acct,
            passkeys=svc.list_passkeys(acct.id),
            grants=_grants_with_labels(svc.list_grants(acct.id),
                                       data["connections"]),
            first_agent=(data["agents"][0]["id"] if data["agents"] else ""),
            imessage_handle=cfg.imessage_handle,
            # Every connected phone, masked, each with its own Disconnect.
            imessage_connected=[
                {"id": x["id"], "label": imessage.masked_display(x["handle"])}
                for x in data["surfaces"]
                if x["kind"] == "imessage" and x["status"] == "active"])

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("landing"))

    # --- email code auth -----------------------------------------------------

    @app.post("/api/auth/email")
    def auth_email():
        email = (request.get_json(silent=True) or {}).get("email", "")
        purpose = "verify"
        try:
            retry_after = svc.start_email_code(email, purpose)
        except AuthError as exc:
            return jsonify({"error": str(exc)}), 400
        except MailUnconfirmed as exc:
            # The third answer (#220). `sent` is null, not false: saying "we
            # didn't send it" would be as unobserved as saying we did, and the
            # code is still live, so the person carries on to the code step
            # rather than being sent back to the start. 202 — accepted, outcome
            # unknown. Ordered before MailError: MailUnconfirmed subclasses it.
            return jsonify({"sent": None, "notice": str(exc)}), 202
        except MailError as exc:
            # Never report "sent" when nothing was sent — this is the front
            # door, and a silent failure leaves the person watching an empty
            # inbox with no idea whether to wait or retry.
            return jsonify({"error": str(exc), "sent": False}), 502
        if retry_after:
            # The cooldown suppressed the send (#262). 200, not 4xx: the
            # request was handled correctly and the person still holds a live
            # code, so the UI still advances to code entry. But claiming
            # "sent" here is the lie — it strands whoever's first email never
            # arrived, telling them to wait for something nobody sent.
            #
            # Not an enumeration oracle: this branch turns only on whether a
            # code was requested for this address moments ago — a state the
            # requester just created themselves — never on whether an account
            # exists. start_email_code touches ca_email_tokens alone; accounts
            # are created at verify. The genuine-send and cooldown responses
            # are byte-identical for an address with an account and one
            # without.
            return jsonify({"sent": False, "reason": "cooldown",
                            "retry_after": retry_after})
        return jsonify({"sent": True})

    @app.post("/api/auth/verify")
    def auth_verify():
        body = request.get_json(silent=True) or {}
        try:
            acct = svc.verify_email_code(body.get("email", ""),
                                         body.get("code", ""))
        except AuthError as exc:
            return jsonify({"error": str(exc)}), 400
        _login(acct)
        return jsonify({"ok": True, "has_passkey": svc.has_passkey(acct.id)})

    # --- WebAuthn (biometric) ------------------------------------------------

    @app.post("/webauthn/register/options")
    @login_required
    def wa_register_options():
        acct = current_account()
        options, challenge = svc.registration_options(acct)
        session["wa_challenge"] = challenge
        return jsonify(options)

    @app.post("/webauthn/register/verify")
    @login_required
    def wa_register_verify():
        acct = current_account()
        challenge = session.pop("wa_challenge", None)
        if not challenge:
            return jsonify({"error": "no challenge"}), 400
        try:
            svc.finish_registration(
                acct.id, request.get_json(force=True), challenge,
                name=(request.args.get("name") or "This device"))
        except Exception:  # noqa: BLE001 — WebAuthn lib raises broadly
            return jsonify({"error": "passkey registration failed"}), 400
        return jsonify({"ok": True})

    @app.post("/webauthn/login/options")
    def wa_login_options():
        options, challenge = svc.authentication_options()
        session["wa_challenge"] = challenge
        return jsonify(options)

    @app.post("/webauthn/login/verify")
    def wa_login_verify():
        challenge = session.pop("wa_challenge", None)
        if not challenge:
            return jsonify({"error": "no challenge"}), 400
        try:
            acct = svc.finish_authentication(request.get_json(force=True),
                                             challenge)
        except AuthError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:  # noqa: BLE001
            return jsonify({"error": "passkey sign-in failed"}), 400
        _login(acct)
        return jsonify({"ok": True})

    # --- consent: a third-party agent asks to read one connection -----------
    #
    # HealthClaw parks the OAuth request and sends the browser here with a
    # signed handle (spec §13.3). Nothing is fetched until the handle
    # verifies; nothing is granted until a fresh, user-verified passkey says
    # this person is present; and the decision goes back signed, single-use
    # and short-lived. What is offered: the account's own connections, real
    # ones only where CARE_REAL_RECORDS opens them (§13.7).

    def _grants_with_labels(grants, connections):
        labels = {c["id"]: c["label"] for c in connections}
        return [{**g, "tenant_label": labels.get(g["connection_id"])} for g in grants]

    def _consent_return(grant):
        return (f"{cfg.healthclaw_public_base}/r6/fhir/oauth/consent/return"
                f"?grant={grant}")

    def _offered_connections(acct):
        real_open = _real_records_open(acct)
        conns = svc.list_home(acct.id)["connections"]
        return [c for c in conns
                if c["status"] != "revoked"
                and (c["kind"] == "sample" or real_open)], real_open

    @app.get("/authorize")
    def consent_authorize():
        req = (request.args.get("req") or "").strip()
        request_id = consent.parse_handle(req, cfg.mint_secret)
        if request_id is None:
            return render_template("consent.html", state="invalid",
                                   me=current_account()), 400
        if current_account() is None:
            session["consent_req"] = req
            return redirect(url_for("auth"))
        try:
            parked = hc.consent_request(request_id)
        except HealthClawError:
            # HealthClaw unreachable: say so plainly, not a 500. The handle is
            # still good for ten minutes, so trying again can work.
            return render_template("consent.html", state="unavailable",
                                   me=current_account()), 503
        if parked is None:
            return render_template("consent.html", state="expired",
                                   me=current_account()), 410
        acct = current_account()
        offered, real_open = _offered_connections(acct)
        return render_template(
            "consent.html", state="ask", req=req, me=acct,
            **consent.app_identity(parked),
            scopes=[consent.describe_scope(x) for x in parked.get("scopes", [])],
            connections=offered, real_open=real_open,
            has_passkey=svc.has_passkey(acct.id),
            privacy_url=f"{cfg.healthclaw_public_base}/privacy")

    @app.post("/webauthn/consent/options")
    @login_required
    def wa_consent_options():
        options, challenge = svc.authentication_options(require_uv=True)
        session["wa_consent_challenge"] = challenge
        return jsonify(options)

    @app.post("/authorize/decide")
    @login_required
    def consent_decide():
        body = request.get_json(force=True, silent=True) or {}
        request_id = consent.parse_handle(body.get("req") or "", cfg.mint_secret)
        if request_id is None:
            return jsonify({"error": "This request is not valid any more."}), 400
        if body.get("decision") != "approved":
            # Only a recognized address gets the OAuth access_denied, which
            # sends the browser to the client. Any other stays here: its
            # redirect URI is a stranger's page. The parked request is spent
            # at HealthClaw with no redirect, so an older tab or another
            # account cannot approve it afterwards (#846). HealthClaw
            # unreachable: nothing was shared, so say no here too; the
            # request then lapses at its ten-minute expiry.
            try:
                parked = hc.consent_request(request_id)
            except HealthClawError:
                parked = None
            if not consent.app_identity(parked or {})["host_recognized"]:
                try:
                    hc.discard_consent_request(request_id)
                except HealthClawError:
                    logger.warning("consent discard failed; request lapses at expiry")
                return jsonify({"redirect": url_for("consent_declined")})
            grant, _ = consent.build_grant(cfg.mint_secret, request_id, "denied")
            return jsonify({"redirect": _consent_return(grant)})

        acct = current_account()
        conn = svc.get_connection(acct.id, str(body.get("connection_id") or ""))
        if conn is None or conn["status"] == "revoked":
            return jsonify({"error": "unknown connection"}), 404
        if conn["kind"] != "sample" and not _real_records_open(acct):
            return jsonify({"error": "Sharing real records is not open for "
                                     "this account yet."}), 403

        # The person, not the session: a fresh assertion with user
        # verification, for this account.
        challenge = session.pop("wa_consent_challenge", None)
        if not challenge:
            return jsonify({"error": "no challenge"}), 400
        try:
            verified = svc.finish_authentication(
                body.get("passkey") or {}, challenge, require_uv=True)
        except AuthError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:  # noqa: BLE001
            return jsonify({"error": "passkey check failed"}), 400
        if verified.id != acct.id:
            return jsonify({"error": "That passkey belongs to another account."}), 403

        parked = hc.consent_request(request_id)
        if parked is None:
            return jsonify({"error": "This request has expired. Start again "
                                     "from the app that asked."}), 410
        grant, consent_id = consent.build_grant(
            cfg.mint_secret, request_id, "approved", tenant_id=conn["tenant_id"])
        svc.add_grant(acct.id, conn["id"], conn["tenant_id"],
                      parked.get("client_id", ""), parked.get("client_name", ""),
                      " ".join(parked.get("scopes", [])), consent_id,
                      redirect_host=consent.app_identity(parked)["redirect_host"])
        return jsonify({"redirect": _consent_return(grant)})

    @app.get("/authorize/declined")
    def consent_declined():
        return render_template("consent.html", state="declined",
                               me=current_account())

    @app.post("/api/grants/<grant_id>/revoke")
    @login_required
    def revoke_grant(grant_id):
        """Take a consent back. HealthClaw first, the row here second, so a
        revocation is never shown that did not happen."""
        acct = current_account()
        grant = svc.get_grant(acct.id, grant_id)
        if grant is None:
            return jsonify({"error": "unknown grant"}), 404
        try:
            hc.revoke_consent(grant["consent_id"])
        except HealthClawError:
            return jsonify({"error": "revocation_failed", "revoked": False,
                            "message": "We couldn't confirm the access was "
                                       "revoked. It is still shown here — "
                                       "please try again."}), 502
        svc.mark_grant_revoked(acct.id, grant_id)
        return jsonify({"revoked": True, "grant_id": grant_id})

    # --- connections ---------------------------------------------------------

    @app.get("/api/connections/catalog")
    @login_required
    def connections_catalog():
        acct = current_account()
        return jsonify({"connectors": connectors.catalog(
            cfg, real_records=_real_records_open(acct))})

    def _sample_answer(account_id, conn_id, existing):
        """What a sample tap answers: the connection, and the chat to open."""
        out = {"id": conn_id, "status": "active", "existing": existing}
        agent = svc.agent_for_connection(account_id, conn_id)
        if agent:
            out["agent_id"] = agent["id"]
            out["redirect"] = url_for("chat", agent=agent["id"])
        return out

    def _consent_refusal(body):
        """The 428 for a consent that does not name the current terms, or
        None. The client echoes the version on the card it showed; a card
        rendered before a terms change and submitted after it is not
        acceptance of wording the person never saw (security review of
        #904, F4). The answer carries the current version so the client
        can show the current card, never so it can resend that version."""
        if (body.get("consent") is True
                and body.get("consent_version")
                == tester_terms.CONSENT_VERSION):
            return None
        if body.get("consent") is True and not body.get("consent_version"):
            # A page loaded before versions were sent (#909) shows `error`
            # as it is: give it a sentence, not a code.
            reload_line = "This page changed. Please reload."
            return jsonify({"error": reload_line, "message": reload_line,
                            "consent_version": tester_terms.CONSENT_VERSION}), 428
        return jsonify({"error": "consent_required",
                        "consent_version": tester_terms.CONSENT_VERSION}), 428

    def _start_connection(connector_id, acct, body):
        # New connections only (D3): refresh, poll, upload and delete on an
        # existing connection never consult the real-records switch.
        plan = connectors.start(
            connector_id, body.get("provider"), cfg, hc,
            real_records=_real_records_open(acct))
        if plan.get("error"):
            return jsonify({"error": plan["error"]}), plan.get("code", 400)
        if plan.get("soon"):
            # Not live yet — acknowledge intent (never a dead-end button).
            return jsonify({"soon": True, "connector": connector_id})
        # Real-record connections require informed consent, enforced here so
        # a client that skips the consent card is refused server-side (CARIN
        # CoC: proactive consent in advance of personal data disclosure).
        consent_version = None
        if plan.get("requires_consent"):
            refused = _consent_refusal(body)
            if refused:
                return refused
            consent_version = tester_terms.CONSENT_VERSION
        if connector_id != "fasten":
            return _persist_connection(connector_id, acct, plan,
                                       consent_version)
        # Two tabs or devices connecting at once (#847): both passed the
        # pending check before either inserted. The account's connect lease
        # (the same one the sample tap uses) makes the check and the insert
        # one step. A pending row keeps the consent_version it was made
        # with; a stale one is asked again by the worker (beta spec 4.3).
        if not svc.claim_sample_start(acct.id):
            return jsonify({"status": "connecting",
                            "error": "Your records are already on their "
                                     "way. Try again in a moment."}), 409
        try:
            # A second tap while the first is still connecting reuses it:
            # two taps made two identical rows, both stuck connecting. Only
            # a pending row is reused, and only after the gate and consent
            # above.
            waiting = svc.pending_connection(acct.id, "fasten")
            if waiting:
                return jsonify({
                    "id": waiting["id"], "status": "pending",
                    "existing": True,
                    "connect_url": hc.fasten_connect_url(
                        waiting["tenant_id"])})
            return _persist_connection(connector_id, acct, plan,
                                       consent_version)
        finally:
            svc.release_sample_start(acct.id)

    def _persist_connection(connector_id, acct, plan, consent_version):
        """What _start_connection does once the gate and consent passed:
        seed, record the connection, answer."""
        tenant = plan["tenant"]
        if plan.get("seed"):
            try:
                hc.seed(tenant)
            except HealthClawError:
                return jsonify({"error": "records service unavailable"}), 503
        cid = svc.add_connection(acct.id, connector_id, tenant, plan["label"],
                                 status=plan["status"],
                                 provider=plan.get("provider"),
                                 consent_version=consent_version)
        if connector_id == "sample":
            # A count for the record card, and the first assistant. Neither
            # blocks the connect: an unknown count renders as no count.
            try:
                svc.mark_synced(cid, hc.record_count(tenant))
            except HealthClawError:
                logger.warning("record count after sample seed failed for %s",
                               cid)
            svc.ensure_first_agent(acct.id, cid)
            return jsonify(_sample_answer(acct.id, cid, False))
        out = {"id": cid, "status": plan["status"]}
        if plan.get("connect_url"):
            out["connect_url"] = plan["connect_url"]
        return jsonify(out)

    @app.post("/api/connections/<connector_id>")
    @login_required
    def add_connection(connector_id):
        acct = current_account()
        body = request.get_json(silent=True) or {}
        if connector_id != "sample":
            return _start_connection(connector_id, acct, body)
        if svc.is_paused(acct.id):
            # Nothing new is added while paused, the sample included; the
            # same answer as refresh and upload (#856 sign-off F2).
            return jsonify({"error": "records_paused",
                            "message": beta.PAUSED_RECORDS_TEXT}), 423
        # One sample per account (calm hub spec section 4). A second tap
        # opens the one that exists; a tap while another is still seeding
        # mints nothing.
        existing = svc.active_sample(acct.id)
        if existing:
            return jsonify(_sample_answer(acct.id, existing["id"], True))
        if not svc.claim_sample_start(acct.id):
            existing = svc.active_sample(acct.id)
            if existing:
                return jsonify(_sample_answer(acct.id, existing["id"], True))
            return jsonify({"status": "connecting",
                            "error": "Your sample records are on their way. "
                                     "Try again in a moment."}), 409
        try:
            # Look again under the lease: another tap may have finished and
            # released it between our first look and our claim.
            existing = svc.active_sample(acct.id)
            if existing:
                return jsonify(_sample_answer(acct.id, existing["id"], True))
            return _start_connection(connector_id, acct, body)
        finally:
            svc.release_sample_start(acct.id)

    @app.post("/api/connections/<conn_id>/upload")
    @login_required
    def upload_connection(conn_id):
        """File upload for the `direct` connector tile (#227).

        The zero-integration ingest path: a signed-in patient posts a FHIR
        Bundle they exported from another app or provider portal, and the
        engine's `internal/ingest-bundle` endpoint runs it through the same
        code path Fasten/SHC take. Deliberately synchronous so the caller
        sees an honest per-entry result rather than a fire-and-forget ack.

        Contract:
          - Ownership: `svc.get_connection(acct.id, conn_id)` — cross-account
            reads 404, same shape as every other connection route.
          - Kind gate: only `direct` connections accept uploads today.
          - Body cap: streamed at max_bytes+1 (Content-Length is untrusted,
            chunked requests can still exceed a header value).
          - MIME: `application/fhir+json`, `application/json`, or
            `application/json+fhir` (charset optional). FHIR R4 §3.2 SHALL.
          - Error codes from the engine (`too_many_entries`, `not_a_bundle`,
            `payload_too_large`, `content_type_required`, `ingest_error`,
            `commit_failed`) are preserved through `HealthClawError.code` so
            the UI can render an actionable message instead of a generic
            "sync failed".
          - Response strips the engine's internal `tenant_id` — the browser
            never needs it and it is not a fact for the user.
          - `mark_synced` runs only when at least one entry landed; an
            all-failed / all-skipped bundle does not fake sync freshness.
        """
        acct = current_account()
        conn = svc.get_connection(acct.id, conn_id)
        if conn is None:
            return jsonify({"error": "unknown connection"}), 404
        if svc.is_paused(acct.id):
            return jsonify({"error": "records_paused",
                            "message": beta.PAUSED_RECORDS_TEXT}), 423
        # Only the `direct` tile ships this flow today. `shl` (SMART Health
        # Link) will land on the same endpoint once the encrypted-manifest
        # decoder is in.
        if conn["kind"] != "direct":
            return jsonify({"error": "wrong_connector_kind",
                            "kind": conn["kind"],
                            "message": "This connection does not accept file "
                                       "uploads. Create an 'Upload records' "
                                       "connection to import a FHIR bundle."}), 400
        # Revocation was enforced only in the template (home.html hides the
        # upload button once status == "revoked"), so a revoked connection
        # still accepted uploads on a direct/crafted request — defeating the
        # disconnect guarantee ("stop new data flowing", accounts.py). A
        # `direct` connection's normal lifecycle is "empty" (created, no
        # file yet) -> "active" (after the first successful upload), so the
        # check must exclude "revoked" specifically rather than require
        # "active" — requiring "active" would refuse every first upload.
        if conn["status"] == "revoked":
            return jsonify({"error": "connection_not_active",
                            "status": conn["status"],
                            "message": "This connection has been disconnected "
                                       "and no longer accepts uploads."}), 409

        # Header short-circuit for callers that DO set Content-Length, but
        # never trusted alone — the stream read below is the real bound.
        clen = request.content_length
        if clen is not None and clen > _UPLOAD_MAX_BYTES:
            return jsonify({"error": "payload_too_large",
                            "max_bytes": _UPLOAD_MAX_BYTES}), 413
        ct = (request.content_type or "").split(";", 1)[0].strip().lower()
        if ct not in _UPLOAD_MIME_TYPES:
            return jsonify({"error": "content_type_required",
                            "message": "Content-Type must be one of: "
                                       + ", ".join(sorted(_UPLOAD_MIME_TYPES))
                            }), 415

        # Streaming hard cap: even a chunked or Content-Length-absent request
        # cannot spend more than max_bytes+1 bytes in our process before we
        # refuse it.
        try:
            raw = request.stream.read(_UPLOAD_MAX_BYTES + 1)
        except Exception:
            return jsonify({"error": "invalid_body"}), 400
        if raw is None:
            raw = b""
        if len(raw) > _UPLOAD_MAX_BYTES:
            return jsonify({"error": "payload_too_large",
                            "max_bytes": _UPLOAD_MAX_BYTES}), 413

        try:
            bundle = json.loads(raw.decode("utf-8")) if raw else {}
        except RecursionError:
            # Same crash lever as the engine's ingest-bundle endpoint
            # (#267 review): CPython's JSON scanner recurses per nesting
            # level, and a payload well under _UPLOAD_MAX_BYTES can nest
            # deep enough to raise RecursionError, which is not a
            # ValueError and was previously unhandled -> 500. This route
            # already sits behind @login_required, so the engine-side fix
            # (moving auth before parse) doesn't apply here; this is purely
            # the crash fix.
            return jsonify({"error": "invalid_json",
                            "message": "nesting too deep to parse"}), 400
        except (ValueError, UnicodeDecodeError):
            return jsonify({"error": "invalid_json"}), 400
        if not isinstance(bundle, dict):
            return jsonify({"error": "invalid_json",
                            "message": "body must be a JSON object"}), 400

        # Engine validates Bundle shape, entry-count cap, per-entry, etc.
        # Its stable `error` code is preserved via HealthClawError.code so
        # the UI can render an actionable message rather than collapsing
        # every 4xx into "ingest_failed".
        try:
            result = hc.ingest_bundle(conn["tenant_id"], bundle)
        except HealthClawError as exc:
            status = exc.status if 400 <= exc.status < 500 else 502
            payload = {
                "error": exc.code or "ingest_failed",
                "status": status,
            }
            # A correlation id from `commit_failed` / `ingest_error` is
            # PHI-safe and lets the user quote it to support. Never echo
            # the raw exception message — it can contain SQL bindings.
            if exc.correlation_id:
                payload["correlation_id"] = exc.correlation_id
            return jsonify(payload), status

        # Empty file / bundle-with-no-entries is not an error — say what
        # landed and return. `mark_synced` and the connection-active flip
        # both key on `ingested > 0` so an all-failed / all-skipped bundle
        # never fakes sync freshness (crista #227 release condition 4).
        landed = int(result.get("ingested") or 0)
        # The tenant's document total after this upload, for the same
        # standing caveat the poll carries. None when it could not be read.
        uncounted = 0
        if landed > 0:
            try:
                svc.activate_connection(conn["tenant_id"])
            except Exception:  # noqa: BLE001
                logger.warning(
                    "could not flip connection %s to active", conn_id)
            uncounted = None
            try:
                readable_total = hc.record_count(conn["tenant_id"])
                uncounted = hc.uncounted_record_count(conn["tenant_id"])
                svc.mark_synced(conn_id, readable_total, uncounted)
            except HealthClawError:
                logger.warning("record_count after upload failed for %s",
                               conn_id)

        # Strip the engine's internal `tenant_id` before the browser sees
        # the response — it is not a fact the UI needs and leaking it here
        # would give the browser a token to try elsewhere.
        response = {k: v for k, v in result.items() if k != "tenant_id"}
        response["connection_id"] = conn_id
        # `ingested` counts documents nothing can open, so the card used to
        # say "17 records added" where a refresh then said 5 (#226). Report
        # what the patient can reach, and the same sentence the poll uses.
        documents = _documents_landed(bundle, result, landed)
        response["records_added"] = landed - documents
        note = _uncounted_note(landed - documents, documents, uncounted)
        if note:
            response["uncounted_note"] = note
        return jsonify(response), 200

    @app.post("/api/connections/<conn_id>/disconnect")
    @login_required
    def disconnect_connection(conn_id):
        """Stop new data flowing; keep records already collected."""
        acct = current_account()
        conn = svc.get_connection(acct.id, conn_id)
        if conn is None:
            return jsonify({"error": "unknown connection"}), 404
        # HealthClaw first, for every kind (a no-op on a sample tenant).
        # Flipping only our row left the engine importing into the tenant
        # from an old connect link, a late webhook, a retry or the reaper.
        # Unconfirmed means the row stays as it was: we never say
        # "disconnected" while records may still arrive.
        try:
            hc.revoke_fasten(conn["tenant_id"])
        except HealthClawError:
            return jsonify({"error": "disconnect_failed",
                            "message": "We couldn't confirm the disconnect. "
                                       "Your connection is still on. Please "
                                       "try again in a minute."}), 503
        if not svc.revoke_connection(acct.id, conn_id):
            return jsonify({"error": "unknown connection"}), 404
        # The hub shows this after its reload, so the person is told what
        # changed, and that what they had stays (#847).
        return jsonify({"status": "revoked", "connection_id": conn_id,
                        "message": ("Disconnected. No new records will "
                                    "arrive. The records already here stay; "
                                    "you'll find them under Past "
                                    "connections.")})

    @app.delete("/api/connections/<conn_id>")
    @login_required
    def delete_connection(conn_id):
        """Delete the records themselves, then the connection.

        Order matters: purge first and only unlink once the engine confirms
        it. Unlinking first would leave the patient with a clean-looking hub
        while their data still sat in HealthClaw, unreachable but present.
        """
        acct = current_account()
        conn = svc.get_connection(acct.id, conn_id)
        if conn is None:
            return jsonify({"error": "unknown connection"}), 404
        try:
            purged = hc.purge_tenant(conn["tenant_id"])
        except HealthClawError:
            # Never claim a deletion that did not happen — and this said more
            # than that. "Nothing was changed" is the same claim the review
            # arms above used to make, on the destructive route: `purge_tenant`
            # raises on ANY non-200 and on a read timeout, so a gateway 5xx or
            # a lost answer can follow a purge that ran. Found by inventorying
            # this file for claims about what was sent, approved, recorded or
            # retried rather than by another report about one arm.
            #
            # No `deleted` field here at all (#586). `purge_tenant` raises on
            # any non-200 and on a lost answer, so on this branch the records
            # may be gone or may not; a field named `deleted` saying False
            # would be wrong on exactly the outcome where the purge ran. What
            # IS observed is that the unlink below was not reached, and the
            # field says that by its name: the connection is still linked.
            return jsonify({"error": "deletion_failed", "unlinked": False,
                            "message": "We couldn't confirm your records were "
                                       "deleted. Your connection is still "
                                       "linked here — please try again."}), 502
        # Anything still sharing these records is revoked at HealthClaw before
        # the connection is unlinked (spec §13.4). A revoke that cannot be
        # confirmed keeps the connection listed, so the person can see the
        # grant still active and retry; the records are already gone either
        # way, and this response says both halves plainly.
        still_active = []
        for grant in svc.grants_for_connection(acct.id, conn_id):
            try:
                hc.revoke_consent(grant["consent_id"])
                svc.mark_grant_revoked(acct.id, grant["id"])
            except HealthClawError:
                still_active.append(grant["id"])
        if still_active:
            return jsonify({
                "deleted": True, "unlinked": False, "grants_active": len(still_active),
                "connection_id": conn_id,
                "rows_deleted": purged.get("rows_deleted", 0),
                "message": ("Your records were deleted, but we couldn't confirm "
                            "that an app you shared them with was cut off. The "
                            "connection stays listed so you can try again."),
            }), 502
        # Read before the unlink: deleting a connection deletes the
        # assistants that read it, and the person is told which (#853).
        gone = [a["name"] for a in svc.list_home(acct.id)["agents"]
                if a["connection_id"] == conn_id]
        svc.delete_connection(acct.id, conn_id)
        # The count is the one the card showed. The engine's resource count
        # includes kinds the card leaves out, so "15 stored items" beside a
        # card saying 9 records read as a mistake (patient tester, #853).
        n = conn.get("last_count")
        n = n if isinstance(n, int) and not isinstance(n, bool) else None
        return jsonify({
            "deleted": True,
            "unlinked": True,
            "connection_id": conn_id,
            "rows_deleted": purged.get("rows_deleted", 0),
            "records_deleted": n,
            "assistants_deleted": gone,
            "audit_retained": True,
            "message": _deleted_sentence(n, conn["label"], gone),
        })

    def _deleted_sentence(n: int | None, label: str,
                          gone: list[str]) -> str:
        if n is None:
            lead = f"Your records in {label} were deleted."
        elif n == 1:
            lead = f"The 1 record in {label} was deleted."
        else:
            lead = f"All {n} records in {label} were deleted."
        if gone:
            lead += (f" {' and '.join(gone)} "
                     f"{'was' if len(gone) == 1 else 'were'} deleted too, "
                     f"because {'it' if len(gone) == 1 else 'they'} read "
                     "these records.")
        return (lead + " We keep a log of who looked at your records, with "
                "no health details in it, and this deletion is in it.")

    @app.post("/api/account/delete")
    @login_required
    def delete_account():
        """Delete the account itself, end to end (#554).

        The connection delete above purges a tenant and leaves the account
        row — email, passkey, consents — with no path to remove it. This is
        that path. Same order and the same posture as the connection delete:
        every connection's records are purged first and a purge the engine
        cannot confirm stops everything, so a vanished account never hides
        records still sitting in HealthClaw. The gate is the typed DELETE the
        records purge uses; re-authentication for both is one change, if
        wanted, and not a difference between them.
        """
        acct = current_account()
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "DELETE":
            return jsonify({"error": "confirm_required",
                            "message": "Type DELETE to confirm."}), 400
        purged = 0
        for conn in svc.list_home(acct.id)["connections"]:
            try:
                hc.purge_tenant(conn["tenant_id"])
            except HealthClawError:
                # As on the connection route: the purge may or may not have
                # run, so no `deleted` field; what is observed is that the
                # account still stands.
                return jsonify({
                    "error": "deletion_failed",
                    "message": "We couldn't confirm your records were "
                               "deleted. Your account is unchanged — please "
                               "try again."}), 502
            purged += 1
            for grant in svc.grants_for_connection(acct.id, conn["id"]):
                try:
                    hc.revoke_consent(grant["consent_id"])
                except HealthClawError:
                    logger.warning("consent revoke unconfirmed during "
                                   "account delete for account %s", acct.id)
        svc.delete_account(acct.id)
        session.clear()
        # Account id only: never the email, never a tenant (#554 audit line).
        logger.info("account deleted: %s (connections purged: %d)",
                    acct.id, purged)
        return jsonify({"deleted": True, "connections_purged": purged,
                        "audit_retained": True})

    @app.post("/api/connections/<conn_id>/consent")
    @login_required
    def reconsent_connection(conn_id):
        """Accept the current terms for an existing real connection (beta
        spec 4.3). Until this is done, the worker answers that connection's
        turns with beta.TERMS_TEXT instead of reaching a model."""
        acct = current_account()
        conn = svc.get_connection(acct.id, conn_id)
        if (conn is None or conn["kind"] == "sample"
                or conn["status"] == "revoked"):
            return jsonify({"error": "unknown connection"}), 404
        body = request.get_json(silent=True) or {}
        refused = _consent_refusal(body)
        if refused:
            return refused
        svc.record_consent(acct.id, conn_id, tester_terms.CONSENT_VERSION)
        return jsonify({"consent_version": tester_terms.CONSENT_VERSION})

    @app.post("/api/connections/<conn_id>/refresh")
    @login_required
    def refresh_connection(conn_id):
        """Re-pull an existing connection.

        Surface-agnostic on purpose: web, Telegram, and iMessage all land here,
        so the consent check and the sync bookkeeping cannot differ by surface.
        Refresh reuses the connection's existing tenant — HealthClaw's ingest
        upserts on (tenant, resource_type, id), so repeating this updates
        records rather than duplicating them.
        """
        acct = current_account()
        conn = svc.get_connection(acct.id, conn_id)
        if conn is None:
            return jsonify({"error": "unknown connection"}), 404
        # Disconnect stops new records. A refresh hands back the connect URL
        # for the same tenant, so on a disconnected connection it would
        # reopen the pipe (security review of #904, F2). Refused before the
        # sync baseline below is touched; same answer upload gives.
        if conn["status"] == "revoked":
            return jsonify({"error": "connection_not_active",
                            "status": conn["status"],
                            "message": "This connection has been "
                                       "disconnected. Connect your records "
                                       "again to get new ones."}), 409
        if svc.is_paused(acct.id):
            return jsonify({"error": "records_paused",
                            "message": beta.PAUSED_RECORDS_TEXT}), 423

        body = request.get_json(silent=True) or {}
        plan = connectors.refresh(conn["kind"], conn["tenant_id"],
                                  body.get("provider"), cfg, hc)
        if plan.get("error"):
            return jsonify({"error": plan["error"]}), plan.get("code", 400)
        if plan.get("unsupported"):
            return jsonify({"unsupported": True, "reason": plan["reason"]})

        # Same server-side consent gate as the initial connect: a client that
        # skips the card is refused here, on every surface.
        if plan.get("requires_consent"):
            refused = _consent_refusal(body)
            if refused:
                return refused

        # Baseline the count BEFORE re-authorizing so the follow-up poll can
        # report what the refresh actually added — documents on the same
        # terms, so "notes arrived" is a measured delta rather than a restatement
        # of "this tenant holds notes" (#226).
        try:
            svc.mark_synced(conn_id, hc.record_count(conn["tenant_id"]),
                            hc.uncounted_record_count(conn["tenant_id"]))
        except HealthClawError:
            return jsonify({"error": "records service unavailable"}), 503

        out = {"status": "reauth", "connection_id": conn_id}
        if plan.get("reauth_url"):
            out["reauth_url"] = plan["reauth_url"]
        return jsonify(out)

    @app.get("/api/connections/<conn_tenant>/poll")
    @login_required
    def poll_connection(conn_tenant):
        acct = current_account()
        # ownership: the tenant must belong to one of the account's connections
        conns = {c["tenant_id"]: c
                 for c in svc.list_home(acct.id)["connections"]}
        if conn_tenant not in conns:
            return jsonify({"error": "not yours"}), 404
        # Disconnected: say so. Records that landed earlier made this answer
        # "active" while the row stayed revoked (QA on #909).
        if conns[conn_tenant]["status"] == "revoked":
            return jsonify({"status": "revoked"})
        try:
            landed = hc.tenant_has_records(conn_tenant)
        except HealthClawError:
            # Not "pending". We did not fail to find records, we failed to
            # look, and the two are different answers. Rendering this as
            # pending left the patient watching "still fetching your records"
            # on a condition nothing would ever re-evaluate, with nothing
            # anywhere saying the record store was down (#403).
            return jsonify({
                "status": "unavailable",
                "error": "records_unavailable",
                "message": "We couldn't reach your records right now. That's "
                           "a problem on our side — we'll keep checking.",
            }), 503
        if landed:
            svc.activate_connection(conn_tenant)
            out = {"status": "active"}
            # After a refresh, report growth against the baseline that refresh
            # recorded. Read-only: the count is re-baselined by the next
            # refresh, so repeated polls keep showing the same number instead
            # of decaying to zero while the patient is still reading it.
            baseline = conns[conn_tenant].get("last_count")
            if baseline is not None:
                try:
                    current = hc.record_count(conn_tenant)
                except HealthClawError:
                    current = None
                # The count covers what the patient can actually reach;
                # DocumentReferences are ingested but unreadable, so they are
                # out of it and the sentence below says so (#226). When the
                # probe itself fails we hedge rather than suppress: the count
                # is a fact we did measure, and hiding it would trade one
                # silence for another. State the known part, name the unknown
                # — the posture the review relay already takes on an
                # unestablished `confirmed`.
                try:
                    uncounted = hc.uncounted_record_count(conn_tenant)
                except HealthClawError:
                    uncounted = None
                if current is not None:
                    new_records = max(0, current - int(baseline))
                    out["record_count"] = current
                    out["new_records"] = new_records
                    # Documents that demonstrably arrived since the refresh
                    # baselined them. None when the probe failed or the
                    # connection predates the baseline column — in both cases
                    # arrival is unknown, and unknown is never claimed.
                    doc_baseline = conns[conn_tenant].get("last_uncounted")
                    new_documents = (
                        None if uncounted is None or doc_baseline is None
                        else max(0, uncounted - int(doc_baseline)))
                    note = _uncounted_note(new_records, new_documents,
                                           uncounted)
                    if note:
                        out["uncounted_note"] = note
            return jsonify(out)
        return jsonify({"status": "pending"})

    # --- agents --------------------------------------------------------------

    @app.post("/api/agents")
    @login_required
    def create_agent():
        acct = current_account()
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "Juniper").strip()[:48] or "Juniper"
        persona = body.get("persona") if body.get(
            "persona") in PERSONAS else DEFAULT_PERSONA
        # Advisor is optional; an unavailable/unknown one is refused rather
        # than silently downgraded — never pretend a capability exists.
        advisor = body.get("advisor") or None
        if advisor:
            spec = advisors.get(advisor)
            if spec is None:
                return jsonify({"error": "unknown advisor"}), 400
            if not spec["available"]:
                return jsonify({"error": "advisor_not_available",
                                "note": spec.get("note", "")}), 400
        try:
            aid = svc.create_agent(acct.id, name, persona,
                                   body.get("connection_id", ""),
                                   advisor=advisor)
        except AuthError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"id": aid})

    @app.post("/api/agents/<agent_id>/rename")
    @login_required
    def rename_agent(agent_id):
        acct = current_account()
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()[:48]
        if not name:
            return jsonify({"error": "Give your assistant a name."}), 400
        if not svc.rename_agent(acct.id, agent_id, name):
            return jsonify({"error": "unknown agent"}), 404
        return jsonify({"id": agent_id, "name": name})

    @app.post("/api/agents/<agent_id>/connection")
    @login_required
    def move_agent(agent_id):
        """Change records. The service does the ownership check, so there
        is one place to get it right and one place to mutation-test."""
        acct = current_account()
        conn_id = str((request.get_json(silent=True) or {})
                      .get("connection_id") or "")
        try:
            svc.move_agent(acct.id, agent_id, conn_id)
        except AuthError:
            return jsonify({"error": "unknown agent or connection"}), 404
        return jsonify({"id": agent_id, "connection_id": conn_id})

    @app.delete("/api/agents/<agent_id>")
    @login_required
    def delete_agent(agent_id):
        acct = current_account()
        if not svc.delete_agent(acct.id, agent_id):
            return jsonify({"error": "unknown agent"}), 404
        return jsonify({"deleted": True, "id": agent_id})

    @app.post("/api/hub/switch-prompt")
    @login_required
    def answer_switch_prompt():
        """Either answer ends the question; "switch" also moves the agent,
        through the same ownership check as Change records."""
        acct = current_account()
        body = request.get_json(silent=True) or {}
        answer = body.get("answer")
        if answer == "switch":
            try:
                svc.move_agent(acct.id, str(body.get("agent_id") or ""),
                               str(body.get("connection_id") or ""))
            except AuthError:
                return jsonify({"error": "unknown agent or connection"}), 404
        elif answer != "later":
            return jsonify({"error": "answer must be switch or later"}), 400
        svc.stamp_switch_prompt(acct.id)
        return jsonify({"answer": answer})

    @app.get("/chat")
    @login_required
    def chat():
        acct = current_account()
        # #645: a tester who typed `agent_id` (the name every JSON-body
        # endpoint in this file uses — /api/agents, /api/chat, /api/form)
        # got a silent redirect to /home with no error. `agent` is this
        # route's actual name (the one internal JS call site that builds
        # this URL uses it); accept the other spelling too rather than
        # failing silently on it.
        agent_id = request.args.get("agent") or request.args.get("agent_id", "")
        ctx = svc.get_agent_context(acct.id, agent_id)
        if not ctx:
            return redirect(url_for("home"))
        p = PERSONAS.get(ctx["agent"]["persona"], PERSONAS[DEFAULT_PERSONA])
        conversation_id = hc.conversation_id(agent_id)
        # Show the conversation they actually had. Rendering only the canned
        # greeting made every return visit look like a first visit.
        try:
            past = hc.recent_messages(
                ctx["tenant"], limit=30,
                conversation_id=conversation_id,
                agent_id=agent_id,
            )
            history_lost = False
        except HealthClawError:
            # An outage used to render as an empty conversation, so a
            # returning patient was greeted as though they had never been
            # here. Say we could not load it rather than showing a blank
            # slate that looks like a fact about them.
            logger.exception("chat history unavailable for agent %s", agent_id)
            past, history_lost = [], True
        conn = ctx.get("connection") or {}
        pending = conn.get("status") == "pending"
        totals = None
        # Count when the greeting will use it, and whenever an import is
        # outstanding — a `pending` row is the one case where the stored status
        # may be behind the records, and a returning patient checking whether
        # anything landed is exactly who must not be told "still arriving"
        # about a chart that is already here.
        if not past or pending:
            # _summary=count, NOT len(entry): entry is one page (the server
            # caps it), so len() reported the page size as the total. A real
            # import showed "50 conditions ... 50 lab results" to a person
            # with 52 and 186 — two different numbers both wrong, and both
            # capped at exactly the page limit, which is how it read as
            # plausible for six weeks on demo-sized data. Same pattern as
            # HealthClawClient.record_count, and PHI-free for the same
            # reason: only totals cross, never resources.
            try:
                def _total(rt: str) -> int:
                    bundle = hc.search(ctx["tenant"], rt,
                                       {"_summary": "count"})
                    return int(bundle.get("total") or 0)
                totals = {
                    "conditions": _total("Condition"),
                    "medications": _total("MedicationRequest"),
                    "labs": _total("Observation"),
                }
            except (HealthClawError, TypeError, ValueError):
                totals = None   # unknown, which is never the same as zero
        intake = intake_state.classify(
            totals=totals,
            connection_status=conn.get("status"),
            connected_at=conn.get("connected_at"),
            now=time.time(),
            provider=conn.get("provider") or conn.get("label"))
        if pending and intake.state == intake_state.READY:
            # Records landed while nobody was polling /connect. Settle the
            # stored status here so the next visit costs no counts.
            svc.set_connection_status(ctx["tenant"], "active")
        # The "Open the review" card is a turn event, and history keeps only
        # text, so a reload lost it while the reply above still promised it
        # (#847). Ask the engine what is waiting instead; nothing is stored.
        # A lookup that fails draws no card, and the "Waiting for you" pill
        # in the header still leads to the list. A disconnected connection
        # is not a pathway to its requests (#215), so it is not asked at
        # all: a card there would promise an approval the relay refuses
        # (security review of #853).
        reviews = []
        if conn.get("status") != "revoked":
            try:
                reviews = [{"id": a["id"],
                            "label": _KIND_LABELS.get(a.get("kind"),
                                                      "Request"),
                            "form": a.get("kind") == "form-fill"}
                           for a in hc.pending_actions(ctx["tenant"])
                           if a.get("id")]
            except HealthClawError:
                logger.warning("pending reviews unavailable for agent %s",
                               agent_id)
                reviews = []
        return render_template("chat.html", me=ctx["agent"], persona=p,
                               agent_id=agent_id,
                               conversation_id=conversation_id,
                               past=past,
                               history_lost=history_lost,
                               intake=intake,
                               summary_counts=intake.counts,
                               pending_reviews=reviews)

    @app.get("/brief")
    @login_required
    def brief():
        acct = current_account()
        agent_id = request.args.get("agent", "")
        ctx = svc.get_agent_context(acct.id, agent_id)
        if not ctx:
            return redirect(url_for("home"))
        try:
            raw = hc.fetch_appointment_brief(ctx["tenant"])
            unavailable = False
        except HealthClawError:
            # "Not available from your connected records" is a statement about
            # the records. We did not read them, so we cannot make it.
            logger.exception("brief unavailable for agent %s", agent_id)
            raw, unavailable = None, True
        sections = _parse_brief_sections(raw) if raw else {}
        return render_template("brief.html", me=ctx["agent"],
                               agent_id=agent_id, sections=sections,
                               brief_unavailable=unavailable,
                               care_gaps_ok=(_parse_care_gaps_status(raw)
                                             == _CARE_GAPS_OK),
                               care_gaps_note=_parse_care_gaps_reason(raw))

    # --- chat API (SSE), scoped to the account's agent -----------------------

    def _allow_turn(key: str) -> bool:
        window = turns[key]
        now = time.time()
        while window and now - window[0] > cfg.chat_window_seconds:
            window.popleft()
        if len(window) >= cfg.chat_turns_per_window:
            return False
        window.append(now)
        return True

    def _event_for_browser(event: dict) -> dict | None:
        kind = event.get("type")
        payload = dict(event.get("payload") or {})
        if kind == "agent.tool":
            return {"type": "tool", "name": payload.get("name"),
                    "label": payload.get("label")}
        if kind == "agent.card":
            payload.pop("provider_call_id", None)
            payload.pop("event_key", None)
            return payload
        if kind == "agent.text":
            return {"type": "text", "text": payload.get("text") or ""}
        if kind == "agent.error":
            return {"type": "error",
                    "text": payload.get("text") or GENERIC_FAILURE_TEXT}
        return None

    def _run_belongs_to(run: dict, tenant: str, agent_id: str) -> bool:
        return run.get("tenant_id") == tenant and run.get("agent_id") == agent_id

    # Worker readiness has three states, not two: the health endpoint said
    # ready, it said not ready, or we could not ask it. All three still fail
    # closed — a turn we cannot promise is refused either way — so the gate
    # below is unchanged. What changes is the claim: an incident used to be
    # filed as "the workers are down" when the workers were never reached
    # (#410).
    WORKERS_READY, WORKERS_NOT_READY, WORKERS_UNKNOWN, WORKERS_REJECTED = (
        "ready", "not_ready", "unknown", "rejected")

    def _worker_state(timeout=None) -> str:
        try:
            status = hc.agent_worker_health(cfg.run_worker_stale_seconds,
                                            timeout=timeout)
        except HealthClawError as exc:
            # A 4xx is an ANSWER, not a failure to ask. HealthClaw understood
            # the request and refused something about IT — the credential, the
            # path, the query — so the fault is in THIS container's
            # configuration and it gates the deploy exactly like a missing
            # worker does. Measured: a wrong HEALTHCLAW_MINT_SECRET answers
            # 403 and a wrong HEALTHCLAW_BASE path root answers 404, and both
            # used to be filed as "we could not ask" and let a chat-dead
            # deployment go live.
            #
            # Everything else stays `unknown`, deliberately: transport failure
            # (status 0), a 5xx, and a 200 carrying a proxy interstitial are
            # all statements about HealthClaw or the network to it, not about
            # us. Blocking on those would re-block the deploy that fixes the
            # outage, which is the ruling this endpoint exists to implement.
            if 400 <= getattr(exc, "status", 0) < 500:
                return WORKERS_REJECTED
            return WORKERS_UNKNOWN
        return WORKERS_READY if status.get("available") else WORKERS_NOT_READY

    #: Which worker states mean "this deployment cannot finish a chat turn and
    #: the fault is in the thing being deployed". These gate `/healthz`, and
    #: through it the deploy. `unknown` is deliberately absent.
    WORKERS_OUR_FAULT = (WORKERS_NOT_READY, WORKERS_REJECTED)

    #: The machine-readable refusal code per state. Three states, three codes:
    #: collapsing `rejected` into `run_workers_unavailable` would file "our
    #: secret is wrong" as "the workers are down", which is the #410
    #: mislabeling one layer along.
    _REFUSAL_CODES = {
        WORKERS_UNKNOWN: "run_workers_unknown",
        WORKERS_REJECTED: "run_workers_rejected",
        WORKERS_NOT_READY: "run_workers_unavailable",
    }

    def _refuse_turn_without_workers(state: str):
        """The 503 body for a chat turn no worker can be promised to run.

        The patient-facing sentence is the same in every failure state,
        because it is true in all of them. Only the machine-readable code
        differs.
        """
        return jsonify({
            "error": _REFUSAL_CODES.get(state, "run_workers_unavailable"),
            "message": "Chat is temporarily unavailable. Try again soon.",
        }), 503

    def _stream_run(tenant: str, agent_id: str, run_id: str, after: int = 0):
        """Replay durable UI events. Disconnecting only stops this projection."""
        cursor = max(0, after)
        started = time.monotonic()
        # #575: the wait after a page with events is the base; after the k-th
        # consecutive empty page it is base * 2**(k-1), capped. A run idling
        # at 90 seconds is no likelier to finish in the next 250ms than one
        # at 5, and a token that just arrived is likely followed by another.
        empty_streak = 0
        yield "data: " + json.dumps({
            "type": "accepted", "run_id": run_id,
            "next_cursor": cursor}) + "\n\n"
        while time.monotonic() - started < cfg.run_sse_timeout_seconds:
            try:
                page = hc.agent_run_events(tenant, run_id, after=cursor,
                                           limit=100)
            except HealthClawError:
                # Without this the generator dies inside an open response and
                # the browser is left with a stream that stopped for no stated
                # reason (#219). Only this projection ends: the run is durable
                # and keeps going, and a reconnect resumes at `cursor`. The
                # text is the same generic sentence every other failure uses —
                # an exception message here would be the first place engine
                # internals reached a patient's screen.
                logger.warning("run event stream failed for tenant %s", tenant)
                yield "data: " + json.dumps({
                    "type": "error", "text": GENERIC_FAILURE_TEXT}) + "\n\n"
                return
            events = page.get("events") or []
            for event in events:
                cursor = max(cursor, int(event.get("id") or 0))
                projected = _event_for_browser(event)
                if projected is not None:
                    yield (f"id: {cursor}\n"
                           f"data: {json.dumps(projected)}\n\n")
            # A terminal run can have more than one page of durable events.
            # Drain full pages before emitting done, otherwise reconnecting at
            # the returned cursor would be the only way to see the tail.
            if len(events) >= 100:
                continue
            status = page.get("status")
            if status in ("completed", "failed", "cancelled",
                          "waiting_for_human"):
                done = {"type": "done", "status": status,
                        "next_cursor": cursor}
                yield (f"id: {cursor}\n"
                       f"data: {json.dumps(done)}\n\n")
                return
            empty_streak = 0 if events else empty_streak + 1
            time.sleep(cfg.run_sse_poll_seconds if not empty_streak else min(
                cfg.run_sse_poll_seconds * 2 ** (empty_streak - 1),
                cfg.run_sse_poll_max_seconds))
        yield "data: " + json.dumps({
            "type": "reconnect", "run_id": run_id,
            "next_cursor": cursor}) + "\n\n"

    def _parse_cursor(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @app.post("/api/chat")
    @login_required
    def api_chat():
        acct = current_account()
        body = request.get_json(silent=True) or {}
        agent_id = body.get("agent_id", "")
        ctx = svc.get_agent_context(acct.id, agent_id)
        if not ctx:
            return jsonify({"error": "unknown agent"}), 404
        text = (body.get("message") or "").strip()
        if not text or len(text) > 2000:
            return jsonify({"error": "message must be 1-2000 characters"}), 400
        workers = _worker_state(timeout=_ADMISSION_WORKER_TIMEOUT)
        if workers != WORKERS_READY:
            return _refuse_turn_without_workers(workers)
        if not _allow_turn(acct.id):
            return jsonify({"error": "rate_limited"}), 429
        # A turn the worker will refuse (paused, or terms not accepted; beta
        # spec 4.3, 4.6) is answered with a fixed sentence and never reaches
        # a model, so the day's allowance does not stop it here.
        refused = beta.turn_block(ctx["connection"], svc.is_paused(acct.id),
                                  tester_terms.CONSENT_VERSION)
        # Durable daily ceiling — survives restarts and is shared across
        # workers, so it is the real bound on per-account inference spend.
        # Read only: the worker charges the turn where the model is called
        # (#856 sign-off F4), so a turn admitted while refused and unblocked
        # before it is claimed is still charged.
        if not refused:
            used = svc.daily_turns_used(acct.id)
            if used >= cfg.chat_turns_per_day:
                return jsonify({
                    "error": "daily_limit_reached",
                    "used": used,
                    "limit": cfg.chat_turns_per_day,
                    "message": beta.DAILY_LIMIT_TEXT,
                }), 429

        tenant = ctx["tenant"]
        agent = ctx["agent"]
        conversation_id = (body.get("conversation_id")
                           or hc.conversation_id(agent["id"]))
        request_id = str(body.get("request_id") or uuid.uuid4())
        if not 1 <= len(conversation_id) <= 128:
            return jsonify({"error": "invalid conversation_id"}), 400
        if not 1 <= len(request_id) <= 128:
            return jsonify({"error": "invalid request_id"}), 400
        created, user_message_id = hc.claim_inbound_message(
            tenant, text, agent["id"], conversation_id, "web", request_id)
        if created is None or not user_message_id:
            return jsonify({"error": "message store unavailable"}), 503
        try:
            run = hc.create_agent_run(
                tenant, user_message_id, cfg.run_deadline_seconds)
        except HealthClawError:
            return jsonify({"error": "run queue unavailable"}), 503
        after = _parse_cursor(body.get("after") or
                              request.headers.get("Last-Event-ID"))
        return Response(_stream_run(
            tenant, agent["id"], run["id"], after),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no",
                                 "X-CareAgents-Run-ID": run["id"]})

    @app.get("/api/chat/runs/<run_id>/events")
    @login_required
    def api_chat_events(run_id):
        acct = current_account()
        agent_id = request.args.get("agent_id", "")
        ctx = svc.get_agent_context(acct.id, agent_id)
        if not ctx:
            return jsonify({"error": "unknown agent"}), 404
        try:
            run = hc.get_agent_run(ctx["tenant"], run_id)
        except HealthClawError as exc:
            if _engine_said_absent(exc):
                return jsonify({"error": "unknown run"}), 404
            # "Unknown run" is a claim about what exists. During an incident
            # it is not true — we just could not look it up (#410).
            return jsonify({"error": "run service unavailable"}), 503
        if not _run_belongs_to(run, ctx["tenant"], agent_id):
            return jsonify({"error": "unknown run"}), 404
        after = _parse_cursor(request.args.get("after") or
                              request.headers.get("Last-Event-ID"))
        return Response(_stream_run(ctx["tenant"], agent_id, run_id, after),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    def _live_agent_context(acct, agent_id):
        """The agent's context when the account owns it AND its connection
        is still live. One rule for every approval surface (#215): the
        pending list, the review page, its submit/decline, and the status
        view all resolve ownership here, on the server. A revoked
        connection is not a pathway to the tenant's requests."""
        ctx = svc.get_agent_context(acct.id, agent_id) if acct else None
        if not ctx or not _connection_is_live(ctx):
            return None
        return ctx

    @app.get("/api/form/<action_id>")
    @login_required
    def form_status(action_id):
        acct = current_account()
        agent_id = request.args.get("agent", "")
        ctx = _live_agent_context(acct, agent_id)
        if not ctx:
            return jsonify({"error": "unknown agent"}), 404
        try:
            status = hc.action_status(ctx["tenant"], action_id)
        except HealthClawError as exc:
            if _engine_said_absent(exc):
                return jsonify({"status": "unknown"}), 404
            return jsonify({"status": "unavailable",
                            "error": "form status unavailable"}), 503
        outcome = {}
        try:
            outcome = json.loads(status.get("outcome_summary") or "{}")
        except ValueError:
            pass
        if not isinstance(outcome, dict):
            outcome = {}
        # `to` is the recipient label the pending list already shows; the
        # review page names it when a request is done (#847).
        return jsonify({"status": status.get("status"),
                        "delivery_link": outcome.get("delivery_link"),
                        "to": status.get("to")})

    @app.get("/api/labs/timeline")
    @login_required
    def labs_timeline():
        """Per-analyte lab series for the chat's timeline card.

        Same-origin and session-authenticated on purpose. The equivalent MCP
        App is served by HealthClaw, and embedding it here would mean either
        a cross-origin iframe with no credentials (a guaranteed 401) or a
        step-up token in a URL. Neither is worth it: this process already
        holds the credentials, so it fetches server-side and the browser
        never sees one.
        """
        acct = current_account()
        agent_id = request.args.get("agent", "")
        ctx = svc.get_agent_context(acct.id, agent_id)
        if not ctx:
            return jsonify({"error": "unknown agent"}), 404
        try:
            labs = hc.interpret_labs(ctx["tenant"])
        except HealthClawError:
            return jsonify({"error": "labs unavailable"}), 502
        keys = labs_timeline_mod.keys_for_topic(request.args.get("topic"))
        return jsonify({
            "series": labs_timeline_mod.build_series(labs.get("bundle") or {},
                                                     keys),
            "disclaimer": labs.get("disclaimer") or "",
        })

    # --- pending approvals (#215) --------------------------------------------

    _APPROVALS_UNCHECKABLE = ("We couldn't check for requests right now. "
                              "Nothing has been approved or declined — "
                              "please try again in a moment.")

    @app.get("/agents/<agent_id>/approvals")
    @login_required
    def approvals(agent_id):
        """Everything proposed on this agent's records that is waiting for the
        person's answer, each linking to the review relay. Ownership is the
        account's connection to the tenant, resolved server-side — which is
        how a request proposed over MCP, with no CareAgents agent id of its
        own, reaches the person who owns those records. Nothing here is
        stored: the list is the engine's answer, fetched on every visit."""
        acct = current_account()
        ctx = _live_agent_context(acct, agent_id)
        if not ctx:
            return render_template("chat_error.html",
                                   message="That agent isn't yours."), 404
        try:
            pending = hc.pending_actions(ctx["tenant"])
        except HealthClawError:
            # An unanswered question is not an empty inbox.
            logger.exception("pending actions failed for %s", agent_id)
            return render_template("chat_error.html",
                                   message=_APPROVALS_UNCHECKABLE), 503
        return render_template("approvals.html", me=ctx["agent"],
                               agent_id=agent_id, pending=pending,
                               kind_labels=_KIND_LABELS)

    @app.get("/api/approvals/count")
    @login_required
    def approvals_count():
        """How many requests wait for this person, for the hub's band.

        The same tenants the approvals page reads: each assistant's live
        connection, counted once. Any tenant that cannot be asked fails the
        whole count. A partial sum would read as a smaller inbox, and a
        failure must never render as zero (calm hub spec section 3).
        """
        acct = current_account()
        data = svc.list_home(acct.id)
        live = {c["id"]: c for c in data["connections"]
                if c["status"] != "revoked"}
        # Every live connection's tenant, whether or not an assistant reads
        # it: deleting an assistant leaves its requests waiting (QA on
        # #843), and counting only assistants' tenants read that as zero.
        by_conn = {}
        for a in data["agents"]:
            by_conn.setdefault(a["connection_id"], a)
        tenants: dict[str, tuple] = {}
        for cid, conn in live.items():
            if conn["tenant_id"] not in tenants:
                tenants[conn["tenant_id"]] = (by_conn.get(cid), conn)
        # One queue per tenant with something waiting. The approvals page
        # reads one assistant's records, so each queue is its own link: a
        # single link to the first would hide every other queue. A queue
        # with no assistant has no page to link to; the hub says so.
        total, queues = 0, []
        for tenant, (agent, conn) in tenants.items():
            try:
                n = len(hc.pending_actions(tenant))
            except HealthClawError:
                logger.warning("pending count unavailable for account %s",
                               acct.id)
                return jsonify({"error": "unavailable"}), 503
            total += n
            if n and agent:
                queues.append({"agent_id": agent["id"], "name": agent["name"],
                               "count": n,
                               "href": url_for("approvals",
                                               agent_id=agent["id"])})
            elif n:
                # Whether any assistant exists decides the next step: with
                # one, it is switched to these records ("Change records");
                # with none, the hub's Start a chat is the way in (#847).
                queues.append({"name": conn["label"], "count": n,
                               "needs_assistant": True,
                               "has_assistant": bool(data["agents"])})
        out = {"count": total}
        if queues:
            linked = [q for q in queues if q.get("href")]
            if linked:
                out["agent_id"] = linked[0]["agent_id"]
                out["href"] = linked[0]["href"]
            out["queues"] = queues
        # What became of requests already answered. A recent list that
        # cannot be read is said, and never fails the count above: the
        # pending number is the one a person acts on.
        try:
            recent = _recent_outcomes(tenants, bool(data["agents"]))
        except HealthClawError:
            logger.warning("recent outcomes unavailable for account %s",
                           acct.id)
            out["recent_unavailable"] = True
        else:
            if recent:
                out["recent"] = recent
        return jsonify(out)

    #: How many answered requests the hub lists. A week of them is the
    #: engine's window; the hub shows the newest few.
    _RECENT_SHOWN = 5

    def _recent_outcomes(tenants, has_assistant):
        """The hub's "done" and "didn't finish" lines (#847), newest first.

        Read from the engine on every visit and stored nowhere. A completed
        intake form is looked up once more for its PDF link; a lookup that
        fails leaves the line saying done, without a link. Records no
        assistant reads carry the same hint as their pending queue: with an
        assistant elsewhere, the step is to switch it, not to start a chat."""
        now = time.time()
        items = []
        for tenant, (agent, conn) in tenants.items():
            for a in hc.recent_actions(tenant):
                state = hub_view.recent_state(a.get("status"))
                if not state or _failed_long_ago(state, a, now):
                    continue
                item = {"id": a.get("id"), "state": state,
                        "label": _KIND_LABELS.get(a.get("kind"),
                                                  "Request"),
                        "updated_at": a.get("updated_at") or ""}
                # An intake form goes nowhere: only a PDF is made, so naming
                # a recipient read as though it had been sent (#853).
                if a.get("to") and a.get("kind") != "form-fill":
                    item["to"] = a["to"]
                if agent:
                    item["agent_name"] = agent["name"]
                    item["chat"] = url_for("chat", agent=agent["id"])
                else:
                    item["records"] = conn["label"]
                    item["has_assistant"] = has_assistant
                items.append((item, tenant, a.get("kind")))
        items.sort(key=lambda t: t[0]["updated_at"], reverse=True)
        out = []
        for item, tenant, kind in items[:_RECENT_SHOWN]:
            if item["state"] == "done" and kind == "form-fill":
                link = _pdf_link(tenant, item["id"], now)
                if link:
                    item["link"] = link
            out.append(item)
        return out

    def _failed_long_ago(state, action, now):
        """A failed request leaves the hub after a day (#876). It could not
        be cleared, and sat beside the ready form that a retry made. Only
        `failed`: a line that asks the person to check with us stays, and
        so does one we cannot date."""
        if state != "failed":
            return False
        try:
            when = datetime.fromisoformat(
                str(action.get("updated_at") or "").replace("Z", "+00:00"))
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return now - when.timestamp() > 86400

    def _pdf_link(tenant, action_id, now):
        try:
            status = hc.action_status(tenant, action_id)
            outcome = json.loads(status.get("outcome_summary") or "{}")
        except (HealthClawError, ValueError, TypeError):
            return None
        if not isinstance(outcome, dict):
            return None
        return hub_view.live_link(outcome.get("delivery_link"), now)

    # --- review relay (credential-injecting proxy, agent-scoped) -------------

    def _form_past_review(tenant, agent_id, action_id):
        """A form the engine no longer offers for review: ready with its PDF,
        still being made, or done. The PDF link comes from the action's
        outcome, as the post-approve screen reads it (/api/form)."""
        try:
            status = hc.action_status(tenant, action_id)
        except HealthClawError as exc:
            if _engine_said_absent(exc):
                return render_template("chat_error.html",
                                       message="That form isn't yours."), 404
            logger.exception("form status failed for %s", action_id)
            return render_template("chat_error.html",
                                   message=_REVIEW_UNCHECKABLE), 503
        outcome = {}
        try:
            outcome = json.loads(status.get("outcome_summary") or "{}")
        except (TypeError, ValueError):
            pass
        link = outcome.get("delivery_link") if isinstance(outcome, dict) else None
        # Only an http(s) link becomes an href, as on the post-approve screen.
        if not (isinstance(link, str)
                and re.match(r"^https?://", link, re.IGNORECASE)):
            link = None
        state = status.get("status")
        shown = ("ready" if state == "completed" and link else
                 "preparing" if state == "executing" else "done")
        # Corrections and other requests pass this way too; only a form
        # (the intake rail, or an engine that names no kind) is "your form".
        is_form = status.get("kind") in (None, "form-fill")
        return render_template("form_done.html", state=shown, link=link,
                               agent_id=agent_id, is_form=is_form)

    def _agent_owns_action(agent_id, action_id):
        """The tenant that owns this action, or None if it is not this
        agent's.

        Raises OwnershipUnknown when the engine could not be asked. An
        outage is not evidence about ownership, and this is the
        human-approval path — the guardrail the product exists to
        guarantee (#410).
        """
        acct = current_account()
        ctx = _live_agent_context(acct, agent_id)
        if not ctx:
            return None
        try:
            hc.action_status(ctx["tenant"], action_id)
        except HealthClawError as exc:
            if _engine_said_absent(exc):
                return None
            raise OwnershipUnknown from exc
        return ctx["tenant"]

    # Both review routes deny with 404 and stall with 503. Saying "that form
    # isn't yours" because we could not reach the engine is a confident false
    # statement about ownership.
    #
    # "Nothing has been approved" survives here and ONLY here, because every
    # site that uses this string failed BEFORE any review POST went out: the
    # ownership pre-check on either route, and the review GET, which has no
    # side effect at all. Nothing was submitted, so nothing could have been
    # recorded. The paragraph this replaced said the claim was "true in every
    # failure mode here" and the submit route used it too — see below.
    _REVIEW_UNCHECKABLE = ("We couldn't check this form right now. Nothing "
                           "has been approved — please try again in a moment.")
    #: Every way the review POST can fail to produce a readable answer, in one
    #: sentence, because the patient is in the same position in all three:
    #: transport loss, a gateway status the engine never wrote, and a 200
    #: nobody could decode. This POST is what MINTS the ActionConfirmation
    #: (#528) — the approval record — so none of the three can say whether an
    #: approval now exists, in either direction.
    #:
    #: It replaces `_REVIEW_UNSUBMITTED`, which asserted "Nothing has been
    #: approved" on the first two. That sentence answered a question the
    #: `confirmed` field answers ("was it carried out": no, and knowably so —
    #: `confirm_action` is reached only on a decodable 200) with words that
    #: state a different one ("is your approval on file": unknown). One
    #: control, two meanings, which is the shape docs/2026-08-02-retro.md is
    #: about. It also warned that "approving twice could send it twice",
    #: deterring the recovery with a claim that is false: once a confirmation
    #: exists the review route answers 409, the payload seal refuses the
    #: resubmit that races that pre-check, and the claim transition into
    #: `executing` has a single winner. Verified against a running engine.
    _REVIEW_UNRESOLVED = ("We couldn't tell whether your review was saved. "
                          "Reload this page to see where this request stands "
                          "before approving again.")

    @app.get("/review/<agent_id>/<action_id>")
    @login_required
    def review(agent_id, action_id):
        try:
            tenant = _agent_owns_action(agent_id, action_id)
        except OwnershipUnknown:
            return render_template("chat_error.html",
                                   message=_REVIEW_UNCHECKABLE), 503
        if not tenant:
            return render_template("chat_error.html",
                                   message="That form isn't yours."), 404
        if svc.is_paused(current_account().id):
            # Pause stops approvals too (beta spec 4.6, #856 review F1).
            return render_template("chat_error.html",
                                   message=beta.PAUSED_HUB_TEXT), 423
        try:
            status, html = hc.fetch_review_page(tenant, action_id)
        except HealthClawError:
            # A dead socket reached Flask as a 500 before this: no statement
            # about the form, on the approval gate.
            logger.exception("review fetch failed for %s", action_id)
            return render_template("chat_error.html",
                                   message=_REVIEW_UNCHECKABLE), 503
        if status != 200:
            # Only the engine's own 4xx is an answer about this form. A 5xx,
            # 408 or 429 is a gateway speaking for an engine that said
            # nothing, and "no longer awaiting review" would be a claim about
            # state this branch never observed — the #416 shape, one route
            # over. A patient with a live pending request must not be told it
            # is gone because a proxy timed out.
            if not HealthClawClient._answered_about_data(status):
                logger.warning("review fetch unanswered (%s) for %s",
                               status, action_id)
                return render_template("chat_error.html",
                                       message=_REVIEW_UNCHECKABLE), 503
            # Past review. The texted "Your intake form is ready" link lands
            # here after approval, so show the form, not a dead end (#875).
            return _form_past_review(tenant, agent_id, action_id)
        html = html.replace(f"/r6/actions/{action_id}/review",
                            f"/review/{agent_id}/{action_id}/submit")
        # The engine names its own product in the tab; here the page is
        # CareAgents', reached from a CareAgents chat or text.
        return _REVIEW_TAB_BRAND.sub(r"\1CareAgents\2", html, count=1)

    def _count_approval(agent_id):
        """One approval on a real-record assistant, for the weekly number
        (beta spec 4.5). The sample is not counted. Never fails a review."""
        acct = current_account()
        try:
            ctx = svc.get_agent_context(acct.id, agent_id)
            if ctx and ctx["connection"]["kind"] != "sample":
                svc.count_activity(acct.id, "approved")
        except Exception:  # noqa: BLE001 - a count never fails a review
            logger.warning("could not count an approval")

    @app.post("/review/<agent_id>/<action_id>/submit")
    @login_required
    def review_submit(agent_id, action_id):
        try:
            tenant = _agent_owns_action(agent_id, action_id)
        except OwnershipUnknown:
            return jsonify({"error": "review_unavailable",
                            "message": _REVIEW_UNCHECKABLE}), 503
        if not tenant:
            return jsonify({"error": "not yours"}), 404
        if svc.is_paused(current_account().id):
            # Before the review is submitted, so nothing is confirmed or
            # executed for a paused account (beta spec 4.6, #856 review F1).
            return jsonify({"error": "records_paused",
                            "message": beta.PAUSED_HUB_TEXT}), 423
        decisions = request.get_json(silent=True) or dict(request.form)
        try:
            status, body = hc.submit_review(tenant, action_id, decisions)
        except HealthClawUnconfirmed:
            # Something answered the review POST and the answer was not
            # readable. Caught FIRST because it is a subclass.
            #
            # `null` rather than the `false` below, and the difference is not
            # about what was executed — no confirm went out on any of these
            # three paths. It is about which answer the page should act on: a
            # 200 means the submit reached something that accepted it, so a
            # status lookup describes the request the patient just acted on,
            # and null is the value that routes to the branch which performs
            # one (#220). The two below have no evidence anything was
            # delivered; polling there would report on a request that may
            # never have been made, so they keep Approve armed instead.
            logger.exception("review submit unreadable for %s", action_id)
            return jsonify({
                "error": "review_unavailable",
                "confirmed": None,
                "message": _REVIEW_UNRESOLVED,
            }), 502
        except HealthClawError:
            # The decisions are gone and nobody told the patient. Ownership
            # was already confirmed, so this is not a permission answer.
            #
            # `confirmed: False` is right and stays: the confirm is only
            # reached on a decodable 200, so the action was not carried out,
            # and False is what leaves Approve armed — which on this path is
            # the recovery, not a hazard. What was wrong was the SENTENCE:
            # `_send` raises this for a read timeout as readily as for a
            # refused connection, so the POST may have been delivered,
            # recorded and minted before the answer was lost.
            logger.exception("review submit failed for %s", action_id)
            return jsonify({
                "error": "review_unavailable",
                "confirmed": False,
                "message": _REVIEW_UNRESOLVED,
            }), 503
        if status and not HealthClawClient._answered_about_data(status) \
                and status != 200:
            # A gateway spoke for an engine that said nothing. This arm used
            # to reason: "we cannot say the review was saved, but we CAN say
            # nothing was approved, because confirm_action is only reached on
            # a 200 below". The first half of that is right and the second
            # half is the same defect as the unreadable-200 one above, one
            # branch over: what confirm_action reaches is EXECUTION. The
            # approval record is minted by the review POST itself (#528), and
            # a 5xx is delivered-or-not — so an approval may exist for a
            # request this arm told the patient had none. `confirmed: False`
            # still states the half that is known.
            logger.warning("review submit unanswered (%s) for %s",
                           status, action_id)
            return jsonify({
                "error": "review_unavailable",
                "confirmed": False,
                "message": _REVIEW_UNRESOLVED,
            }), 503
        if status == 200:
            try:
                hc.confirm_action(tenant, action_id)
            except HealthClawUnconfirmed:
                # The third answer (#220). The engine never replied, so the
                # action may already be running. "Nothing has been sent — try
                # again" would be a claim we did not observe, and acting on it
                # is how a prescription request gets sent twice. `confirmed` is
                # null: not true, not false, not knowable from here.
                logger.exception("confirm unanswered after review for %s",
                                 action_id)
                body = dict(body) if isinstance(body, dict) else {}
                body.update({
                    "confirmed": None,
                    # "approving twice could send it twice" was the last false
                    # deterrent in this file, and the third instance of one
                    # shape: two rounds fixed the sentence in front of them
                    # and left the twin one arm over. Since #550 a second
                    # approval reaches the REVIEW route, which answers 409
                    # while the action is still awaiting confirmation and 404
                    # once it has moved on; `confirm_action` is called only on
                    # a decodable 200, so it is never reached twice and there
                    # is no second send to warn about. What is true is that we
                    # did not retry it ourselves, and that the page is about
                    # to look up where the request stands.
                    "message": ("Your review was saved. We couldn't tell "
                                "whether your approval went through, so we "
                                "have not tried it again. We are checking "
                                "where this request stands."),
                })
                return jsonify(body), 502
            except HealthClawError:
                # The review was recorded but the confirmation didn't land, so
                # the action is still sitting unexecuted. Swallowing this told
                # the person they'd approved something that would never happen.
                #
                # `confirmed` stays False, and that is not a formality: what
                # reaches THIS branch is a failed approval-token mint (the
                # confirm never went out) or a 4xx the engine itself answered.
                # Every ambiguous case — transport loss on the confirm POST, a
                # gateway 5xx/408/429, an undecodable 200 — is routed to
                # HealthClawUnconfirmed above and answers `null` instead
                # (careagents/healthclaw.py:352-357, :387-397). The action did
                # not execute, and that is KNOWN, so `null` here would assert
                # an uncertainty that does not exist — #220's third answer
                # collapsed from the other side.
                #
                # The INSTRUCTION was the defect. This said "Nothing has been
                # sent — please try approving again". Since #528 sealed the
                # payload, the review route mints a confirmation on the first
                # submit, so that retry answers 409 and the page tells them
                # they have already approved: the patient was directed into a
                # loop the page itself creates. "Nothing has been sent" also
                # reads as "start over" when an approval IS on file.
                #
                # So: say what is known — saved, recorded, not completed — say
                # plainly that re-approving here will not resend it, and point
                # at the status rather than back at the button.
                logger.exception("confirm failed after review for %s", action_id)
                body = dict(body) if isinstance(body, dict) else {}
                body.update({
                    "confirmed": False,
                    "message": ("Your review was saved and your approval is "
                                "recorded, but we could not complete it just "
                                "now. Approving again here will not resend "
                                "it. Check where this request stands."),
                })
                return jsonify(body), 502
            body = dict(body) if isinstance(body, dict) else {}
            body["confirmed"] = True
            _count_approval(agent_id)
        return jsonify(body), status

    @app.post("/review/<agent_id>/<action_id>/decline")
    @login_required
    def review_decline(agent_id, action_id):
        """The person read the form and said no (#520). Recorded as a
        decline, never as a timeout, on the same credential as Approve."""
        try:
            tenant = _agent_owns_action(agent_id, action_id)
        except OwnershipUnknown:
            return jsonify({"error": "review_unavailable",
                            "message": _REVIEW_UNCHECKABLE}), 503
        if not tenant:
            return jsonify({"error": "not yours"}), 404
        try:
            status, body = hc.decline_action(tenant, action_id)
        except HealthClawError:
            logger.exception("decline failed for %s", action_id)
            return jsonify({
                "error": "review_unavailable",
                "declined": None,
                "message": ("We couldn't record your answer just now. "
                            "Reload this page to see where this request "
                            "stands."),
            }), 503
        body = dict(body) if isinstance(body, dict) else {}
        if status == 200:
            body["declined"] = True
            return jsonify(body), 200
        if HealthClawClient._answered_about_data(status):
            # The engine answered: already approved, lapsed, or not ours.
            # The page reads the status to say which.
            body.setdefault("error", "not_declined")
            body["declined"] = False
            return jsonify(body), status
        return jsonify({
            "error": "review_unavailable",
            "declined": None,
            "message": ("We couldn't tell whether your answer was recorded. "
                        "Reload this page to see where this request stands."),
        }), 503

    # --- surfaces ------------------------------------------------------------

    @app.post("/api/surfaces/telegram")
    @login_required
    def connect_telegram():
        acct = current_account()
        body = request.get_json(silent=True) or {}
        agent_id = body.get("agent_id", "")
        ctx = svc.get_agent_context(acct.id, agent_id)
        if not ctx:
            return jsonify({"error": "unknown agent"}), 404
        code = new_binding_code()
        sid = svc.add_surface(acct.id, agent_id, "telegram", code,
                              status="pending")
        deep = (f"https://t.me/{cfg.telegram_bot}?start=care_{code}"
                if cfg.telegram_bot else None)
        return jsonify({"id": sid, "code": code, "deep_link": deep})

    @app.post("/api/surfaces/telegram/bind")
    def telegram_bind():
        """Called by the OpenClaw bot's /start handler with the code + chat_id.
        Gated by the mint secret (server-to-server)."""
        if not _relay_secret_ok():
            return jsonify({"error": "forbidden"}), 403
        body = request.get_json(silent=True) or {}
        code = str(body.get("code") or "").replace("care_", "")
        chat_id = body.get("chat_id")
        surface = svc.find_surface_by_code(code)
        if not surface or chat_id is None:
            return jsonify({"error": "unknown code"}), 404
        ctx = svc.get_agent_context(surface["account_id"], surface["agent_id"])
        if not ctx or not hc.bind_telegram(ctx["tenant"], int(chat_id)):
            return jsonify({"error": "bind failed"}), 502
        svc.bind_surface(surface["id"], str(chat_id))
        return jsonify({"ok": True})

    # --- iMessage surface ----------------------------------------------------
    # Unlike Telegram (driven by the OpenClaw gateway), careagents runs the
    # iMessage message loop itself. careagents.imessage.handle_inbound is the
    # transport-agnostic core; the mint-secret-gated routes below are the
    # Mac-mini relay's adapter onto it.

    def _imessage_admission_block(account_id: str, ctx: dict) -> str | None:
        # The same admission rule as /api/chat: a turn the worker will
        # refuse (paused, terms) is queued and answered there; otherwise the
        # day's allowance is read here so a spent day costs no run.
        if beta.turn_block(ctx["connection"], svc.is_paused(account_id),
                           tester_terms.CONSENT_VERSION):
            return None
        if svc.daily_turns_used(account_id) >= cfg.chat_turns_per_day:
            return beta.DAILY_LIMIT_TEXT
        return None

    def _imessage_queue_turn(ctx, text, request_id, conversation_id):
        tenant, agent = ctx["tenant"], ctx["agent"]
        conversation_id = conversation_id or hc.conversation_id(agent["id"])
        created, user_message_id = hc.claim_inbound_message(
            tenant, text, agent["id"], conversation_id, "imessage",
            str(request_id or uuid.uuid4()))
        if created is None or not user_message_id:
            raise HealthClawError("message store unavailable")
        run = hc.create_agent_run(
            tenant, user_message_id, cfg.run_deadline_seconds)
        return {**run, "duplicate": not created}

    def _relay_secret_ok() -> bool:
        """X-Internal-Secret, compared in constant time."""
        given = request.headers.get("X-Internal-Secret") or ""
        return secret_matches(given, cfg.mint_secret)

    def _imessage_connected(account_id: str, handle: str) -> None:
        """Tell the owner by email that a phone was connected. One line, the
        handle masked. Skipped quietly when email is not set up."""
        if not cfg.resend_api_key:
            return
        acct = svc.get_account(account_id)
        if acct is None:
            return
        try:
            mail.send_notice(cfg, acct.email,
                             "A phone was connected to CareAgents",
                             imessage.connected_notice(handle))
        except Exception:               # pragma: no cover - defensive
            logger.warning("connected notice failed to send")

    def _imessage_reverify(account_id: str, handle: str) -> None:
        """Tell the owner, once, that their phone was asked to re-confirm
        (#871). Masked, and skipped quietly when email is not set up."""
        if not cfg.resend_api_key:
            return
        acct = svc.get_account(account_id)
        if acct is None:
            return
        try:
            mail.send_notice(cfg, acct.email, imessage.REVERIFY_SUBJECT,
                             imessage.reverify_notice(handle))
        except Exception:               # pragma: no cover - defensive
            logger.warning("reverify notice failed to send")

    def _imessage_pending_count(ctx: dict) -> int:
        # The approvals page's rule (#215): a revoked connection is not a
        # pathway to the tenant's requests, so it is not asked. Raised as
        # "could not check", never answered as zero.
        if not _connection_is_live(ctx):
            raise HealthClawError("connection revoked", 0)
        return len(hc.pending_actions(ctx["tenant"]))

    imessage_deps = imessage.Deps(
        origin=cfg.origin, svc=svc,
        workers_ready=lambda: _worker_state(
            timeout=_ADMISSION_WORKER_TIMEOUT) == WORKERS_READY,
        allow_turn=lambda account_id: _allow_turn(account_id),
        admission_block=_imessage_admission_block,
        queue_turn=_imessage_queue_turn,
        queue_error=HealthClawError,
        burst_window_seconds=cfg.chat_window_seconds,
        on_connected=_imessage_connected,
        reverify_seconds=cfg.imessage_reverify_days * 86400,
        on_reverify=_imessage_reverify,
        # APPROVALS by text: the approvals page's own source and its rule —
        # an engine that cannot answer raises, never reads as zero (#215).
        pending_count=_imessage_pending_count)
    # A second transport (a hosted provider's webhook) calls the same core.
    app.extensions["careagents_imessage"] = imessage_deps
    sendblue_surface.register(app, cfg, svc, imessage_deps)

    def _imessage_json_object() -> dict | None:
        """The request's JSON object, {} when there is no body at all, or
        None for anything else. `get_json(silent=True) or {}` read null,
        [], 0, false, "" and malformed JSON as {}, and on Disconnect {}
        meant every phone."""
        body = request.get_json(silent=True)
        if body is None:
            return None if request.get_data(cache=True) else {}
        return body if isinstance(body, dict) else None

    @app.post("/api/surfaces/imessage")
    @login_required
    def connect_imessage():
        acct = current_account()
        body = _imessage_json_object()
        if body is None:
            return jsonify({"error": "body must be a JSON object"}), 400
        agent_id = body.get("agent_id", "")
        if not svc.get_agent_context(acct.id, agent_id):
            return jsonify({"error": "unknown agent"}), 404
        code = new_binding_code()
        sid = svc.add_surface(acct.id, agent_id, "imessage", code,
                              status="pending")
        return jsonify({"id": sid, "code": code,
                        "handle": cfg.imessage_handle,
                        "instructions": (
                            f"Text  care {code}  to {cfg.imessage_handle}"
                            if cfg.imessage_handle else
                            "iMessage isn't available yet.")})

    @app.post("/api/surfaces/imessage/disconnect")
    @login_required
    def disconnect_imessage():
        """Settings' Disconnect: unbind one phone ({surface_id}), or every
        iMessage handle on the account only when asked in so many words
        ({"all": true}). Anything else is a 400, never "all". A freed phone
        may text again later and get a fresh sign-in link."""
        acct = current_account()
        body = _imessage_json_object()
        if body is None:
            return jsonify({"error": "body must be a JSON object"}), 400
        surface_id = body.get("surface_id")
        if surface_id is not None and not isinstance(surface_id, str):
            return jsonify({"error": "invalid surface_id"}), 400
        if surface_id is None and body.get("all") is not True:
            return jsonify({"error": "name a surface_id, or all: true"}), 400
        removed = svc.disconnect_imessage(acct.id, surface_id)
        if surface_id is not None and not removed:
            return jsonify({"error": "unknown phone"}), 404
        return jsonify({"ok": True, "removed": removed})

    @app.post("/api/surfaces/imessage/bind")
    def imessage_bind():
        """An older relay calls this for `care <code>`. Inbound handles the
        same line now; kept so a relay not yet updated keeps pairing."""
        if not _relay_secret_ok():
            return jsonify({"error": "forbidden"}), 403
        body = _imessage_json_object()
        if body is None:
            return jsonify({"error": "body must be a JSON object"}), 400
        code = str(body.get("code") or "").replace("care_", "").replace(
            "care ", "").strip().lower()
        raw = str(body.get("handle") or "").strip()
        if not raw:
            return jsonify({"error": "missing handle"}), 400
        handle = imessage.normalize_handle(raw)
        if not handle:
            # The same rule as inbound: nothing we cannot text back is bound.
            return jsonify({"error": "unsupported handle"}), 400
        reply, status = imessage.bind_by_code(imessage_deps, handle, code,
                                              raw=raw)
        return jsonify(reply), status

    @app.post("/api/surfaces/imessage/inbound")
    def imessage_inbound():
        """Relay POSTs an inbound message {handle, text}. Answers
        {reply?, run_id?}: send `reply` now if present, then poll `run_id`
        for the agent's answer."""
        if not _relay_secret_ok():
            return jsonify({"error": "forbidden"}), 403
        body = _imessage_json_object()
        if body is None:
            return jsonify({"error": "body must be a JSON object"}), 400
        request_id = body.get("request_id")
        conversation_id = body.get("conversation_id")
        if request_id is not None and not 1 <= len(str(request_id)) <= 128:
            return jsonify({"error": "invalid request_id"}), 400
        reply, status = imessage.handle_inbound(
            imessage_deps, str(body.get("handle") or ""),
            str(body.get("text") or ""),
            request_id=str(request_id) if request_id else None,
            conversation_id=conversation_id)
        return jsonify(reply), status

    @app.get("/link")
    def imessage_link():
        """The sign-in link texted to a new handle, `/link?t=<token>`. The
        token is in the query string so the access log, which records the
        path only, never holds it. Parks the link's id in the session, then
        the ordinary sign-in (or sign-up) runs; the handle is bound once
        someone signed in says yes."""
        token = str(request.args.get("t") or "")
        link_id = (svc.peek_imessage_link(token)
                   if 0 < len(token) <= 64 else None)
        if not link_id:
            return render_template("imessage_link.html", outcome="expired"), 410
        session["imessage_link"] = link_id
        if current_account() is None:
            return redirect(url_for("auth"))
        return redirect(url_for("imessage_link_claim"))

    @app.route("/link/done", methods=["GET", "POST"])
    @login_required
    def imessage_link_claim():
        """GET asks, POST binds. A link can be forwarded, so whoever opens
        it must see which phone they are connecting and say yes; otherwise
        a stranger's number could be tied to their records unnoticed."""
        if request.method == "GET":
            link_id = session.get("imessage_link")
            if not link_id:
                return redirect(url_for("home"))
            handle = svc.imessage_link_handle(link_id)
            if not handle:
                session.pop("imessage_link", None)
                return render_template("imessage_link.html",
                                       outcome="expired"), 410
            # Already this account's phone: the link is a re-confirm.
            bound = svc.find_surface_by_handle(handle)
            again = bool(bound and bound["account_id"]
                         == current_account().id)
            return render_template("imessage_link.html", outcome="confirm",
                                   again=again,
                                   handle=imessage.display_handle(handle))
        link_id = session.pop("imessage_link", None)
        if not link_id:
            return redirect(url_for("home"))
        if request.form.get("connect") != "yes":
            handle = svc.imessage_link_handle(link_id)
            # Spent, so the same link opened again reads as used.
            svc.void_imessage_link(link_id)
            # The owner says their own phone isn't theirs any more (#871):
            # disconnect it as Settings does. Only this account's binding.
            bound = svc.find_surface_by_handle(handle) if handle else None
            if bound and bound["account_id"] == current_account().id:
                svc.disconnect_imessage(bound["account_id"], bound["id"])
                return render_template("imessage_link.html",
                                       outcome="disconnected")
            return render_template("imessage_link.html", outcome="declined")
        acct = current_account()
        handle = svc.imessage_link_handle(link_id)
        outcome = svc.claim_imessage_link(link_id, acct.id)
        if outcome == "connected":
            if handle:
                _imessage_connected(acct.id, handle)
            if not svc.list_home(acct.id)["agents"]:
                outcome = "connected_setup"
        return render_template("imessage_link.html", outcome=outcome)

    @app.get("/api/surfaces/imessage/runs/<run_id>")
    def imessage_run_result(run_id):
        """Mint-secret-gated projection polled by the Mac relay."""
        if not _relay_secret_ok():
            return jsonify({"error": "forbidden"}), 403
        # A header, not the query string: the access log keeps neither,
        # but a URL travels further than a header does. The query form is
        # still read for a relay not yet updated.
        raw = str(request.headers.get("X-Imessage-Handle")
                  or request.args.get("handle") or "").strip()
        surface = svc.find_surface_by_handle(
            imessage.normalize_handle(raw) or raw, kind="imessage", also=raw)
        if (not surface or not surface.get("agent_id")
                or imessage.reverify_due(surface, time.time(),
                                         imessage_deps.reverify_seconds)):
            # Due to re-confirm (#871): read as unbound, so the relay sends
            # nothing to whoever holds the number now.
            return jsonify({"error": "unbound handle"}), 404
        ctx = svc.get_agent_context(surface["account_id"], surface["agent_id"])
        if not ctx:
            return jsonify({"error": "unknown agent"}), 404
        try:
            run = hc.get_agent_run(ctx["tenant"], run_id)
            if not _run_belongs_to(run, ctx["tenant"], surface["agent_id"]):
                return jsonify({"error": "unknown run"}), 404
            page = hc.agent_run_events(
                ctx["tenant"], run_id, after=0, limit=500)
        except HealthClawError as exc:
            if _engine_said_absent(exc):
                return jsonify({"error": "unknown run"}), 404
            # The relay retries on 503. Answering 404 retired the run as
            # non-existent and dropped the patient's reply (#410).
            return jsonify({"error": "run service unavailable"}), 503
        if page.get("status") not in imessage.FINAL_STATUSES:
            return jsonify({"run_id": run_id,
                            "status": page.get("status")}), 202
        reply = imessage.run_reply(page.get("events") or [], cfg.origin,
                                   surface["agent_id"])
        return jsonify({"run_id": run_id, "status": page.get("status"),
                        "reply": reply})

    # --- trust + ops ---------------------------------------------------------

    #: How long one read of the engine's badge answers for. /safety and
    #: /api/trust are public, so each load was an engine call (#884
    #: security). Per process; the engine keeps its own longer cache.
    BADGE_TTL = 120
    badge_cache = {"at": None, "message": "unavailable"}
    badge_lock = threading.Lock()

    def _badge_message() -> str:
        """The engine's badge message, read at most once per BADGE_TTL. An
        unreachable engine is cached too: an outage is when re-asking on
        every load hurts most."""
        now = time.time()
        with badge_lock:
            at = badge_cache["at"]
            if at is not None and now - at < BADGE_TTL:
                return badge_cache["message"]
        try:
            message = hc.conformance_badge().get("message", "unavailable")
        except HealthClawError:
            # The badge is a claim about the engine. Unreachable means we do
            # not have one — the same honest answer a non-200 already gives,
            # rather than a 500 on the trust panel (#403).
            message = "unavailable"
        message = str(message or "unavailable")
        with badge_lock:
            badge_cache.update(at=now, message=message)
        return message

    @app.get("/api/trust")
    def trust():
        return jsonify({"badge": _badge_message()})

    @app.get("/safety")
    def safety():
        """What the safety grade means, in plain words (#884 G7). Public,
        like the landing page that links it. The grade is the engine's,
        read as /api/trust reads it; without one, the page links the live
        report instead of showing a grade nobody fetched."""
        message = _badge_message()
        grade = message.split(" ")[0] if message else ""
        # Back where the person came from (#884 G7): their chat when they
        # have an assistant, the hub when they have none, the landing page
        # when signed out. Reading the session sets no cookie.
        acct = current_account()
        agents = svc.list_home(acct.id)["agents"] if acct else []
        if agents:
            back = (url_for("chat", agent=agents[0]["id"]), "Back to chat")
        elif acct:
            back = ("/home", "Back to CareAgents")
        else:
            back = ("/", "Back")
        return render_template(
            "safety.html", back_href=back[0], back_label=back[1],
            grade=grade if grade in ("A", "B", "C", "D", "F") else None,
            report_url=(cfg.healthclaw_public_base.rstrip("/")
                        + "/r6/fhir/$conformance?format=text"))

    @app.get("/manifest.webmanifest")
    def manifest():
        return jsonify({
            "name": "CareAgents", "short_name": "CareAgents",
            "start_url": "/home", "display": "standalone",
            "background_color": "#FBF6EE", "theme_color": "#C2532E",
            "icons": [{"src": "/static/icon.svg", "sizes": "any",
                       "type": "image/svg+xml"}]})

    @app.get("/healthz")
    def healthz():
        """Readiness, not liveness: reports 503 when the account store is
        unreachable.

        This used to hard-code accounts=True, which meant a container that
        could not reach its database still advertised itself as healthy — a
        load balancer would route real sign-ins straight into failure. It now
        round-trips a trivial query so the answer reflects reality.

        Readiness has two inputs and they are NOT symmetric (#219). The
        account store is ours, so it gates the status code outright. The run
        workers are HealthClaw's, so what gates the status code is their
        ANSWER, not our ability to get one:

        - Confirmed absent — HealthClaw answered, and no worker presence is
          fresh. That deployment serves pages and cannot finish a chat turn.
          It is the web-only deployment the durable-worker runbook tells
          operators to expect a 503 for, and it stays a 503 deliberately: on
          Railway the health check runs at the start of a deploy, and this is
          a deploy that must not go live.
        - Refused — HealthClaw answered 4xx. It understood the request and
          rejected something about it: this container's mint secret, or the
          base URL it was pointed at. That is a fault in the artifact being
          deployed, exactly like a missing worker, so it gates the deploy the
          same way. Reported as `run_workers_state: "rejected"`. Splitting
          this out of "unknown" is the difference between a deploy that is
          blocked and a chat-dead deployment that goes live: measured, a
          wrong mint secret answers 403 and a wrong base path answers 404.
        - Could not ask, inside `_HEALTHZ_WORKER_TIMEOUT` — that is a fact
          about HealthClaw, not about this container. Reported as
          `run_workers_state: "unknown"`; it does not fail the endpoint.
          Failing it would block a CareAgents deploy on someone else's
          outage, including the deploy that fixes it, while traffic stays on
          a previous version with exactly the same dependency. The probe
          measured the old shape taking 25.01s to answer 503 with nothing
          running at all (evidence §8, PR #573). A transport failure, a 5xx
          and a proxy interstitial all land here, because none of them is
          HealthClaw telling us something about ourselves.

        Admission is unchanged, and it is the gate that protects people:
        `POST /api/chat` still refuses a turn in ALL THREE failure states, so
        nothing routes anyone into a turn no worker can run.

        It also reports which build is running (#258). That is telemetry, not
        a gate: an absent marker reports "unknown" and changes nothing about
        the status code.

        Callers that only need "is this process up" (a boot gate, a restart
        probe) must not use this endpoint — it answers for the whole system,
        including dependencies it does not control.
        """
        accounts_ok = svc.ping()
        workers_state = _worker_state(timeout=_HEALTHZ_WORKER_TIMEOUT)
        # `run_workers` stays a boolean readiness VERDICT, not a claim about
        # the workers: false is correct when we could not confirm them, and
        # two runbooks plus the container-roles test read it as a bool.
        # `run_workers_state` carries the claim the bool cannot make (#410) —
        # and now also the difference the status code turns on.
        workers_ok = workers_state == WORKERS_READY
        ready = accounts_ok and workers_state not in WORKERS_OUR_FAULT
        body = {"status": "ok" if ready else "degraded",
                "provider": cfg.provider, "accounts": accounts_ok,
                "run_workers": workers_ok,
                "run_workers_state": workers_state,
                "build": cfg.build_sha, "built_at": cfg.build_time}
        return jsonify(body), (200 if ready else 503)

    # --- reading the counter -------------------------------------------------

    @app.cli.command("page-views")
    @click.option("--days", default=7, show_default=True,
                  help="How many UTC days back to print.")
    def _page_views(days):
        """Print the public-page view counts this app has recorded.

        A command rather than a route on purpose: the numbers are for the
        operator, and a new endpoint is a new thing to authorise, rate-limit
        and get wrong.
        """
        rows = analytics.counts(svc.session, days=days)
        if not rows:
            click.echo("no page views recorded"
                       + ("" if cfg.analytics_enabled
                          else " (CARE_ANALYTICS is not set)"))
            return
        for day, endpoint, views in rows:
            click.echo(f"{day}  {endpoint:<12} {views}")

    # --- real-record invites (beta pathway spec section 4.2) ---------------

    @app.cli.group("invites")
    def _invites():
        """Invite, list and revoke real-record testers.

        Invites are read only when CARE_REAL_RECORDS=allowlist, alongside
        CARE_REAL_RECORDS_ALLOWLIST. A command for the same reason as
        page-views: no admin page to authorise.
        """

    @_invites.command("add")
    @click.argument("email")
    @click.option("--by", "invited_by", required=True,
                  help="Who is inviting, for the record.")
    def _invites_add(email, invited_by):
        try:
            created = svc.invite_real_records(email, invited_by)
        except (AuthError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"invited {email.strip().lower()}" if created
                   else f"{email.strip().lower()} was already invited")
        if cfg.real_records != "allowlist":
            click.echo(f"note: CARE_REAL_RECORDS is {cfg.real_records!r}; "
                       "invites are read only in 'allowlist' mode")
        if not tester_terms.approved():
            click.echo("note: invites are not honoured until the tester "
                       "terms are approved (#565)")

    @_invites.command("revoke")
    @click.argument("email")
    def _invites_revoke(email):
        """Refuse new real connections. Existing ones keep working until the
        person disconnects them."""
        try:
            revoked = svc.revoke_real_records_invite(email)
        except AuthError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"revoked {email.strip().lower()}" if revoked
                   else f"no live invite for {email.strip().lower()}")

    @_invites.command("list")
    def _invites_list():
        rows = svc.real_record_invites()
        if not rows:
            click.echo("no invites")
        for r in rows:
            when = datetime.fromtimestamp(r["invited_at"], timezone.utc)
            state = ("revoked " + datetime.fromtimestamp(
                r["revoked_at"], timezone.utc).strftime("%Y-%m-%d")
                     if r["revoked_at"] else "live")
            click.echo(f"{r['email']:<40} {when:%Y-%m-%d} "
                       f"by {r['invited_by']:<20} {state}")

    # --- pause and the weekly number (beta spec 4.5, 4.6) ------------------

    operator_cli.register(app, svc)
    beta_signup.register(app, svc, cfg)
    feedback.register(app, svc, cfg)

    return app
