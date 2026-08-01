"""CareAgents Flask app — accounts, biometric auth, the health hub, and chat.

Identity model: a signed cookie holds `account_id` after passkey/email login.
Everything (connections, agents, surfaces) is account-scoped; a foreign id
reads as 404. Chat history lives in process memory keyed by (agent tenant).
Deploy with ONE gunicorn worker (threads for concurrency).

No PHI is stored here — health data lives in HealthClaw tenants behind the
guardrail layer; careagents holds identity + pointers only.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from functools import wraps
from threading import Lock

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for)

from careagents.accounts import (AccountService, AuthError, MailError,
                                 new_binding_code)
from careagents import advisors, connectors
from careagents.agent import run_turn, run_turn_to_message
from careagents.config import Config
from careagents.healthclaw import HealthClawClient, HealthClawError
from careagents.personas import DEFAULT_PERSONA, PERSONAS, system_prompt

logger = logging.getLogger(__name__)

# Bump when the consent-card copy or the terms/privacy content it points at
# changes materially. Stored per connection so we always know which version a
# person agreed to — a later change never silently claims earlier consent.
# 2026-08-01: the "leaving" clause said to email support, while self-serve
# Disconnect and Delete sat on the same page (#203). Understating our own
# strongest privacy control in the one place people read carefully.
CONSENT_VERSION = "2026-08-01"

# In-memory conversation bounds. One worker holds every live chat, so these
# cap memory and keep a long-running process from degrading (#218).
MAX_LIVE_CONVERSATIONS = 200
CONVERSATION_IDLE_SECONDS = 6 * 3600


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

    histories: dict[str, list] = defaultdict(list)
    hist_lock = Lock()
    turns: dict[str, deque] = defaultdict(deque)
    # Last time each conversation was touched, so idle ones can be released.
    # Without this every tenant that ever chatted keeps its full transcript in
    # memory for the life of the process.
    hist_seen: dict[str, float] = {}

    def load_history(tenant: str) -> list:
        """The in-memory conversation, rehydrated from HealthClaw if cold.

        Process memory is a cache, not the record. Deploys, restarts and idle
        eviction all empty it, and before this the agent simply forgot the
        person — a strange trait for a product promising a *persistent* health
        agent. Call under hist_lock.
        """
        history = histories[tenant]
        if not history:
            history.extend(hc.recent_messages(tenant))
        touch_history(tenant)
        return history

    def touch_history(tenant: str) -> None:
        """Mark a conversation live; release idle ones once we're holding many.

        Deliberately simple: only sweeps when the map is already large, so the
        common path is a single dict write. Evicting is now safe rather than
        lossy — a dropped conversation reloads from HealthClaw on next use.
        """
        seen_at = time.time()
        hist_seen[tenant] = seen_at
        if len(histories) <= MAX_LIVE_CONVERSATIONS:
            return
        for other, last in list(hist_seen.items()):
            if other != tenant and seen_at - last > CONVERSATION_IDLE_SECONDS:
                histories.pop(other, None)
                turns.pop(other, None)
                hist_seen.pop(other, None)

    # --- auth plumbing -------------------------------------------------------

    def current_account():
        aid = session.get("account_id")
        return svc.get_account(aid) if aid else None

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            if not session.get("account_id"):
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
            svc.start_email_code(email, purpose)
        except AuthError as exc:
            return jsonify({"error": str(exc)}), 400
        except MailError as exc:
            # Never report "sent" when nothing was sent — this is the front
            # door, and a silent failure leaves the person watching an empty
            # inbox with no idea whether to wait or retry.
            return jsonify({"error": str(exc), "sent": False}), 502
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
        if hc.tenant_has_records(conn_tenant):
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
        # Show the conversation they actually had. Rendering only the canned
        # greeting made every return visit look like a first visit.
        return render_template("chat.html", me=ctx["agent"], persona=p,
                               agent_id=agent_id,
                               past=hc.recent_messages(ctx["tenant"], limit=30))

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
        sysprompt = system_prompt(agent["name"], agent["persona"],
                                  agent.get("advisor"))

        def remember(role, body):
            """Best-effort persist. Losing the transcript is bad; failing the
            conversation in front of the person is worse."""
            try:
                hc.log_message(tenant, role, body, agent["id"])
            except Exception:  # noqa: BLE001
                logger.warning("chat turn not persisted for %s", tenant)

        # Record the question before streaming the answer: they definitely
        # asked it, even if they close the tab before the reply finishes.
        remember("user", text)

        def stream():
            with hist_lock:
                history = load_history(tenant)
            reply = []
            try:
                for event in run_turn(cfg, hc, tenant, sysprompt,
                                      history, text):
                    if event.get("type") == "text" and event.get("text"):
                        reply.append(event["text"])
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception:  # noqa: BLE001
                yield ('data: {"type": "error", "text": '
                       '"Something went wrong on our side."}\n\n')
            if reply:
                remember("assistant", "\n\n".join(reply))
            yield 'data: {"type": "done"}\n\n'

        return Response(stream(), mimetype="text/event-stream",
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
        except HealthClawError:
            return jsonify({"status": "unknown"}), 404
        outcome = {}
        try:
            outcome = json.loads(status.get("outcome_summary") or "{}")
        except ValueError:
            pass
        return jsonify({"status": status.get("status"),
                        "delivery_link": outcome.get("delivery_link")})

    # --- review relay (credential-injecting proxy, agent-scoped) -------------

    def _agent_owns_action(agent_id, action_id):
        acct = current_account()
        ctx = svc.get_agent_context(acct.id, agent_id) if acct else None
        if not ctx:
            return None
        try:
            hc.action_status(ctx["tenant"], action_id)
            return ctx["tenant"]
        except HealthClawError:
            return None

    @app.get("/review/<agent_id>/<action_id>")
    @login_required
    def review(agent_id, action_id):
        tenant = _agent_owns_action(agent_id, action_id)
        if not tenant:
            return render_template("chat_error.html",
                                   message="That form isn't yours."), 404
        status, html = hc.fetch_review_page(tenant, action_id)
        if status != 200:
            return render_template(
                "chat_error.html",
                message="This form is no longer awaiting review."), 404
        html = html.replace(f"/r6/actions/{action_id}/review",
                            f"/review/{agent_id}/{action_id}/submit")
        return html

    @app.post("/review/<agent_id>/<action_id>/submit")
    @login_required
    def review_submit(agent_id, action_id):
        tenant = _agent_owns_action(agent_id, action_id)
        if not tenant:
            return jsonify({"error": "not yours"}), 404
        decisions = request.get_json(silent=True) or dict(request.form)
        status, body = hc.submit_review(tenant, action_id, decisions)
        if status == 200:
            try:
                hc.confirm_action(tenant, action_id)
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
        if not _allow_turn(surface["account_id"]):
            return jsonify({"reply": "One moment — too many messages just now. "
                                     "Try again in a bit."}), 200
        tenant = ctx["tenant"]
        agent = ctx["agent"]
        sysprompt = system_prompt(agent["name"], agent["persona"],
                                  agent.get("advisor"))
        with hist_lock:
            history = load_history(tenant)
        try:
            reply = run_turn_to_message(cfg, hc, tenant, sysprompt, history,
                                        text, origin=cfg.origin,
                                        agent_id=agent["id"])
        except Exception:  # noqa: BLE001
            reply = "Something went wrong on our side. Please try again."
        else:
            # Same store as the web surface, so a conversation started in
            # Telegram continues on the web and vice versa.
            hc.log_message(tenant, "user", text, agent["id"])
            hc.log_message(tenant, "assistant", reply, agent["id"])
        return jsonify({"reply": reply})

    # --- trust + ops ---------------------------------------------------------

    @app.get("/api/trust")
    def trust():
        badge = hc.conformance_badge()
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
        """
        accounts_ok = svc.ping()
        body = {"status": "ok" if accounts_ok else "degraded",
                "provider": cfg.provider, "accounts": accounts_ok}
        return jsonify(body), (200 if accounts_ok else 503)

    return app
