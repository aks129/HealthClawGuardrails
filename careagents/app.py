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
import time
from collections import defaultdict, deque
from functools import wraps
from threading import Lock

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for)

from careagents.accounts import (AccountService, AuthError, new_binding_code)
from careagents.agent import run_turn, run_turn_to_message
from careagents.config import Config
from careagents.healthclaw import HealthClawClient, HealthClawError
from careagents.personas import DEFAULT_PERSONA, PERSONAS, system_prompt


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
        if session.get("account_id"):
            return redirect(url_for("home"))
        return render_template("auth.html", rp_id=cfg.rp_id)

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
            imessage_handle=cfg.imessage_handle)

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

    @app.post("/api/connections/sample")
    @login_required
    def add_sample_connection():
        acct = current_account()
        tenant = hc.new_tenant_id()
        try:
            hc.seed(tenant)
        except HealthClawError:
            return jsonify({"error": "records service unavailable"}), 503
        cid = svc.add_connection(acct.id, "sample", tenant, "Sample records",
                                 status="active", provider="CareAgents sample")
        return jsonify({"id": cid, "status": "active"})

    @app.post("/api/connections/fasten")
    @login_required
    def add_fasten_connection():
        acct = current_account()
        if not cfg.fasten_public_key:
            return jsonify({"error": "real-records connect isn't configured "
                                     "on this deployment yet"}), 503
        tenant = hc.new_tenant_id()
        cid = svc.add_connection(acct.id, "fasten", tenant,
                                 "My health provider", status="pending",
                                 provider="Connecting…")
        url = hc.fasten_connect_url(tenant)
        return jsonify({"id": cid, "connect_url": url, "status": "pending"})

    @app.get("/api/connections/<conn_tenant>/poll")
    @login_required
    def poll_connection(conn_tenant):
        acct = current_account()
        # ownership: the tenant must belong to one of the account's connections
        owned = {c["tenant_id"] for c in svc.list_home(acct.id)["connections"]}
        if conn_tenant not in owned:
            return jsonify({"error": "not yours"}), 404
        if hc.tenant_has_records(conn_tenant):
            svc.set_connection_status(conn_tenant, "active")
            return jsonify({"status": "active"})
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
        try:
            aid = svc.create_agent(acct.id, name, persona,
                                   body.get("connection_id", ""))
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
        return render_template("chat.html", me=ctx["agent"], persona=p,
                               agent_id=agent_id)

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

        tenant = ctx["tenant"]
        agent = ctx["agent"]
        sysprompt = system_prompt(agent["name"], agent["persona"])

        def stream():
            with hist_lock:
                history = histories[tenant]
            try:
                for event in run_turn(cfg, hc, tenant, sysprompt,
                                      history, text):
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception:  # noqa: BLE001
                yield ('data: {"type": "error", "text": '
                       '"Something went wrong on our side."}\n\n')
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
                pass
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
        sysprompt = system_prompt(agent["name"], agent["persona"])
        with hist_lock:
            history = histories[tenant]
        try:
            reply = run_turn_to_message(cfg, hc, tenant, sysprompt, history,
                                        text, origin=cfg.origin,
                                        agent_id=agent["id"])
        except Exception:  # noqa: BLE001
            reply = "Something went wrong on our side. Please try again."
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
        return jsonify({"status": "ok", "provider": cfg.provider,
                        "accounts": True})

    return app
