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
import time
import uuid
from collections import defaultdict, deque
from functools import wraps

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for)

from careagents.accounts import (AccountService, AuthError, MailError,
                                 MailUnconfirmed, new_binding_code)
from careagents import advisors, connectors
from careagents import intake_state
from careagents import labs_timeline as labs_timeline_mod
from careagents.agent import GENERIC_FAILURE_TEXT
from careagents.config import Config
from careagents.healthclaw import (HealthClawClient, HealthClawError,
                                   HealthClawUnconfirmed)
from careagents.personas import DEFAULT_PERSONA, PERSONAS

logger = logging.getLogger(__name__)

# Bump when the consent-card copy or the terms/privacy content it points at
# changes materially. Stored per connection so we always know which version a
# person agreed to — a later change never silently claims earlier consent.
# 2026-08-01: the "leaving" clause said to email support, while self-serve
# Disconnect and Delete sat on the same page (#203). Understating our own
# strongest privacy control in the one place people read carefully.
CONSENT_VERSION = "2026-08-01"

# `/healthz` asks HealthClaw whether a run worker is present. That call gets
# its own budget rather than the client's 25s chat timeout: as a
# `(connect, read)` pair the worst case is 2.0s of network wait, which fits
# inside the 4s the container probe allows (`careagents/healthcheck.py`) and
# the Dockerfile's `HEALTHCHECK --timeout=5s`. The probe measured the old
# shape taking 25.01s to answer at zero load, purely because it inherited the
# chat timeout (docs/evidence/2026-09-03-probe-219-thread-saturation.md §8,
# PR #573).
_HEALTHZ_WORKER_TIMEOUT = (1.0, 1.0)

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

_BRIEF_SECTION_PREFIX = "https://healthclaw.io/fhir/StructureDefinition/brief-section-"


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


def _parse_brief_sections(resource: dict) -> dict[str, list[dict]]:
    """Deserialize a FHIR Basic AppointmentBrief into section→field lists.

    Each section is a list of dicts with keys: label, value, sourceType, sourceId.
    Returns {} on any parse error so the template always gets a plain dict —
    empty sections render as 'not available from connected records'.
    """
    out: dict[str, list[dict]] = {}
    try:
        for ext in resource.get("extension", []):
            url = ext.get("url", "")
            if not url.startswith(_BRIEF_SECTION_PREFIX):
                continue
            name = url[len(_BRIEF_SECTION_PREFIX):]
            fields = []
            for fe in ext.get("extension", []):
                raw = fe.get("valueString")
                if raw:
                    try:
                        fields.append(json.loads(raw))
                    except (ValueError, TypeError):
                        pass
            out[name] = fields
    except (AttributeError, TypeError):
        pass
    return out


# Mirrors r6.brief.engine.CARE_GAPS_OK. CareAgents talks to HealthClaw over
# HTTP and imports nothing from it, so the string is repeated rather than
# shared.
_CARE_GAPS_OK = "ok"


def _parse_care_gaps_status(resource: dict | None) -> str:
    """Whether the screening review ran, from the brief's care-gaps section.

    Anything short of an explicit "ok" — no brief, no care-gaps section, no
    marker, an unparseable payload — is not an evaluation, and the page must
    not render it as "nothing due" (#381). Callers get "" for all of those.
    """
    care_gaps_url = _BRIEF_SECTION_PREFIX + "care-gaps"
    try:
        for ext in (resource or {}).get("extension", []):
            if ext.get("url") != care_gaps_url:
                continue
            for sub in ext.get("extension", []):
                if sub.get("url") == "status":
                    return sub.get("valueString") or ""
    except (AttributeError, TypeError):
        pass
    return ""


