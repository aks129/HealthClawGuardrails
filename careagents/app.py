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

import ipaddress
import json
import logging
import math
import re
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote

import click
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for)
from markupsafe import Markup, escape
from sqlalchemy.exc import SQLAlchemyError

from careagents.accounts import (RESEND_COOLDOWN, AccountService, AuthError,
                                 MailError, MailUnconfirmed,
                                 new_binding_code, secret_matches)
from careagents import advisors, analytics, connectors, consent, mail
from careagents import beta, imessage, operator_cli, tester_terms
from careagents import security_headers, sendblue_surface
from careagents import brief as brief_mod
from careagents import hub as hub_view
from careagents import intake_state
from careagents import labs_timeline as labs_timeline_mod
from careagents import beta_signup, causes, errors, feedback, funnel, invites
from careagents import checkin_wallet, text_self
from careagents import checkins
from careagents.agent import GENERIC_FAILURE_TEXT
from careagents.config import Config
from careagents.healthclaw import (HealthClawClient, HealthClawError,
                                   HealthClawUnconfirmed, acting_for)
from careagents.personas import DEFAULT_PERSONA, PERSONAS

logger = logging.getLogger(__name__)

#: Session key: this session was opened by a beta confirm link (_first_run),
#: not by a code or passkey. Limited (full_sign_in_required) and ended once
#: the account has real records (login_required), #920 F1.
_VIA_LINK = "via_link"
#: Session key: the page a signed-out person asked for, so the sign-in that
#: follows ends there and not on the hub (beta feedback, 2026-10-22).
_AFTER_SIGN_IN = "after_sign_in"
#: When it was parked. Honoured for _AFTER_SIGN_IN_TTL seconds only, so a
#: page parked for one sign-in never takes over a hub visit later (#1016).
_AFTER_SIGN_IN_AT = "after_sign_in_at"
_AFTER_SIGN_IN_TTL = 15 * 60
#: One-shot: a parked page was dropped for its age, so the hub says why it
#: opened instead (patient walk of #1016).
_AFTER_SIGN_IN_STALE = "after_sign_in_stale"
#: Longer than any page here; the cookie that carries it is small.
_AFTER_SIGN_IN_MAX = 512


def _local_target(raw) -> str | None:
    """`raw` if it is a path on this site, else None. A path starts with a
    single "/" and holds only visible ASCII: no "//" or "\\" a browser reads
    as another host, no scheme, no whitespace or control character."""
    if (not isinstance(raw, str) or not 1 <= len(raw) <= _AFTER_SIGN_IN_MAX
            or not raw.startswith("/") or raw.startswith("//")
            or "\\" in raw or not all(33 <= ord(c) <= 126 for c in raw)):
        return None
    return raw


def _tapped_target(req) -> str | None:
    """The page as it was tapped, still percent-encoded, so %3F stays part
    of the path rather than coming back as a query. From the server's raw
    request line where it gives one, else Flask's decoded full path."""
    raw = req.environ.get("RAW_URI") or req.environ.get("REQUEST_URI")
    if isinstance(raw, str):
        path = raw.split("?", 1)[0]
        query = req.query_string.decode("latin-1")
        target = _local_target(f"{path}?{query}" if query else path)
        if target:
            return target
    return _local_target(req.full_path.rstrip("?"))


def _parked_state(sess) -> tuple[str | None, bool]:
    """(the parked page if it is on this site and still fresh, whether a
    page on this site was there but too old). Only age makes it "too old":
    a missing or unusable time is no page at all, said nothing about."""
    target = _local_target(sess.get(_AFTER_SIGN_IN))
    at = sess.get(_AFTER_SIGN_IN_AT)
    if (target is None or not isinstance(at, (int, float))
            or isinstance(at, bool)):
        return None, False
    age = time.time() - at
    if 0 <= age <= _AFTER_SIGN_IN_TTL:
        return target, False
    return None, age > _AFTER_SIGN_IN_TTL


def _parked_target(sess) -> str | None:
    """The parked page if it is on this site and still fresh, else None."""
    return _parked_state(sess)[0]


#: What the hub says when HealthClaw cannot be reached to set up or
#: refresh records: a sentence and what to do, never "service unavailable".
#: The burst limit's sentence. chat.js shows its own copy (PACE_TEXT) for
#: a 429 that carries no message; this one is sent only when crisis wording
#: needs the emergency line ahead of it. Pinned equal by a test.
PACE_TEXT = "You've hit the pace limit for now \u2014 give it a few minutes."

RECORDS_DOWN_TEXT = ("We couldn't reach the records service just now. "
                     "Please try again in a minute.")
#: What a link session is told when it reaches for more than the sample.
REAUTH_TEXT = ("For that, sign in again with a code we email you. Your chat "
               "stays as it is.")

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

#: Sign-in code requests one client address may make per window. Sized for
#: a family on one home Wi-Fi, each asking for codes and asking again; the
#: per-address caps in accounts.py protect each inbox.
CODE_REQUESTS_PER_IP = 30
CODE_REQUEST_WINDOW = 600
#: Client addresses the code-request limiter remembers before it forgets
#: the oldest.
CODE_REQUEST_KEYS = 4096


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

#: Consecutive failed run-event reads before the browser stream says the
#: turn failed (#926). A read that met a closing keep-alive connection
#: succeeds on the next try; an engine that is really gone does not.
_REPLAY_MAX_MISSES = 3

# Local hard cap for the file-upload path (#227). The engine's
# `internal/ingest-bundle` is the source of truth for the value; this
# ceiling stops an oversized (or chunked / no-Content-Length) request
# from ever spending more than max_bytes+1 in our process before we
# refuse it. 5 MiB matches the engine default so a request either fails
# here quickly or lands cleanly.
_UPLOAD_MAX_BYTES = 5 * 1024 * 1024

# The footnote under the chat's lab chart. The engine's disclaimer is written
# for a clinician ("Advisory decision support... the performing lab's own
# reference range takes precedence"); this says the same three things in
# plain words (#942). The engine's wording still reaches the model with the
# labs it reads (careagents/agent.py get_labs).
CHART_FOOTNOTE = ("This chart is not a diagnosis. Each result is compared "
                  "with typical adult ranges, and ranges differ by lab, age "
                  "and sex. If your lab report shows a different range, go "
                  "by your lab's range.")
#: Where nothing on the chart was scored (blood pressure: the engine has no
#: range for it), the range sentence above would be untrue (QA #1021).
CHART_FOOTNOTE_UNSCORED = ("This chart is not a diagnosis. These readings "
                           "were not compared with any range; your clinician "
                           "can say what they mean for you.")
#: Scored and unscored series on one card.
CHART_FOOTNOTE_MIXED = (CHART_FOOTNOTE + " Readings shown in grey were not "
                        "compared with any range.")


def _chart_footnote(series: list[dict]) -> str:
    """The footnote that is true of these points: one is scored when it
    carries the engine's flag rather than IND. Decided per point, not per
    series, so a lone grey dot on a ranged series is captioned too."""
    scored = [r.get("flag") != "IND" for s in series for r in s["readings"]]
    if scored and not any(scored):
        return CHART_FOOTNOTE_UNSCORED
    return CHART_FOOTNOTE if all(scored) else CHART_FOOTNOTE_MIXED

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


#: Default names for the assistant of someone the holder cares for, in
#: order; the first one no assistant on the account already has. Fixed
#: words only: an assistant's name is part of the model's prompt, so a
#: nickname is never used (#951 F4).
_CARE_NAMES = ("Rowan", "Hazel", "Linden", "Willow", "Aspen", "Cedar")

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

#: The engine's sample review speaks to the account holder: its banner is
#: the holder's line, and the approved form is "your form ... send it to
#: your new doctor". In the review of someone the holder cares for, these
#: exact strings are reworded here, so the engine never learns a person
#: exists and nothing reaches it (patient test on #952). Each is pinned
#: against the engine's own template by
#: tests/test_careagents_cared_for_wording.py.
_CARED_FOR_REVIEW_WORDS = (
    ("'Confirmed. Your form is ready. '", "'Confirmed. The form is ready. '"),
    ("'Open your form (PDF)'", "'Open the form (PDF)'"),
    ("' to save it, print it or send it to your new doctor.'",
     "' to save it or print it.'"),
)


#: A text (propose_action, or one staged another way) whose number is not
#: the account's verified phone. Said by the relay's 409 and by the review
#: page in place of Approve; Decline still works.
_NOT_YOUR_NUMBER = ("This text is not going to the phone number linked to "
                    "your account, so it can't be approved here. You can "
                    "decline it.")

