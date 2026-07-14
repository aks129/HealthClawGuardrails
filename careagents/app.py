"""CareAgents Flask app — routes, sessions, and the review relay.

State model (v1): the signed cookie holds identity {tenant, name, persona};
chat history lives in process memory keyed by tenant. Deploy with ONE gunicorn
worker (threads for concurrency) so history is coherent; the deploy unit
encodes that. History loss on restart degrades gracefully (the agent re-reads
records via tools).
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque
from threading import Lock

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for)
from itsdangerous import BadSignature, URLSafeTimedSerializer

from careagents.agent import run_turn
from careagents.config import Config
from careagents.healthclaw import HealthClawClient, HealthClawError
from careagents.personas import DEFAULT_PERSONA, PERSONAS, system_prompt

MAGIC_LINK_MAX_AGE = 90 * 24 * 3600
_NAME_RE = re.compile(r"^[A-Za-z0-9 .'\-]{1,32}$")


def create_app(config: Config | None = None,
               client: HealthClawClient | None = None) -> Flask:
    cfg = config or Config()
    app = Flask(__name__)
    app.secret_key = cfg.session_secret
    app.config.update(SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=(cfg.app_env == "production"))
    hc = client or HealthClawClient(cfg.healthclaw_base, cfg.mint_secret)
    magic = URLSafeTimedSerializer(cfg.session_secret, salt="careagents-magic")

    histories: dict[str, list] = defaultdict(list)
    hist_lock = Lock()
    turns: dict[str, deque] = defaultdict(deque)  # rate limit windows

    def _me() -> dict | None:
        t = session.get("tenant")
        if not t or not str(t).startswith("ca-"):
            return None
        return {"tenant": t,
                "name": session.get("agent_name") or "Your agent",
                "persona": session.get("persona") or DEFAULT_PERSONA}

    def _allow_turn(tenant: str) -> bool:
        window = turns[tenant]
        now = time.time()
        while window and now - window[0] > cfg.chat_window_seconds:
            window.popleft()
        if len(window) >= cfg.chat_turns_per_window:
            return False
        window.append(now)
        return True

    # --- pages ---------------------------------------------------------------

    @app.get("/")
    def landing():
        return render_template("landing.html", personas=PERSONAS,
                               me=_me())

    @app.get("/start")
    def start():
        return render_template("setup.html", personas=PERSONAS)

    @app.post("/start")
    def create_agent():
        name = (request.form.get("agent_name") or "").strip() or "Juniper"
        if not _NAME_RE.match(name):
            name = "Juniper"
        persona = request.form.get("persona") or DEFAULT_PERSONA
        if persona not in PERSONAS:
            persona = DEFAULT_PERSONA
        tenant = hc.new_tenant_id()
        try:
            hc.seed(tenant)
            hc.mint_token(tenant)
        except HealthClawError:
            return render_template("setup.html", personas=PERSONAS,
                                   error=("The guardrail layer is not "
                                          "reachable right now — try again "
                                          "in a minute.")), 503
        session.permanent = True
        session.update(tenant=tenant, agent_name=name, persona=persona)
        return redirect(url_for("chat"))

    @app.get("/chat")
    def chat():
        me = _me()
        if not me:
            return redirect(url_for("start"))
        p = PERSONAS[me["persona"]]
        token = magic.dumps({"t": me["tenant"], "n": me["name"],
                             "p": me["persona"]})
        return render_template("chat.html", me=me, persona=p,
                               magic_link=url_for("magic", token=token,
                                                  _external=True))

    @app.get("/m/<token>", endpoint="magic")
    def magic_entry(token):
        try:
            data = magic.loads(token, max_age=MAGIC_LINK_MAX_AGE)
        except BadSignature:
            return render_template("setup.html", personas=PERSONAS,
                                   error="That link has expired — start a "
                                         "fresh agent below."), 400
        session.permanent = True
        session.update(tenant=data["t"], agent_name=data["n"],
                       persona=data["p"])
        return redirect(url_for("chat"))

    # --- chat API (SSE) -------------------------------------------------------

    @app.post("/api/chat")
    def api_chat():
        me = _me()
        if not me:
            return jsonify({"error": "no agent session"}), 401
        text = ((request.get_json(silent=True) or {}).get("message")
                or "").strip()
        if not text or len(text) > 2000:
            return jsonify({"error": "message must be 1-2000 characters"}), 400
        if not _allow_turn(me["tenant"]):
            return jsonify({"error": "rate_limited"}), 429

        sysprompt = system_prompt(me["name"], me["persona"])
        tenant = me["tenant"]

        def stream():
            with hist_lock:
                history = histories[tenant]
            try:
                for event in run_turn(cfg, hc, tenant, sysprompt,
                                      history, text):
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception:  # noqa: BLE001 — never leak internals to SSE
                yield ('data: {"type": "error", "text": '
                       '"Something went wrong on our side."}\n\n')
            yield 'data: {"type": "done"}\n\n'

        return Response(stream(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    # --- review relay (credential-injecting proxy) ----------------------------

    def _owned_action_or_none(action_id: str, tenant: str) -> bool:
        """The layer scopes actions by tenant: a foreign id reads as 404."""
        try:
            hc.action_status(tenant, action_id)
            return True
        except HealthClawError:
            return False

    @app.get("/review/<action_id>")
    def review(action_id):
        me = _me()
        if not me:
            return redirect(url_for("start"))
        if not _owned_action_or_none(action_id, me["tenant"]):
            return render_template("chat_error.html",
                                   message="That form isn't yours."), 404
        status, html = hc.fetch_review_page(me["tenant"], action_id)
        if status != 200:
            return render_template(
                "chat_error.html",
                message="This form is no longer awaiting review."), 404
        # Repoint the page's own API calls at our relay (same-origin).
        html = html.replace(f"/r6/actions/{action_id}/review",
                            f"/review/{action_id}/submit")
        return html

    @app.post("/review/<action_id>/submit")
    def review_submit(action_id):
        me = _me()
        if not me:
            return jsonify({"error": "no agent session"}), 401
        if not _owned_action_or_none(action_id, me["tenant"]):
            return jsonify({"error": "not yours"}), 404
        decisions = request.get_json(silent=True) or dict(request.form)
        status, body = hc.submit_review(me["tenant"], action_id, decisions)
        if status == 200:
            # The human approved every item — run the out-of-band confirm so
            # the PDF renders; the chat picks the link up via check_form_status.
            try:
                hc.confirm_action(me["tenant"], action_id)
            except HealthClawError:
                pass  # surfaced by check_form_status as not-completed
        return jsonify(body), status

    @app.get("/api/form/<action_id>")
    def form_status(action_id):
        """Chat polls this after the review tab opens; when the human has
        approved and the layer has executed, the PDF card drops into chat."""
        me = _me()
        if not me:
            return jsonify({"error": "no agent session"}), 401
        try:
            status = hc.action_status(me["tenant"], action_id)
        except HealthClawError:
            return jsonify({"status": "unknown"}), 404
        outcome = {}
        try:
            outcome = json.loads(status.get("outcome_summary") or "{}")
        except ValueError:
            pass
        return jsonify({"status": status.get("status"),
                        "delivery_link": outcome.get("delivery_link")})

    # --- trust + ops -----------------------------------------------------------

    @app.get("/api/trust")
    def trust():
        me = _me()
        badge = hc.conformance_badge()
        audits = None
        if me:
            try:
                bundle = hc.search(me["tenant"], "AuditEvent", {"_count": 1})
                audits = bundle.get("total")
            except HealthClawError:
                audits = None
        return jsonify({"badge": badge.get("message", "unavailable"),
                        "audit_events": audits})

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "provider": cfg.provider})

    return app