def create_app(config: Config | None = None,
               client: HealthClawClient | None = None,
               accounts: AccountService | None = None) -> Flask:
    cfg = config or Config()
    app = Flask(__name__)
    app.secret_key = cfg.session_secret
    app.config.update(SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=(cfg.app_env == "production"),
                      PERMANENT_SESSION_LIFETIME=90 * 24 * 3600)
    hc = client or HealthClawClient(cfg.healthclaw_base, cfg.mint_secret)
    svc = accounts or AccountService(cfg)
    # Exposed for deterministic integration tests and process diagnostics.
    # Production Gunicorn never executes this worker object; the systemd/OCI
    # worker service constructs its own clients and calls careagents.worker.
    app.extensions["careagents_runtime"] = {
        "config": cfg, "client": hc, "accounts": svc}

    turns: dict[str, deque] = defaultdict(deque)

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
                    session.clear()
                if request.path.startswith("/api/") or request.path.startswith(
                        "/webauthn/"):
                    return jsonify({"error": "sign in"}), 401
                return redirect(url_for("auth"))
            return fn(*a, **k)
        return wrapper

    def _login(account):
        session.clear()
        session.permanent = True
        session["account_id"] = account.id

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
                               terms_url=f"{cfg.healthclaw_base}/terms",
                               privacy_url=f"{cfg.healthclaw_base}/privacy")

    @app.get("/home")
    @login_required
    def home():
        acct = current_account()
        data = svc.list_home(acct.id)
        return render_template(
            "home.html", me=acct, personas=PERSONAS,
            connections=data["connections"], agents=data["agents"],
            surfaces=data["surfaces"], has_passkey=svc.has_passkey(acct.id),
            telegram_bot=cfg.telegram_bot,
            imessage_handle=cfg.imessage_handle,
            terms_url=f"{cfg.healthclaw_base}/terms",
            privacy_url=f"{cfg.healthclaw_base}/privacy",
            advisors=advisors.catalog(),
            catalog=connectors.catalog(cfg))

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

    # --- connections ---------------------------------------------------------

    @app.get("/api/connections/catalog")
    @login_required
    def connections_catalog():
        return jsonify({"connectors": connectors.catalog(cfg)})

    @app.post("/api/connections/<connector_id>")
    @login_required
    def add_connection(connector_id):
        acct = current_account()
        body = request.get_json(silent=True) or {}
        plan = connectors.start(connector_id, body.get("provider"), cfg, hc)
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
            if body.get("consent") is not True:
                return jsonify({"error": "consent_required",
                                "consent_version": CONSENT_VERSION}), 428
            consent_version = CONSENT_VERSION
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
        out = {"id": cid, "status": plan["status"]}
        if plan.get("connect_url"):
            out["connect_url"] = plan["connect_url"]
        return jsonify(out)

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
        if landed > 0:
            try:
                svc.set_connection_status(conn["tenant_id"], "active")
            except Exception:  # noqa: BLE001
                logger.warning(
                    "could not flip connection %s to active", conn_id)
            try:
                svc.mark_synced(conn_id, hc.record_count(conn["tenant_id"]))
            except HealthClawError:
                logger.warning("record_count after upload failed for %s",
                               conn_id)

        # Strip the engine's internal `tenant_id` before the browser sees
        # the response — it is not a fact the UI needs and leaking it here
        # would give the browser a token to try elsewhere.
        response = {k: v for k, v in result.items() if k != "tenant_id"}
        response["connection_id"] = conn_id
        return jsonify(response), 200

    @app.post("/api/connections/<conn_id>/disconnect")
    @login_required
    def disconnect_connection(conn_id):
        """Stop new data flowing; keep records already collected."""
        acct = current_account()
        if not svc.revoke_connection(acct.id, conn_id):
            return jsonify({"error": "unknown connection"}), 404
        return jsonify({"status": "revoked", "connection_id": conn_id})

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
            # Never claim a deletion that did not happen.
            return jsonify({"error": "deletion_failed", "deleted": False,
                            "message": "Your records were not deleted. "
                                       "Nothing was changed — please retry."}), 502
        svc.delete_connection(acct.id, conn_id)
        return jsonify({
            "deleted": True,
            "connection_id": conn_id,
            "rows_deleted": purged.get("rows_deleted", 0),
            "audit_retained": True,
            "message": ("Your records were deleted. The PHI-free audit trail "
                        "is kept as the record of who accessed what, and this "
                        "deletion was added to it."),
        })

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

        body = request.get_json(silent=True) or {}
        plan = connectors.refresh(conn["kind"], conn["tenant_id"],
                                  body.get("provider"), cfg, hc)
        if plan.get("error"):
            return jsonify({"error": plan["error"]}), plan.get("code", 400)
        if plan.get("unsupported"):
            return jsonify({"unsupported": True, "reason": plan["reason"]})

        # Same server-side consent gate as the initial connect: a client that
        # skips the card is refused here, on every surface.
        if plan.get("requires_consent") and body.get("consent") is not True:
            return jsonify({"error": "consent_required",
                            "consent_version": CONSENT_VERSION}), 428

        # Baseline the count BEFORE re-authorizing so the follow-up poll can
        # report what the refresh actually added.
        try:
            svc.mark_synced(conn_id, hc.record_count(conn["tenant_id"]))
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
            svc.set_connection_status(conn_tenant, "active")
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
                if current is not None:
                    out["record_count"] = current
                    out["new_records"] = max(0, current - int(baseline))
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

    @app.get("/chat")
    @login_required
    def chat():
        acct = current_account()
        agent_id = request.args.get("agent", "")
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
        return render_template("chat.html", me=ctx["agent"], persona=p,
                               agent_id=agent_id,
                               conversation_id=conversation_id,
                               past=past,
                               history_lost=history_lost,
                               intake=intake,
                               summary_counts=intake.counts)

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
                                             == _CARE_GAPS_OK))

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
            time.sleep(cfg.run_sse_poll_seconds)
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
        # Durable daily ceiling — survives restarts and is shared across
        # workers, so it is the real bound on per-account inference spend.
        allowed, used = svc.claim_daily_turn(acct.id, cfg.chat_turns_per_day)
        if not allowed:
            return jsonify({
                "error": "daily_limit_reached",
                "used": used,
                "limit": cfg.chat_turns_per_day,
                "message": ("You've reached today's message limit. It resets "
                            "at midnight UTC."),
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

    @app.get("/api/form/<action_id>")
    @login_required
    def form_status(action_id):
        acct = current_account()
        agent_id = request.args.get("agent", "")
        ctx = svc.get_agent_context(acct.id, agent_id)
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
        return jsonify({"status": status.get("status"),
                        "delivery_link": outcome.get("delivery_link")})

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

    # --- review relay (credential-injecting proxy, agent-scoped) -------------

    def _agent_owns_action(agent_id, action_id):
        """The tenant that owns this action, or None if it is not this
        agent's.

        Raises OwnershipUnknown when the engine could not be asked. An
        outage is not evidence about ownership, and this is the
        human-approval path — the guardrail the product exists to
        guarantee (#410).
        """
        acct = current_account()
        ctx = svc.get_agent_context(acct.id, agent_id) if acct else None
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
    # statement about ownership; saying nothing was approved is true in every
    # failure mode here, because nothing was.
    _REVIEW_UNCHECKABLE = ("We couldn't check this form right now. Nothing "
                           "has been approved — please try again in a moment.")
    #: Distinct from the above: the review was SENT and the engine never
    #: answered, so whether the decisions were recorded is unknown. What is
    #: known is that the approval step below was never reached.
    _REVIEW_UNSUBMITTED = ("We couldn't confirm your review reached your "
                           "records. Nothing has been approved — please open "
                           "the form again to check before approving, because "
                           "approving twice could send it twice.")

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
            return render_template(
                "chat_error.html",
                message="This form is no longer awaiting review."), 404
        html = html.replace(f"/r6/actions/{action_id}/review",
                            f"/review/{agent_id}/{action_id}/submit")
        return html

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
        decisions = request.get_json(silent=True) or dict(request.form)
        try:
            status, body = hc.submit_review(tenant, action_id, decisions)
        except HealthClawError:
            # The decisions are gone and nobody told the patient. Ownership
            # was already confirmed, so this is not a permission answer.
            logger.exception("review submit failed for %s", action_id)
            return jsonify({
                "error": "review_unavailable",
                "confirmed": False,
                "message": _REVIEW_UNSUBMITTED,
            }), 503
        if status and not HealthClawClient._answered_about_data(status) \
                and status != 200:
            # The engine never answered, so we cannot say the review was
            # saved. We CAN say nothing was approved: confirm_action is only
            # reached on a 200 below, and that is the half that keeps a
            # second approval from sending the request twice.
            logger.warning("review submit unanswered (%s) for %s",
                           status, action_id)
            return jsonify({
                "error": "review_unavailable",
                "confirmed": False,
                "message": _REVIEW_UNSUBMITTED,
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
                    "message": ("Your review was saved, but we couldn't "
                                "confirm whether the approval went through. "
                                "Don't approve again yet — check the request's "
                                "status first, because approving twice could "
                                "send it twice."),
                })
                return jsonify(body), 502
            except HealthClawError:
                # The review was recorded but the confirmation didn't land, so
                # the action is still sitting unexecuted. Swallowing this told
                # the person they'd approved something that would never happen.
                # Say so plainly and let them retry — same posture as the
                # delete flow, which never claims an outcome it didn't get.
                logger.exception("confirm failed after review for %s", action_id)
                body = dict(body) if isinstance(body, dict) else {}
                body.update({
                    "confirmed": False,
                    "message": ("Your review was saved, but we couldn't submit "
                                "the approval. Nothing has been sent — please "
                                "try approving again."),
                })
                return jsonify(body), 502
            body = dict(body) if isinstance(body, dict) else {}
            body["confirmed"] = True
        return jsonify(body), status

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
        if request.headers.get("X-Internal-Secret") != cfg.mint_secret:
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
    # iMessage message loop itself: a Mac-mini relay POSTs inbound texts here
    # (mint-secret gated) and we return the agent's reply for it to send back.

    @app.post("/api/surfaces/imessage")
    @login_required
    def connect_imessage():
        acct = current_account()
        body = request.get_json(silent=True) or {}
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
                            "iMessage isn't configured on this deployment yet.")})

    @app.post("/api/surfaces/imessage/bind")
    def imessage_bind():
        """Relay calls this when a user texts `care <code>`: bind the sender's
        handle to the pending surface. Mint-secret gated (server-to-server)."""
        if request.headers.get("X-Internal-Secret") != cfg.mint_secret:
            return jsonify({"error": "forbidden"}), 403
        body = request.get_json(silent=True) or {}
        code = str(body.get("code") or "").replace("care_", "").replace(
            "care ", "").strip()
        handle = str(body.get("handle") or "").strip()
        if not handle:
            return jsonify({"error": "missing handle"}), 400
        surface = svc.find_surface_by_code(code, kind="imessage")
        if not surface:
            return jsonify({"error": "unknown code"}), 404
        svc.bind_surface(surface["id"], handle)
        return jsonify({"ok": True})

    @app.post("/api/surfaces/imessage/inbound")
    def imessage_inbound():
        """Relay POSTs an inbound message {handle, text}; we route it to the
        bound agent and return {reply} for the relay to send back."""
        if request.headers.get("X-Internal-Secret") != cfg.mint_secret:
            return jsonify({"error": "forbidden"}), 403
        body = request.get_json(silent=True) or {}
        handle = str(body.get("handle") or "").strip()
        text = (body.get("text") or "").strip()
        surface = svc.find_surface_by_handle(handle, kind="imessage")
        if not surface:
            return jsonify({"error": "unbound handle"}), 404
        ctx = svc.get_agent_context(surface["account_id"], surface["agent_id"])
        if not ctx:
            return jsonify({"error": "unknown agent"}), 404
        if not text or len(text) > 2000:
            return jsonify({"error": "message must be 1-2000 characters"}), 400
        workers = _worker_state(timeout=_ADMISSION_WORKER_TIMEOUT)
        if workers != WORKERS_READY:
            return _refuse_turn_without_workers(workers)
        if not _allow_turn(surface["account_id"]):
            return jsonify({"reply": "One moment — too many messages just now. "
                                     "Try again in a bit."}), 200
        tenant = ctx["tenant"]
        agent = ctx["agent"]
        conversation_id = (body.get("conversation_id")
                           or hc.conversation_id(agent["id"]))
        request_id = str(body.get("request_id") or uuid.uuid4())
        try:
            created, user_message_id = hc.claim_inbound_message(
                tenant, text, agent["id"], conversation_id,
                "imessage", request_id)
            if created is None or not user_message_id:
                return jsonify({"error": "message store unavailable"}), 503
            run = hc.create_agent_run(
                tenant, user_message_id, cfg.run_deadline_seconds)
        except HealthClawError:
            return jsonify({"error": "run queue unavailable"}), 503
        return jsonify({"run_id": run["id"], "status": run["status"],
                        "duplicate": not created}), 202

    @app.get("/api/surfaces/imessage/runs/<run_id>")
    def imessage_run_result(run_id):
        """Mint-secret-gated projection polled by the Mac relay."""
        if request.headers.get("X-Internal-Secret") != cfg.mint_secret:
            return jsonify({"error": "forbidden"}), 403
        handle = str(request.args.get("handle") or "").strip()
        surface = svc.find_surface_by_handle(handle, kind="imessage")
        if not surface:
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
        if page.get("status") not in (
                "completed", "failed", "cancelled", "waiting_for_human"):
            return jsonify({"run_id": run_id,
                            "status": page.get("status")}), 202

        parts: list[str] = []
        extras: list[str] = []
        for raw in page.get("events") or []:
            event = _event_for_browser(raw)
            if not event:
                continue
            if event.get("type") == "text" and event.get("text"):
                parts.append(event["text"])
            elif event.get("type") == "card" and (
                    event.get("kind") == "review"):
                extras.append(
                    "I've prepared a form for your review — approve each "
                    f"item here: {cfg.origin}/review/{surface['agent_id']}/"
                    f"{event.get('action_id', '')}")
            elif event.get("type") == "card" and (
                    event.get("kind") == "pdf" and event.get("url")):
                extras.append(
                    f"Your signed document is ready: {event['url']}")
            elif event.get("type") == "error":
                parts.append(event.get("text") or GENERIC_FAILURE_TEXT)
        reply = "\n\n".join([*parts, *extras]).strip() or (
            GENERIC_FAILURE_TEXT + " Please try again.")
        return jsonify({"run_id": run_id, "status": page.get("status"),
                        "reply": reply})

    # --- trust + ops ---------------------------------------------------------

    @app.get("/api/trust")
    def trust():
        try:
            badge = hc.conformance_badge()
        except HealthClawError:
            # The badge is a claim about the engine. Unreachable means we do
            # not have one — the same honest answer a non-200 already gives,
            # rather than a 500 on the trust panel (#403).
            badge = {}
        return jsonify({"badge": badge.get("message", "unavailable")})

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

    return app