#: The engine approve page's own markup, each reworded once by the relay
#: for a text to a number that is not the account's. Pinned against
#: templates/action_approve.html by tests/test_careagents_propose_action.py.
_NOT_YOUR_NUMBER_PAGE = (
    ('<div class="form-check mt-2 p-3 bg-body-secondary rounded">',
     '<div class="form-check mt-2 p-3 bg-body-secondary rounded" hidden>'),
    ('<div id="review-gate-msg" class="alert alert-secondary" role="alert">',
     '<div id="review-gate-msg" class="alert alert-warning not-your-number" '
     'role="alert" '
     'data-not-your-number>' + _NOT_YOUR_NUMBER + '</div><div hidden>'),
    ('id="approve-btn" class="btn btn-primary btn-lg" disabled>',
     'id="approve-btn" class="btn btn-primary btn-lg" disabled hidden>'),
)


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


def _landed_of_types(bundle: dict, result: dict, landed: int,
                     types: tuple[str, ...]) -> int:
    """How many of an upload's ingested entries are one of `types`.

    The engine reports one `ingested` total, and an `errors[]` row with the
    `index` of every entry it did not store. Entries without an error row
    landed; of those, count the ones of `types`. Only the resourceType
    string is read — nothing from the record is kept.
    """
    refused = {e.get("index") for e in (result.get("errors") or [])
               if isinstance(e, dict)}
    n = 0
    for idx, entry in enumerate(bundle.get("entry") or []):
        if idx in refused or not isinstance(entry, dict):
            continue
        res = entry.get("resource")
        if isinstance(res, dict) and res.get("resourceType") in types:
            n += 1
    return min(n, landed)


def _nothing_counted_note(bundle: dict, result: dict, landed: int) -> str:
    """Why an upload that landed added no records: the file held only the
    person's own details (Patient), or only kinds the card does not count."""
    if _landed_of_types(bundle, result, landed, ("Patient",)) == landed:
        return "This file had personal details but no health records."
    return "This file had no health records of a kind shown here yet."


def _documents_landed(bundle: dict, result: dict, landed: int) -> int:
    """How many of an upload's ingested entries are uncounted documents."""
    return _landed_of_types(bundle, result, landed,
                            HealthClawClient.UNCOUNTED_TYPES)


def _records_landed(bundle: dict, result: dict, landed: int) -> int:
    """How many of an upload's ingested entries are records, by the rule the
    records card counts with (`record_count`, `COUNTED_TYPES`). The Patient
    is the person, not a record about them, so "4 new records added" sat
    above a card saying 3 (#948)."""
    return _landed_of_types(bundle, result, landed,
                            HealthClawClient.COUNTED_TYPES)


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
    # The resend wait, for the delete dialog's code step to count down as
    # /auth does (10-20 patient walk).
    app.jinja_env.globals["resend_wait"] = RESEND_COOLDOWN
    app.config.update(SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=(cfg.app_env == "production"),
                      PERMANENT_SESSION_LIFETIME=90 * 24 * 3600)
    hc = client or HealthClawClient(cfg.healthclaw_base, cfg.mint_secret,
                                    public_base=cfg.healthclaw_public_base)
    svc = accounts or AccountService(cfg)
    # Spent and expired sign-in links hold a number; ones made before #1004
    # deleted them are swept here, once per start. Idempotent, and a failure
    # must not stop the app serving.
    try:
        svc.prune_imessage_links()
    except SQLAlchemyError:
        logger.warning("imessage link sweep failed at start", exc_info=True)
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

    security_headers.install(app, production=cfg.app_env == "production")

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
            #
            # A session a confirm link opened (_first_run) ends the moment
            # its account has anything but the sample: a forwarded link must
            # never read records connected after it was used (#920 F1). A
            # code or passkey sign-in replaces it with an ordinary session.
            ended = current_account() is None or (
                session.get(_VIA_LINK)
                and svc.has_real_connection(session.get("account_id")))
            if ended:
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
                # A page, not a form post: the sign-in that follows comes
                # back here (/home resumes it), so a tapped link is not lost.
                target = _tapped_target(request)
                if (request.method == "GET" and target
                        and request.path != "/home"):
                    session[_AFTER_SIGN_IN] = target
                    session[_AFTER_SIGN_IN_AT] = time.time()
                return redirect(url_for("auth"))
            # The engine's audit trail names the account that asked
            # (one assistant per person spec, section 6.2).
            with acting_for(session.get("account_id")):
                return fn(*a, **k)
        return wrapper

    def full_sign_in_required(fn):
        """For what outlasts or widens a session: a passkey, a grant to an
        app, a phone or chat bound to the account, real records; and for
        what undoes the owner's own choices: deleting the account, revoking
        a grant. A session
        a confirm link opened is refused until a code or passkey sign-in
        (#920 F1). Goes under @login_required."""
        @wraps(fn)
        def wrapper(*a, **k):
            if session.get(_VIA_LINK):
                if (request.path.startswith(("/api/", "/webauthn/"))
                        or request.method == "POST"
                        and request.path == "/authorize/decide"):
                    return jsonify({"error": "sign_in_again",
                                    "message": REAUTH_TEXT}), 403
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
        after_sign_in, after_sign_in_stale = _parked_state(session)
        after_sign_in_at = session.get(_AFTER_SIGN_IN_AT)
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
        # The page a signed-out tap asked for, resumed once by /home.
        if after_sign_in:
            session[_AFTER_SIGN_IN] = after_sign_in
            session[_AFTER_SIGN_IN_AT] = after_sign_in_at
        elif after_sign_in_stale:
            session[_AFTER_SIGN_IN_STALE] = True

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
        asked_enroll = request.args.get("enroll") == "1"
        # A confirm-link session signs in properly here before anything
        # full_sign_in_required guards, a passkey included.
        via_link = bool(session.get(_VIA_LINK))
        enroll = asked_enroll and not via_link
        # The chat "Add a passkey" came from, to return to afterwards: only
        # this account's own, so it can never send anyone elsewhere.
        back = ""
        me = current_account()
        agent = request.args.get("agent") or ""
        if asked_enroll and me and svc.get_agent_context(me.id, agent):
            back = url_for("chat", agent=agent)
        # "Sign in again to connect your records" from a link session's
        # hub card or chat (#934 QA F1): a code first, then back to the card.
        code_for_connect = (request.args.get("connect") == "1"
                            and via_link and me is not None)
        if code_for_connect:
            back = url_for("home") + "#connect-records"
        # The same step for Settings' invite link (#937): a code, then back
        # to the card that now shows the link.
        code_for_invite = (request.args.get("invite") == "1"
                           and via_link and me is not None
                           and not code_for_connect)
        if code_for_invite:
            back = url_for("settings") + "#invite-section"
        if session.get("account_id") and not enroll and not via_link:
            return redirect(url_for("home"))
        # Set by a beta confirm that could not sign in (beta_signup): the
        # address is filled in and the email code is the first step.
        prefill = session.get(beta_signup.AUTH_EMAIL_KEY)
        if via_link and not isinstance(prefill, str):
            prefill = me.email if me else ""
        return render_template("auth.html", rp_id=cfg.rp_id, enroll=enroll,
                               back=back,
                               # "Add a passkey" from a link session: say
                               # why a code comes first (patient walk F1).
                               code_for_passkey=asked_enroll and via_link,
                               code_for_connect=code_for_connect,
                               code_for_invite=code_for_invite,
                               prefill=(prefill if isinstance(prefill, str)
                                        else ""),
                               imessage_pending=bool(
                                   session.get("imessage_link")),
                               # A page tapped while signed out is waiting:
                               # say so, and go straight there after the
                               # code (patient walk of #1016). Not while a
                               # consent request or texted link waits: /home
                               # sends those first and drops the page.
                               resume=(_parked_target(session) is not None
                                       and not session.get("consent_req")
                                       and not session.get("imessage_link")),
                               terms_url=f"{cfg.healthclaw_public_base}/terms",
                               privacy_url=f"{cfg.healthclaw_public_base}/privacy")

    @app.get("/home")
    @login_required
    def home():
        # The page tapped while signed out is taken now, whatever wins
        # below: fresh, it is followed once after the steps that come
        # first; when one of those wins it is dropped, so it never takes
        # over a later, unrelated visit to the hub.
        after_sign_in, stale = _parked_state(session)
        session.pop(_AFTER_SIGN_IN, None)
        session.pop(_AFTER_SIGN_IN_AT, None)
        # Dropped for its age, here or at sign-in: the hub says why it
        # opened instead, once. Not when a step below wins.
        stale = session.pop(_AFTER_SIGN_IN_STALE, None) is True or stale
        # A consent request that was waiting on sign-in resumes here, once.
        pending_req = session.pop("consent_req", None)
        if pending_req:
            return redirect(url_for("consent_authorize", req=pending_req))
        # So does a texted sign-in link: bind the handle it came from.
        if session.get("imessage_link"):
            return redirect(url_for("imessage_link_claim"))
        # Then the tapped page. The page itself checks the account owns
        # what it names.
        if after_sign_in:
            return redirect(after_sign_in)
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
        menu = hub_view.menu_items(
            connectors.catalog(cfg, real_records=real_open), real_open)
        return render_template(
            "home.html", me=acct,
            stale_link_note=stale,
            connect_next=_connect_next(acct, view, menu, real_open, paused),
            # A confirm-link session connects only after a code (#920 F1):
            # the card says so and goes to /auth (#934 QA F1).
            link_session=bool(session.get(_VIA_LINK)),
            invite_line=_invite_line(acct, real_open),
            stale_consent=stale_consent,
            paused=paused, paused_line=beta.PAUSED_HUB_TEXT,
            invited=invited,
            hub=view,
            # Person, tone and routines per assistant, and whether the
            # account kept several (one assistant per person spec, 4 and
            # 8), for the hub card to render.
            people_view=hub_view.assistants(data),
            # "Add someone you care for", made-up records only (owner
            # decision D1), behind CARE_PEOPLE as /api/people is.
            people_enabled=cfg.people_enabled,
            text_tile=beta_signup.text_tile(
                cfg, view, svc.session, acct.email),
            # "Connected" only once a real connection is active; has_real
            # also counts one still connecting. "Made-up", as on /beta.
            banner_records=("your records are connected"
                            if any(not r["is_sample"]
                                   for r in view["active_records"])
                            else "made-up records"),
            switch_prompt=(None if svc.switch_prompted_at(acct.id)
                           else hub_view.switch_prompt(data)),
            # A Fasten card still connecting links to its connect page
            # (#937). A link is never blocked as a popup is, so a person
            # whose browser blocked the first window is not left with only
            # a Refresh that repeats it. While an import runs the card says
            # the records are on their way instead, and after a failed one
            # it says so and offers the page again (QA940 rounds 2 and 3).
            connect_pages=({} if paused else {
                r["id"]: {"url": hc.fasten_connect_url(r["tenant_id"]),
                          "state": _import_state(r["tenant_id"])}
                for r in view["records"]
                if r["kind"] == "fasten" and r["status"] == "pending"}),
            has_grants=bool(svc.list_grants(acct.id)),
            # One status per source row, chosen in one place (hub critique).
            chip=hub_view.status_chip,
            # "Where you can reach me": connected phones, masked as Settings
            # shows them. Setting one up stays on Settings.
            reach_phones=[imessage.masked_display(x["handle"])
                          for x in data["surfaces"]
                          if x["kind"] == "imessage"
                          and x["status"] == "active"],
            imessage_handle=cfg.imessage_handle,
            menu_open=real_open, groups=connectors.GROUPS,
            terms_url=f"{cfg.healthclaw_public_base}/terms",
            privacy_url=f"{cfg.healthclaw_public_base}/privacy",
            tester_terms_approved=tester_terms.approved(),
            consent_version=tester_terms.CONSENT_VERSION,
            terms_change=tester_terms.CHANGE_SUMMARY,
            menu=menu)

    def _can_connect_next(menu, real_open, paused, has_real) -> bool:
        """May "Connect your records" be offered? When the account may
        start a real-record connection (`_real_records_open`, the one
        gate), the Fasten tile is live, and it has no real records yet."""
        return (not paused and real_open and not has_real
                and any(m["id"] == "fasten" and m["tier"] != "soon"
                        for m in menu))

    def _answered(acct) -> bool:
        try:
            return funnel.reached(svc.session, acct.id, "first_answer")
        except Exception:  # noqa: BLE001 - the card is left out, not the page
            return False

    def _import_state(tenant: str) -> str:
        """What a connecting Fasten card says (QA940 rounds 2 and 3):
        "running", "failed", or anything else for the plain connect link.
        A failed look answers "unknown": the link is the fallback for a
        blocked popup, so doubt keeps it."""
        try:
            return hc.fasten_import_state(tenant)
        except HealthClawError:
            logger.warning("import status unavailable on the hub")
            return "unknown"

    def _connect_next(acct, view, menu, real_open, paused) -> bool:
        """Is "Connect your records" the hub's next step? Once made-up
        records have answered a question, and `_can_connect_next`."""
        return (_can_connect_next(menu, real_open, paused, view["has_real"])
                and _answered(acct))

    def _chat_connect_next(acct, sample: bool) -> str | None:
        """The chat's pointer to the hub's card: "now" once a made-up answer
        has come, "after" to show it when this page's first answer does, or
        None. On a sample chat only."""
        if not sample:
            return None
        real_open = _real_records_open(acct)
        menu = hub_view.menu_items(
            connectors.catalog(cfg, real_records=real_open), real_open)
        has_real = any(c["kind"] != "sample" and c["status"] != "revoked"
                       for c in svc.list_home(acct.id)["connections"])
        if not _can_connect_next(menu, real_open, svc.is_paused(acct.id),
                                 has_real):
            return None
        return "now" if _answered(acct) else "after"

    def _invite_line(acct, real_open) -> str | None:
        """The honest line for someone who joined through an invite link
        but could not be given real records."""
        if real_open:
            return None
        try:
            outcome = invites.join_outcome(svc.session, acct.email)
        except Exception:  # noqa: BLE001
            return None
        if outcome == invites.FULL:
            return ("Spots for connecting your own records are full right "
                    "now, so you're using made-up records.")
        if outcome == invites.CLOSED:
            return ("Connecting your own records isn't open yet, so you're "
                    "using made-up records.")
        if outcome == invites.NONE:
            # The hub's own words for an empty account, not "isn't open
            # to you", which reads as being left out (patient re-walk).
            return ("For now you're using made-up records. Connecting your "
                    "own records is coming later in the beta.")
        return None

    @app.get("/settings")
    @login_required
    def settings():
        acct = current_account()
        data = svc.list_home(acct.id)
        assistants = hub_view.assistants(data)["assistants"]
        # A link is a lasting power to sign people up, so it is made and
        # shown only after a full sign-in (#928 F6), as full_sign_in_required
        # holds for the routes that widen an account.
        invite = None
        if not session.get(_VIA_LINK):
            try:
                invite = invites.settings_row(svc, cfg, acct.id)
            except Exception:  # noqa: BLE001 - the row is left out, not the page
                logger.warning("invite link not shown")
        return render_template(
            "settings.html", me=acct, invite=invite,
            # Says where the link is instead of leaving the section out
            # (#937): one code sign-in away.
            link_session=bool(session.get(_VIA_LINK)),
            passkeys=svc.list_passkeys(acct.id),
            grants=_grants_with_labels(svc.list_grants(acct.id),
                                       data["connections"]),
            # Pairing a phone binds the holder's own assistant, never the
            # one for someone they care for (spec section 5).
            first_agent=next(
                (a["id"] for a in assistants
                 if a["is_primary"] and a["person"]["kind"] == "self"), ""),
            assistants=assistants,
            tones=list(PERSONAS.items()),
            # The hub card's own words ("Tone: calm"), so the two match.
            tone_words=hub_view.TONE_WORDS,
            routine_catalog=advisors.routine_catalog(),
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

    code_requests: OrderedDict[str, deque] = OrderedDict()
    # gunicorn runs several threads per worker: reading, counting and
    # re-inserting a window is one step, or a racing burst beats the limit.
    code_requests_lock = threading.Lock()

    def _code_request_client() -> str:
        # As /feedback and /beta do: X-Real-IP, never X-Forwarded-For, whose
        # entries can be the client's own words. The edge must overwrite
        # X-Real-IP with the connecting address (Railway's does); passed
        # through from the client, it would pick its own bucket.
        raw = (request.headers.get("X-Real-IP") or "").strip()
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            return request.remote_addr or ""

    def _code_request_wait(key: str) -> int:
        """0 and counted, or the whole seconds until this client may ask."""
        moment = time.time()
        with code_requests_lock:
            window = code_requests.pop(key, None) or deque()
            while window and moment - window[0] > CODE_REQUEST_WINDOW:
                window.popleft()
            wait = 0
            if len(window) >= CODE_REQUESTS_PER_IP:
                oldest = window[len(window) - CODE_REQUESTS_PER_IP]
                wait = max(1, math.ceil(oldest + CODE_REQUEST_WINDOW
                                        - moment))
            else:
                window.append(moment)
            code_requests[key] = window             # newest last
            while len(code_requests) > CODE_REQUEST_KEYS:
                code_requests.popitem(last=False)   # forget the oldest key
            return wait

    @app.post("/api/auth/email")
    def auth_email():
        email = (request.get_json(silent=True) or {}).get("email", "")
        purpose = "verify"
        # Per client address, before any address is looked at: one network
        # asking for codes to many inboxes. Keyed on the network alone, so
        # the answer says nothing about the address asked for. A 429 with a
        # sentence, not the resend wait: that answer tells the person a code
        # went out moments ago, and from here none did.
        wait = _code_request_wait(_code_request_client())
        if wait:
            funnel.safely(funnel.count_issue, svc.session, "rate_limited")
            minutes = math.ceil(wait / 60)
            return jsonify({
                "error": ("Too many code requests from this network. Try "
                          f"again in {minutes} minute"
                          f"{'' if minutes == 1 else 's'}."),
                "retry_after": wait}), 429
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
            funnel.note_mail(svc.session, cfg, mail.NOT_SENT)
            # Never report "sent" when nothing was sent — this is the front
            # door, and a silent failure leaves the person watching an empty
            # inbox with no idea whether to wait or retry.
            return jsonify({"error": str(exc), "sent": False}), 502
        if retry_after:
            # The resend wait, or a per-address cap, suppressed the send
            # (#262). 200, not 4xx: the request was handled correctly and the
            # address was sent a code recently, so the UI still advances to
            # code entry and shows the wait. But claiming "sent" here is the
            # lie — it strands whoever's first email never arrived, telling
            # them to wait for something nobody sent.
            #
            # Not an enumeration oracle: this branch turns only on the codes
            # already requested for this address, and their wrong guesses —
            # states requesters created — never on whether an account exists.
            # start_email_code reads ca_accounts only to choose the email's
            # wording; the account's existence never reaches this answer, and
            # accounts are created at verify. The genuine-send and wait
            # responses are byte-identical for an address with an account and
            # one without (tests/test_careagents_walk_10_20_security.py).
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
            funnel.safely(funnel.count_issue, svc.session, "sign_in_failed")
            return jsonify({"error": str(exc)}), 400
        _login(acct)
        # Nothing connected yet: the records come first, and the passkey
        # waits (it is in the chat's menu), so auth.js skips its prompt.
        return jsonify({"ok": True, "has_passkey": svc.has_passkey(acct.id),
                        "first_run": not svc.has_connections(acct.id),
                        # After the code only, so this says nothing to
                        # someone who doesn't hold the inbox (10-20 walk).
                        "new_account": bool(getattr(acct, "new_account",
                                                    False))})

    # --- WebAuthn (biometric) ------------------------------------------------

    @app.post("/webauthn/register/options")
    @login_required
    @full_sign_in_required
    def wa_register_options():
        acct = current_account()
        options, challenge = svc.registration_options(acct)
        session["wa_challenge"] = challenge
        return jsonify(options)

    @app.post("/webauthn/register/verify")
    @login_required
    @full_sign_in_required
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
                                             challenge, method="passkey")
        except AuthError as exc:
            funnel.safely(funnel.count_issue, svc.session, "sign_in_failed")
            return jsonify({"error": str(exc)}), 400
        except Exception:  # noqa: BLE001
            funnel.safely(funnel.count_issue, svc.session, "sign_in_failed")
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
        # Someone the holder cares for is never offered to a third-party
        # app in the beta (one assistant per person spec, 6.3).
        others = svc.other_person_connection_ids(acct.id)
        return [c for c in conns
                if c["status"] != "revoked" and c["id"] not in others
                and (c["kind"] == "sample" or real_open)], real_open

    @app.get("/authorize")
    def consent_authorize():
        req = (request.args.get("req") or "").strip()
        request_id = consent.parse_handle(req, cfg.mint_secret)
        if request_id is None:
            return render_template("consent.html", state="invalid",
                                   me=current_account()), 400
        if current_account() is None or session.get(_VIA_LINK):
            # A confirm-link session grants no app anything (#920 F1): the
            # request waits for a code or passkey sign-in, as signed out.
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
    @full_sign_in_required
    def wa_consent_options():
        options, challenge = svc.authentication_options(require_uv=True)
        session["wa_consent_challenge"] = challenge
        return jsonify(options)

    @app.post("/authorize/decide")
    @login_required
    @full_sign_in_required
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
        if (conn is None or conn["status"] == "revoked"
                or conn["id"] in svc.other_person_connection_ids(acct.id)):
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
    @full_sign_in_required
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
        real_open = _real_records_open(acct)
        plan = connectors.start(
            connector_id, body.get("provider"), cfg, hc,
            real_records=real_open)
        if plan.get("error"):
            if connector_id == "fasten" and real_open:
                # Past the gate and still refused: Fasten is not available
                # here. A refusal at the gate is not a Fasten failure.
                funnel.safely(funnel.count_issue, svc.session,
                              "fasten_failed")
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
        if connector_id not in ("fasten", "direct"):
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
            if connector_id == "direct":
                # Same for a file: a connection still waiting for its first
                # file takes the next one, so a second try never adds a
                # second card.
                waiting = svc.pending_connection(acct.id, "direct",
                                                 status="empty")
                if waiting:
                    return jsonify({"id": waiting["id"], "status": "empty",
                                    "existing": True})
                return _persist_connection(connector_id, acct, plan,
                                           consent_version)
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
                return jsonify({"error": RECORDS_DOWN_TEXT}), 503
        cid = svc.add_connection(acct.id, connector_id, tenant, plan["label"],
                                 status=plan["status"],
                                 provider=plan.get("provider"),
                                 consent_version=consent_version)
        if consent_version is not None:
            # A real-record connection, past the gate and the consent.
            funnel.safely(funnel.mark, svc.session, acct.id,
                          "connect_started")
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
            if session.get(_VIA_LINK):
                # Real records only after a code or passkey (#920 F1).
                return jsonify({"error": "sign_in_again",
                                "message": REAUTH_TEXT}), 403
            return _start_connection(connector_id, acct, body)
        return _sample_tap(acct, body)

    def _sample_tap(acct, body):
        """"Explore with made-up records": the hub's tap, and a confirmed
        beta request's first run (_first_run)."""
        connector_id = "sample"
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

    def _first_run(email: str) -> str | None:
        """Sign in the address that just confirmed its beta request and
        open its made-up records, or None for the confirmed page.

        Never swaps out someone else signed in on this browser, and never
        signs in to an account with records already (first_run_account):
        a forwarded link opens nothing that is there. A seed that fails
        still signs in; the hub's sample button tries again."""
        me = current_account()
        if me is not None and me.email != email.strip().lower():
            return None
        acct = svc.first_run_account(email, sign_in=me is None)
        if acct is None:
            return None
        if me is None:
            _login(acct)
            # A link-opened session: gone with the browser, limited to the
            # sample, and ended once real records arrive (#920 F1).
            session.permanent = False
            session[_VIA_LINK] = True
        # Already signed in here as this address: that session, full or
        # link, stays as it is; never downgraded (#920 QA Low 1).
        with acting_for(acct.id):
            answer = app.make_response(_sample_tap(acct, {}))
        landing = (answer.get_json(silent=True) or {}).get("redirect")
        if answer.status_code == 200 and landing:
            return landing
        return url_for("home")

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
            funnel.safely(funnel.mark, svc.session, acct.id,
                          "records_connected")
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
        # say "17 records added" where a refresh then said 5 (#226), and the
        # Patient, which the records card never counts (#948). Report the
        # records the card counts, and the same sentence the poll uses.
        documents = _documents_landed(bundle, result, landed)
        records = _records_landed(bundle, result, landed)
        response["records_added"] = records
        # So the hub says "you can read" only when unreadable documents did
        # arrive; otherwise the line is "No new records added." (#953 QA).
        response["documents_added"] = documents
        note = _uncounted_note(records, documents, uncounted)
        if landed and not records and not documents:
            # Something landed and none of it is a record the card counts.
            # "No new records added." alone read as a failed upload (QA on
            # #953), so say what the file held. Only resource types are read.
            note = _nothing_counted_note(bundle, result, landed)
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
        # "disconnected" while records may still arrive. Nor "still on": a
        # lost answer can follow a revoke that ran, as in delete_connection.
        try:
            hc.revoke_fasten(conn["tenant_id"])
        except HealthClawError:
            return jsonify({"error": "disconnect_failed",
                            "message": "We couldn't confirm the disconnect. "
                                       "Please try again in a minute."}), 503
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
        # The typed DELETE, checked here as the account delete checks it
        # (security review of #944): the modal alone is a client's promise.
        # After the ownership check, so a stranger's id still reads 404.
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get("confirm") != "DELETE":
            return jsonify({"error": "confirm_required",
                            "message": "Type the word delete to confirm."}), 400
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
        home = svc.list_home(acct.id)
        gone = [a["name"] for a in home["agents"]
                if a["connection_id"] == conn_id]
        # Whose records, and whether that person leaves the hub with them,
        # as the dialog said before the tap (patient test on #952).
        row = next((r for r in hub_view.build(home, time.time())["records"]
                    if r["id"] == conn_id), {})
        label = (f"{conn['label']} ({row['whose']})" if row.get("whose")
                 else conn["label"])
        left = (hub_view.person_name(hub_view.person_of(
                    home, conn.get("person_id")))
                if row.get("leaves") else "")
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
            "message": _deleted_sentence(n, label, gone, left),
        })

    def _deleted_sentence(n: int | None, label: str,
                          gone: list[str], left: str = "") -> str:
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
        if left:
            lead += f" {left} was removed from your hub too."
        return (lead + " We keep a log of who looked at your records, with "
                "no health details in it, and this deletion is in it.")

    @app.post("/api/account/delete")
    @login_required
    @full_sign_in_required
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
                            "message": "Type the word delete to confirm."}), 400
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
        # of "this tenant holds notes" (#226). Not while a Fasten import is
        # still writing: that count is part of an import, and storing it left
        # the card at "49 records" for good (QA940-F1b). The last baseline
        # stands, or none, and the poll stores the count once it is done.
        try:
            if not (conn["kind"] == "fasten"
                    and hc.fasten_import_running(conn["tenant_id"])):
                svc.mark_synced(conn_id, hc.record_count(conn["tenant_id"]),
                                hc.uncounted_record_count(conn["tenant_id"]))
        except HealthClawError:
            return jsonify({"error": RECORDS_DOWN_TEXT}), 503

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
            # A Fasten export is written in the background, a Patient first,
            # so records landing is not the import being done. Until it is,
            # no count is stored or shown: a mid-import count was stored once
            # and the card read "29 records" for good with 150 there
            # (QA940-F1).
            importing = (landed and conns[conn_tenant]["kind"] == "fasten"
                         and hc.fasten_import_running(conn_tenant))
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
        if importing:
            # Still "Connecting…" on the card, so the pending poller keeps
            # asking. A connection already active (a refresh) stays active,
            # with no "new records" yet: the watcher stops at the first one.
            return jsonify({"status": "active"
                            if conns[conn_tenant]["status"] == "active"
                            else "pending"})
        if landed:
            svc.activate_connection(conn_tenant)
            if conns[conn_tenant]["kind"] != "sample":
                funnel.safely(funnel.mark, svc.session, acct.id,
                              "records_connected")
            out = {"status": "active"}
            # First records ever on this connection: store the count the
            # card shows ("N records", hub.py). Only when none is stored, so
            # a refresh's baseline below is never overwritten (#937). A
            # failed count leaves it unset, which the card shows as nothing,
            # never as zero.
            if conns[conn_tenant].get("last_count") is None:
                try:
                    svc.mark_synced(conns[conn_tenant]["id"],
                                    hc.record_count(conn_tenant),
                                    hc.uncounted_record_count(conn_tenant))
                except HealthClawError:
                    logger.warning("record count unavailable on activation")
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
        """The assistant for the person whose records `connection_id` holds.
        One per person (spec section 3): when they already have one, that
        one is the answer, `existing` set, and nothing is made."""
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
        conn_id = str(body.get("connection_id") or "")
        had = svc.agent_for_person_of(acct.id, conn_id)
        if had:
            return jsonify({"id": had, "existing": True})
        try:
            aid = svc.create_agent(acct.id, name, persona, conn_id,
                                   advisor=advisor)
        except AuthError as exc:
            return jsonify({"error": str(exc)}), 400
        if aid and advisor:
            svc.set_agent_settings(
                acct.id, aid, persona,
                advisors.routines_for({"routines": None, "advisor": advisor}))
        return jsonify({"id": aid})

    @app.post("/api/agents/<agent_id>/settings")
    @login_required
    def agent_settings(agent_id):
        """Tone and routines (spec section 7). Both are checked against the
        fixed sets here; neither changes what the assistant can read."""
        acct = current_account()
        body = request.get_json(silent=True) or {}
        ctx = svc.get_agent_context(acct.id, agent_id)
        if ctx is None:
            return jsonify({"error": "unknown agent"}), 404
        if "persona" not in body and "routines" not in body:
            return jsonify({"error": "nothing to change"}), 400
        persona = body.get("persona", ctx["agent"]["persona"])
        # A string first: a list or object is unhashable, not a tone.
        if not isinstance(persona, str) or persona not in PERSONAS:
            return jsonify({"error": "unknown tone"}), 400
        if "routines" in body:
            asked = body["routines"]
            if (not isinstance(asked, list)
                    or not all(isinstance(r, str) and r in advisors.ROUTINES
                               for r in asked)):
                return jsonify({"error": "unknown routine"}), 400
            # Stored in the catalog's order, once each.
            routines = [r for r in advisors.ROUTINES if r in asked]
        else:
            routines = advisors.routines_for(ctx["agent"])
        svc.set_agent_settings(acct.id, agent_id, persona, routines)
        return jsonify({"id": agent_id, "persona": persona,
                        "routines": routines})

    @app.post("/api/people")
    @login_required
    @full_sign_in_required
    def add_person():
        """"Add someone you care for", on made-up records only (spec 6,
        owner decision D1). A relationship and an optional nickname; the
        body is read for nothing else, so there is no path here to real
        records. Their own sample tenant, their own assistant."""
        if not cfg.people_enabled:
            # Behind CARE_PEOPLE until the hub renders people (#947 QA).
            return jsonify({"error": "not found"}), 404
        acct = current_account()
        body = request.get_json(silent=True) or {}
        if svc.is_paused(acct.id):
            return jsonify({"error": "records_paused",
                            "message": beta.PAUSED_RECORDS_TEXT}), 423
        # The sample tap's lease: one tenant mint at a time per account.
        if not svc.claim_sample_start(acct.id):
            return jsonify({"status": "connecting",
                            "error": "Made-up records are on their way. "
                                     "Try again in a moment."}), 409
        try:
            try:
                person = svc.add_person(acct.id, body.get("relationship"),
                                        body.get("nickname"))
            except ValueError:
                return jsonify({"error": "Choose who they are to you, and "
                                         "a nickname of up to 24 "
                                         "characters if you like."}), 400
            except AuthError as exc:
                return jsonify({"error": str(exc)}), 409
            plan = connectors.start("sample", None, cfg, hc)
            try:
                hc.seed(plan["tenant"])
            except HealthClawError:
                svc.remove_empty_person(acct.id, person)
                return jsonify({"error": RECORDS_DOWN_TEXT}), 503
            cid = svc.add_connection(acct.id, "sample", plan["tenant"],
                                     plan["label"], status="active",
                                     provider=plan.get("provider"),
                                     person_id=person)
            try:
                svc.mark_synced(cid, hc.record_count(plan["tenant"]))
            except HealthClawError:
                logger.warning("record count after sample seed failed for %s",
                               cid)
            # A name of their own, so two cards are not both "Juniper"
            # (#951 F4). From a fixed list, never the nickname: the name
            # goes into the model's prompt ("You are ...").
            taken = {a["name"] for a in svc.list_home(acct.id)["agents"]}
            name = next((n for n in _CARE_NAMES if n not in taken),
                        _CARE_NAMES[0])
            aid = svc.create_agent(acct.id, name, DEFAULT_PERSONA, cid)
            return jsonify({"person_id": person, "connection_id": cid,
                            "agent_id": aid,
                            "redirect": url_for("chat", agent=aid)})
        finally:
            svc.release_sample_start(acct.id)

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
        # A person's primary assistant is not removed, here as on the card
        # (owner decision D5, SEC951-L1): only an older account's extras
        # are. Deleting the account, or the records it reads, still goes.
        primary = {a["id"]: a["person"] for a in hub_view.assistants(
            svc.list_home(acct.id))["assistants"] if a["is_primary"]}
        if agent_id in primary:
            # Mom's says so, not "your assistant" (FHI-44). By relationship,
            # never the nickname: a JSON error is a string any client may
            # one day render raw (security review of #952, I1).
            name = hub_view.relation_of(primary[agent_id])
            return jsonify({"error": (
                f"This is the assistant for {name}, so it can't be "
                "removed on its own. To remove it, delete their records on "
                "your hub." if name else
                "This is your assistant, so it can't be removed.")}), 409
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
        sample = conn.get("kind") == "sample"
        # Someone the holder cares for (FHI-44). `who` is their relationship
        # ("your parent") for anything a model can read back: the sample
        # line every answer opens with, and a starter tap. `name` may be
        # the nickname and is for this page's own words only.
        person = hub_view.person_of(svc.list_home(acct.id),
                                    ctx["agent"].get("person_id"))
        who = hub_view.relation_of(person)
        frame = beta.sample_frame(who)
        if sample:
            # The banner above the log says the records are made up, so a
            # replayed answer does not say it again (beta.under_banner).
            past = [dict(m, content=beta.under_banner(str(m.get("content")
                                                          or ""), frame))
                    if m.get("role") != "user" else m for m in past]
        # Retries of a broken chat replay as one failure, not a wall.
        past = errors.collapse_failed_turns(past)
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
        # A Fasten export still being written is a fraction of the record:
        # no greeting counts, no settle and no stored count, as the poll
        # leaves it (QA940-F1). A failed look is treated the same way. Never
        # "overdue" either: a long import is not one that sent nothing.
        importing = False
        # Asked only once something landed, so "still arriving" is the true
        # sentence whether the import runs or could not be asked about.
        if (pending and totals and any(totals.values())
                and conn.get("kind") == "fasten"):
            try:
                importing = hc.fasten_import_running(ctx["tenant"])
            except HealthClawError:
                logger.warning("import status unavailable for agent %s",
                               agent_id)
                importing = True
        intake = intake_state.classify(
            totals=None if importing else totals,
            connection_status=conn.get("status"),
            connected_at=None if importing else conn.get("connected_at"),
            now=time.time(),
            provider=conn.get("provider") or conn.get("label"))
        if pending and intake.state == intake_state.READY:
            # Records landed while nobody was polling /connect. Settle the
            # stored status here so the next visit costs no counts.
            svc.set_connection_status(ctx["tenant"], "active")
            # And the hub card's "N records", as the poll stores it (#937).
            if conn.get("id") and conn.get("last_count") is None:
                try:
                    svc.mark_synced(conn["id"], hc.record_count(ctx["tenant"]),
                                    hc.uncounted_record_count(ctx["tenant"]))
                except HealthClawError:
                    logger.warning("record count unavailable on activation")
        # Starter taps tuned to what the records can answer (hub critique
        # 2026-10). Labs only when laboratory results are known to be there,
        # not a heart rate (QA #944 F2). Only a total crosses, as for the
        # greeting: the engine's `code` filter matches a category's code
        # too, so `code=laboratory` counts Observations filed as labs
        # (pinned on the real engine by test_hub_labs_count_real_engine).
        # The made-up records are a fixed seed that holds lab results and
        # files none by category. A failed count hides the tap, never
        # offers it. "What changed since my last visit?" is not offered:
        # records hold no visit to compare with (patient tester on #944).
        has_labs = False
        if totals and totals["labs"] == 0:
            has_labs = False
        elif sample:
            has_labs = True
        else:
            try:
                has_labs = int(hc.search(
                    ctx["tenant"], "Observation",
                    {"code": "laboratory", "_summary": "count"}
                ).get("total") or 0) > 0
            except (HealthClawError, TypeError, ValueError):
                has_labs = False
        has_records = (any(totals.values()) if totals is not None
                       else bool(conn.get("last_count")))
        # Intake leads, always: that path ends in an approval, which is
        # what the product is for (spec section 5). A tap is sent to the
        # model as the person's message, so someone they care for is named
        # by relationship, never by nickname (FHI-44).
        mine = "my " + who[len("your "):] if who.startswith("your ") else (
            "someone I care for" if who else "")
        starters = [s for s, ok in (
            ((f"Fill out a new doctor's intake form for {mine}" if mine
              else "Fill out my intake form for a new doctor"), True),
            ((f"Make a visit brief for {mine}" if mine
              else "Make my visit brief"), has_records),
            ((f"Explain the latest labs for {mine}" if mine
              else "Explain my latest labs"), has_labs)) if ok]
        # The "Open the review" card is a turn event, and history keeps only
        # text, so a reload lost it while the reply above still promised it
        # (#847). Ask the engine what is waiting instead; nothing is stored.
        # A lookup that fails draws no card, and the "Waiting for you" pill
        # in the header still leads to the list. A disconnected connection
        # is not a pathway to its requests (#215), so it is not asked at
        # all: a card there would promise an approval the relay refuses
        # (security review of #853).
        reviews = []
        # Only a lookup that answered says no form is waiting (#998 L2).
        pending_known = False
        if conn.get("status") != "revoked":
            try:
                reviews = [{"id": a["id"],
                            "label": _KIND_LABELS.get(a.get("kind"),
                                                      "Request"),
                            "form": a.get("kind") == "form-fill"}
                           for a in hc.pending_actions(ctx["tenant"])
                           if a.get("id")]
                pending_known = True
            except HealthClawError:
                logger.warning("pending reviews unavailable for agent %s",
                               agent_id)
                reviews = []
        # Approved, its card is gone and the reply above still says the
        # form isn't done (patient walk 2026-10-18). The newest finished
        # form this week is drawn as done, linking to its review page,
        # which shows the PDF. Not while a form waits: that one is next.
        done_form = None
        if pending_known and not any(r["form"] for r in reviews):
            try:
                done = sorted(
                    (a for a in hc.recent_actions(ctx["tenant"])
                     if a.get("id") and a.get("kind") == "form-fill"
                     and a.get("status") == "completed"),
                    # str(): an engine row with a numeric time must not
                    # 500 the chat (#998 L1).
                    key=lambda a: str(a.get("updated_at") or ""),
                    reverse=True)
                done_form = done[0]["id"] if done else None
            except HealthClawError:
                logger.warning("recent forms unavailable for agent %s",
                               agent_id)
        return render_template("chat.html", me=ctx["agent"], persona=p,
                               agent_id=agent_id,
                               conversation_id=conversation_id,
                               past=past,
                               history_lost=history_lost,
                               intake=intake,
                               # Records landed and more are coming: the
                               # assistant reads the part that is here.
                               importing=importing,
                               summary_counts=intake.counts,
                               pending_reviews=reviews,
                               done_form=done_form,
                               starters=starters,
                               # A link session reaches it through a code
                               # sign-in first (/auth, #920 F1).
                               offer_passkey=not svc.has_passkey(acct.id),
                               # Back from /auth?enroll=1 (auth.js); said
                               # only when the passkey is really there.
                               passkey_added=(
                                   request.args.get("passkey") == "added"
                                   and svc.has_passkey(acct.id)),
                               connect_next=_chat_connect_next(acct, sample),
                               link_session=bool(session.get(_VIA_LINK)),
                               sample_line=frame if sample else None,
                               # "Mom's records" when they are not the
                               # holder's own (spec section 5).
                               whose=hub_view.whose_records(person),
                               # The greeting's words only, never sent.
                               name=person.get("nickname") or who)

    def _whose(acct, agent, mid: bool = False) -> str:
        """Whose records this assistant reads, as the chat header and the
        approvals page name them; empty for the holder's own. Rendered by
        the template only: the nickname is never passed to the model, a
        log line or the engine."""
        return hub_view.whose_records(hub_view.person_of(
            svc.list_home(acct.id), agent.get("person_id")), mid=mid)

    @app.get("/brief")
    @login_required
    def brief():
        acct = current_account()
        agent_id = request.args.get("agent", "")
        ctx = svc.get_agent_context(acct.id, agent_id)
        if not ctx:
            return redirect(url_for("home"))
        sample = (ctx.get("connection") or {}).get("kind") == "sample"
        try:
            # Made-up records ask the engine to word its sentences about
            # "the sample person" (r6/voice.py). A real brief asks as before.
            raw = (hc.fetch_appointment_brief(ctx["tenant"], voice="sample")
                   if sample else hc.fetch_appointment_brief(ctx["tenant"]))
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
                               care_gaps_note=_parse_care_gaps_reason(raw),
                               sample_line=(beta.sample_frame(
                                   hub_view.relation_of(hub_view.person_of(
                                       svc.list_home(acct.id),
                                       ctx["agent"].get("person_id"))))
                                   if sample else None))

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
            # The worker's failure text already carries the 911 line; one
            # the engine wrote, or none, gets it here.
            return {"type": "error", "text": beta.with_911(
                payload.get("text") or GENERIC_FAILURE_TEXT)}
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

    def _refuse_turn_without_workers(state: str, text: str = ""):
        """The 503 body for a chat turn no worker can be promised to run.

        The patient-facing sentence is the same in every failure state,
        because it is true in all of them. Only the machine-readable code
        differs. It carries the 911 line (_with_emergency), and chat.js
        shows it instead of retrying.
        """
        return _chat_unavailable(
            _REFUSAL_CODES.get(state, "run_workers_unavailable"), text)

    def _chat_unavailable(code: str, text: str):
        """A 503 for a chat turn that could not be taken: the store, the
        run queue or the workers. One sentence for all of them."""
        return jsonify(_with_emergency({
            "error": code,
            "message": "Chat is temporarily unavailable. Try again soon.",
        }, text)), 503

    def _with_emergency(body: dict, text: str) -> dict:
        """`body` with its sentence carrying the 911 line (beta.with_911):
        led by it, and marked `emergency`, when `text` reads as a crisis;
        followed by it otherwise. The sentence defaults to the pace
        sentence chat.js shows for a 429 that carries none."""
        sentence = body.get("message") or PACE_TEXT
        said = beta.with_911(sentence, text)
        if said.startswith(beta.EMERGENCY_LINE):
            return {**body, "message": said, "emergency": True}
        return {**body, "message": said}

    def _run_said_anything(tenant: str, run_id: str,
                           account_id: str | None) -> bool:
        """Whether a run left the person any text or failure sentence, read
        from its first event, so a resumed stream does not say it twice.
        A read that fails counts as nothing said."""
        try:
            with acting_for(account_id):
                page = hc.agent_run_events(tenant, run_id, after=0, limit=500)
        except HealthClawError:
            return False
        return any(e.get("type") in ("agent.text", "agent.error")
                   for e in page.get("events") or [])

    def _stream_run(tenant: str, agent_id: str, run_id: str, after: int = 0,
                    account_id: str | None = None):
        """Replay durable UI events. Disconnecting only stops this projection.

        The body runs after login_required has returned, so each read names
        the account itself (spec 6.2), scoped to that one call: a block held
        across a yield would outlive the request on this thread."""
        cursor = max(0, after)
        started = time.monotonic()
        # #575: the wait after a page with events is the base; after the k-th
        # consecutive empty page it is base * 2**(k-1), capped. A run idling
        # at 90 seconds is no likelier to finish in the next 250ms than one
        # at 5, and a token that just arrived is likely followed by another.
        empty_streak = 0
        misses = 0
        yield "data: " + json.dumps({
            "type": "accepted", "run_id": run_id,
            "next_cursor": cursor}) + "\n\n"
        while time.monotonic() - started < cfg.run_sse_timeout_seconds:
            try:
                with acting_for(account_id):
                    page = hc.agent_run_events(tenant, run_id, after=cursor,
                                               limit=100)
            except HealthClawError as exc:
                # The class and status only: the message is engine text.
                misses += 1
                logger.warning(
                    "run event read %d/%d failed for tenant %s: %s status=%s",
                    misses, _REPLAY_MAX_MISSES, tenant, type(exc).__name__,
                    exc.status)
                # #926: one failed read is not a failed run. A poll that
                # lands as the engine closes an idle keep-alive connection
                # (gunicorn's 2s, the same as the poll's backoff cap) never
                # reaches the engine, and ending here showed "Something went
                # wrong" on a run that went on to complete. The read has no
                # side effect and resumes at `cursor`, so it is tried again.
                if misses < _REPLAY_MAX_MISSES:
                    time.sleep(cfg.run_sse_poll_seconds)
                    continue
                # Without this the generator dies inside an open response and
                # the browser is left with a stream that stopped for no stated
                # reason (#219). Only this projection ends: the run is durable
                # and keeps going, and a reconnect resumes at `cursor`. The
                # text is the same generic sentence every other failure uses —
                # an exception message here would be the first place engine
                # internals reached a patient's screen.
                funnel.safely(funnel.count_issue, svc.session,
                              "run_replay_error")
                yield "data: " + json.dumps({
                    "type": "error",
                    "text": beta.with_911(GENERIC_FAILURE_TEXT)}) + "\n\n"
                return
            misses = 0
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
            if status == "failed" and not _run_said_anything(
                    tenant, run_id, account_id):
                # A run the engine failed itself (swept past its deadline
                # before a worker claimed it) has no answer at all: without
                # this the page showed nothing. Generic, with the 911 line.
                yield "data: " + json.dumps({
                    "type": "error",
                    "text": beta.with_911(GENERIC_FAILURE_TEXT)}) + "\n\n"
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
            funnel.safely(funnel.count_chat_error, svc.session,
                          causes.ENGINE_UNAVAILABLE)
            return _refuse_turn_without_workers(workers, text)
        if not _allow_turn(acct.id):
            funnel.safely(funnel.count_issue, svc.session, "rate_limited")
            return jsonify(_with_emergency({"error": "rate_limited"},
                                           text)), 429
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
                funnel.safely(funnel.count_issue, svc.session,
                              "rate_limited")
                return jsonify({
                    "error": "daily_limit_reached",
                    "used": used,
                    "limit": cfg.chat_turns_per_day,
                    # Always the 911 line; first for crisis wording.
                    "message": beta.with_911(beta.daily_limit_text(), text),
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
            funnel.safely(funnel.count_chat_error, svc.session,
                          causes.ENGINE_UNAVAILABLE)
            return _chat_unavailable("message store unavailable", text)
        try:
            run = hc.create_agent_run(
                tenant, user_message_id, cfg.run_deadline_seconds)
        except HealthClawError as exc:
            funnel.safely(funnel.count_chat_error, svc.session,
                          causes.cause_of(exc))
            return _chat_unavailable("run queue unavailable", text)
        after = _parse_cursor(body.get("after") or
                              request.headers.get("Last-Event-ID"))
        return Response(_stream_run(
            tenant, agent["id"], run["id"], after, account_id=acct.id),
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
            funnel.safely(funnel.count_issue, svc.session,
                          "run_replay_error")
            return jsonify({"error": "run service unavailable"}), 503
        if not _run_belongs_to(run, ctx["tenant"], agent_id):
            return jsonify({"error": "unknown run"}), 404
        after = _parse_cursor(request.args.get("after") or
                              request.headers.get("Last-Event-ID"))
        return Response(_stream_run(ctx["tenant"], agent_id, run_id, after,
                                    account_id=acct.id),
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
        series = labs_timeline_mod.build_series(labs.get("bundle") or {},
                                                keys)
        return jsonify({"series": series,
                        "disclaimer": _chart_footnote(series)})

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
                               kind_labels=_KIND_LABELS,
                               # "Requests for Mom's records" (#951 F3).
                               whose=_whose(acct, ctx["agent"], mid=True))

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
            # Each queue says whose records it is when they are not the
            # holder's own (spec section 5): the connection's person, the
            # same person its assistant is for.
            whose = hub_view.whose_records(
                hub_view.person_of(data, conn.get("person_id")))
            named = {"whose": whose} if whose else {}
            if n and agent:
                queues.append({"agent_id": agent["id"], "name": agent["name"],
                               "count": n, **named,
                               "href": url_for("approvals",
                                               agent_id=agent["id"])})
            elif n:
                # Whether any assistant exists decides the next step: with
                # one, it is switched to these records ("Change records");
                # with none, the hub's Start a chat is the way in (#847).
                queues.append({"name": conn["label"], "count": n, **named,
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
            recent = _recent_outcomes(tenants, bool(data["agents"]), data)
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

    def _recent_outcomes(tenants, has_assistant, home):
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
                # Whose, as the pending queues say it (patient test on
                # #952): "Intake form (Mom's records): Ready."
                whose = hub_view.whose_records(
                    hub_view.person_of(home, conn.get("person_id")))
                if whose:
                    item["whose"] = whose
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

    def _form_past_review(tenant, agent_id, action_id, whose=""):
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
                               agent_id=agent_id, is_form=is_form,
                               whose=whose)

    def _agent_owns_action(agent_id, action_id):
        """The tenant that owns this action, or None if it is not this
        agent's.

        Raises OwnershipUnknown when the engine could not be asked. An
        outage is not evidence about ownership, and this is the
        human-approval path — the guardrail the product exists to
        guarantee (#410).
        """
        ctx = _agent_action_context(agent_id, action_id)
        return ctx["tenant"] if ctx else None

    def _agent_action_context(agent_id, action_id):
        """The agent context whose tenant owns this action, or None.

        One read, so a caller deciding anything else from the context (the
        review's sample voice) sees the same connection that resolved the
        tenant (#919 security review F1).
        """
        acct = current_account()
        ctx = _live_agent_context(acct, agent_id)
        if not ctx:
            return None
        try:
            action = hc.action_status(ctx["tenant"], action_id)
        except HealthClawError as exc:
            if _engine_said_absent(exc):
                return None
            raise OwnershipUnknown from exc
        # The status rides along, so the text check below reads the same
        # answer that proved ownership.
        return dict(ctx, action=action if isinstance(action, dict) else {})

    def _text_not_yours(action):
        """True for an sms CareAgents will not approve now. Only sms is
        held to this. It must be what propose_action would have staged for
        the signed-in account at this moment: text_self.sms_phone_for
        (CARE_ACTIONS_SMS open for it, not paused, one verified phone)
        answers this number, `to` is the fixed label, and the words are one
        of the fixed texts. A text staged by another surface or a raw POST,
        an iMessage binding changed since, or the flag turned off all
        refuse. Logs the action id and the reason, never the number or
        the words."""
        if action.get("kind") != "sms":
            return False
        payload = action.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        acct = current_account()
        mine = text_self.sms_phone_for(cfg, svc, acct.id, acct.email)
        if not mine:
            reason = "no_phone_to_text"
        elif payload.get("phone") != mine:
            reason = "number"
        elif payload.get("to") != text_self.RECIPIENT_LABEL:
            reason = "label"
        elif payload.get("body") not in {text_self.body_for(p, cfg.origin)
                                         for p in text_self.PURPOSES}:
            reason = "words"
        else:
            return False
        logger.info("text approval refused for action %s: %s",
                    str(action.get("id") or "")[:64], reason)
        return True

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
            ctx = _agent_action_context(agent_id, action_id)
        except OwnershipUnknown:
            return render_template("chat_error.html",
                                   message=_REVIEW_UNCHECKABLE), 503
        if not ctx:
            return render_template("chat_error.html",
                                   message="That form isn't yours."), 404
        tenant = ctx["tenant"]
        if svc.is_paused(current_account().id):
            # Pause stops approvals too (beta spec 4.6, #856 review F1).
            return render_template("chat_error.html",
                                   message=beta.PAUSED_HUB_TEXT), 423
        # Made-up records ask the engine to word the review about "the sample
        # person" (r6/voice.py), so a tester is not asked to attest about
        # someone else as "I". Decided from the connection, never the request,
        # and from the same read that resolved the tenant.
        sample = (ctx.get("connection") or {}).get("kind") == "sample"
        # Someone the holder cares for: their records named on the page,
        # the nickname escaped and kept here, never sent to the engine.
        acct = current_account()
        person = hub_view.person_of(svc.list_home(acct.id),
                                    ctx["agent"].get("person_id"))
        whose = hub_view.whose_records(person)
        try:
            status, html = (hc.fetch_review_page(tenant, action_id,
                                                 voice="sample")
                            if sample else
                            hc.fetch_review_page(tenant, action_id))
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
            return _form_past_review(tenant, agent_id, action_id,
                                     whose=whose)
        html = html.replace(f"/r6/actions/{action_id}/review",
                            f"/review/{agent_id}/{action_id}/submit")
        if _text_not_yours(ctx.get("action") or {}):
            # "Not your number" in place of Approve; Decline stays. The
            # submit refuses it as well, before anything is recorded.
            for old, new in _NOT_YOUR_NUMBER_PAGE:
                html = html.replace(old, new, 1)
        if sample and whose:
            # The person's own line, as on their chat page, then the
            # approved message about the form, not "your" form.
            html = html.replace(
                f'role="note">{beta.SAMPLE_FRAME}</div>',
                f'role="note">'
                f'{escape(beta.sample_frame(hub_view.relation_of(person)))}'
                '</div>', 1)
            for old, new in _CARED_FOR_REVIEW_WORDS:
                html = html.replace(old, new, 1)
        # The engine names its own product in the tab; here the page is
        # CareAgents', reached from a CareAgents chat or text.
        return _REVIEW_TAB_BRAND.sub(r"\1CareAgents\2", html, count=1)

    def _count_approval(agent_id):
        """One approval on a real-record assistant, for the weekly number
        (beta spec 4.5). The sample is not counted. Never fails a review."""
        acct = current_account()
        # The funnel counts an approval on any records, the sample included.
        funnel.safely(funnel.mark, svc.session, acct.id, "action_approved")
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
            ctx = _agent_action_context(agent_id, action_id)
        except OwnershipUnknown:
            return jsonify({"error": "review_unavailable",
                            "message": _REVIEW_UNCHECKABLE}), 503
        if not ctx:
            return jsonify({"error": "not yours"}), 404
        tenant = ctx["tenant"]
        if svc.is_paused(current_account().id):
            # Before the review is submitted, so nothing is confirmed or
            # executed for a paused account (beta spec 4.6, #856 review F1).
            return jsonify({"error": "records_paused",
                            "message": beta.PAUSED_HUB_TEXT}), 423
        if _text_not_yours(ctx.get("action") or {}):
            # BEFORE submit_review: that POST is what writes the consent
            # record, so a check after it would refuse a text already
            # approved. Nothing was recorded, so `confirmed` is false.
            return jsonify({"error": "not_your_number", "confirmed": False,
                            "message": _NOT_YOUR_NUMBER}), 409
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

    def _is_real(ctx: dict) -> bool:
        """Whether the agent is on anything but the sample, for the Mac
        relay and Telegram. Unconditional: SENDBLUE_REAL_RECORDS stands for
        Sendblue's BAA only, and these transports have none. An unknown
        kind counts as real."""
        return (ctx.get("connection") or {}).get("kind") != "sample"

    def _telegram_real_records():
        return jsonify({"error": "real_records",
                        "message": sendblue_surface.real_records_text(
                            cfg.origin)}), 409

    @app.post("/api/surfaces/telegram")
    @login_required
    @full_sign_in_required
    def connect_telegram():
        acct = current_account()
        body = request.get_json(silent=True) or {}
        agent_id = body.get("agent_id", "")
        ctx = svc.get_agent_context(acct.id, agent_id)
        if not ctx:
            return jsonify({"error": "unknown agent"}), 404
        if _is_real(ctx):
            # The gateway answers Telegram outside the run worker, so no
            # turn check here would hold it back: refuse the binding.
            return _telegram_real_records()
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
        if ctx and _is_real(ctx):
            # A code minted while sample does not expire; read fresh here,
            # so a switch to real records since then binds nothing.
            return _telegram_real_records()
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
            return beta.daily_limit_text()
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
        funnel.safely(funnel.mark, svc.session, account_id,
                      "imessage_linked")
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
    @full_sign_in_required
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
            conversation_id=conversation_id,
            # A real-records agent gets the app pointer and no turn is
            # queued, whatever SENDBLUE_REAL_RECORDS says (_is_real).
            transport_block=lambda ctx: (
                sendblue_surface.real_records_text(cfg.origin)
                if _is_real(ctx) else None))
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
    @full_sign_in_required
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
        if _is_real(ctx):
            # Read fresh at each poll, before anything about the run: an
            # assistant switched to real records mid-run, by kind or by a
            # move to a new tenant (where the run lookup would fail and the
            # relay retry forever), gets the pointer and nothing of the run.
            # A refusal: it carries the 911 line (the message is not at
            # hand here, so the line follows).
            return jsonify({"run_id": run_id,
                            "reply": beta.with_911(
                                sendblue_surface.real_records_text(
                                    cfg.origin))})
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
            "background_color": "#F6F8F7", "theme_color": "#0B6B5D",
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
        typed, key = email.strip().lower(), invites.invite_key(email)
        click.echo(f"invited {typed}" if created
                   else f"{typed} was already invited")
        if key != typed:
            click.echo(f"matches the inbox {key} (any alias of it)")
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
        """Each invite: as typed, the inbox it matches, when and by whom,
        live or revoked, and whether that inbox has signed up yet."""
        rows = invites.real_record_rows(svc.session)
        if not rows:
            click.echo("no invites")
        for r in rows:
            when = datetime.fromtimestamp(r["invited_at"], timezone.utc)
            state = ("revoked " + datetime.fromtimestamp(
                r["revoked_at"], timezone.utc).strftime("%Y-%m-%d")
                     if r["revoked_at"] else "live")
            joined = "signed up" if r["signed_up"] else "not signed up"
            click.echo(f"{r['display']:<40} matches {r['key']:<32} "
                       f"{when:%Y-%m-%d} by {r['invited_by']:<20} "
                       f"{state:<18} {joined}")

    # --- pause and the weekly number (beta spec 4.5, 4.6) ------------------

    operator_cli.register(app, svc)
    beta_signup.register(app, svc, cfg, first_run=_first_run)
    feedback.register(app, svc, cfg)
    errors.register(app, svc, cfg)
    invites.register(app, svc, cfg, beta_signup.page_context)
    funnel.register(app, svc, cfg, full_session=lambda: not session.get(
        _VIA_LINK))
    checkin_wallet.register(app, svc, cfg, hc,
                            full_session=lambda: not session.get(_VIA_LINK))
    checkins.register(app, svc, cfg)

    return app
