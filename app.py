"""
Flask application routes for HealthClaw Guardrails.

Web UI routes:
- / (landing page)
- /r6-dashboard (Health Data Dashboard — FHIR interactive showcase)
- /faq (Frequently Asked Questions)
- /wiki (Project Wiki)
- POST /api/subscribe (newsletter sign-up via Resend Audiences API)
"""

import logging
import os

import httpx
from email_validator import EmailNotValidError, validate_email
from flask import Response, jsonify, render_template, request
from main import app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Security headers — applied to every response
# ---------------------------------------------------------------------------
@app.after_request
def _security_headers(response):
    # Default content security: locked-down but permissive enough for the
    # current Bootstrap/FontAwesome CDN dependencies.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), microphone=(), camera=(), usb=()",
    )
    # HSTS: only emit when behind HTTPS (avoid breaking localhost dev).
    if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


# ---------------------------------------------------------------------------
# robots.txt — deny indexing of the personal tenant host
# ---------------------------------------------------------------------------
@app.route('/robots.txt')
def robots_txt():
    """
    Personal deployments (app.healthclaw.io) return a blanket disallow to
    keep them out of search indexes. Public-demo deployments (healthclaw.io)
    can set ALLOW_INDEXING=1 to serve a permissive robots.txt instead.
    """
    if os.environ.get("ALLOW_INDEXING", "").lower() in ("1", "true", "yes"):
        body = "User-agent: *\nAllow: /\n"
    else:
        body = "User-agent: *\nDisallow: /\n"
    return Response(body, mimetype="text/plain")


@app.route('/')
def index():
    """Landing page."""
    return render_template('index.html')


@app.route('/r6-dashboard')
def r6_dashboard():
    """Health Data Dashboard (FHIR) — interactive guardrail showcase."""
    return render_template('r6_dashboard.html')


@app.route('/faq')
def faq():
    """Frequently Asked Questions."""
    return render_template('faq.html')


@app.route('/wiki')
def wiki():
    """Project Wiki — architecture, concepts, and how-tos."""
    return render_template('wiki.html')


@app.route('/privacy')
def privacy():
    """Privacy Policy."""
    return render_template('privacy.html')


@app.route('/terms')
def terms():
    """Terms & Conditions."""
    return render_template('terms.html')


# ---------------------------------------------------------------------------
# Newsletter sign-up — POSTs the email to a Resend Audience.
#
# Resend uses the same domain that already serves healthclaw.io email
# (privacy@, security@, legal@). When sending verification or update emails,
# we'd use updates@healthclaw.io as the From — but for now this endpoint only
# stores the contact in the audience and lets Resend's broadcast UI handle the
# outbound side.
#
# Required env: RESEND_API_KEY, RESEND_AUDIENCE_ID
# If neither is set, the endpoint returns 503 so we never silently drop signups.
# ---------------------------------------------------------------------------
RESEND_CONTACTS_URL = "https://api.resend.com/audiences/{audience_id}/contacts"


@app.route('/api/subscribe', methods=['POST'])
def api_subscribe():
    payload = request.get_json(silent=True) or request.form
    raw_email = (payload.get("email") or "").strip()

    if not raw_email:
        return jsonify({"error": "email is required"}), 400

    try:
        email = validate_email(raw_email, check_deliverability=False).normalized
    except EmailNotValidError as exc:
        return jsonify({"error": f"invalid email: {exc}"}), 400

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    audience_id = os.environ.get("RESEND_AUDIENCE_ID", "").strip()
    if not api_key or not audience_id:
        logger.warning("subscribe: Resend not configured — RESEND_API_KEY/AUDIENCE_ID missing")
        return jsonify({"error": "subscriptions are not configured"}), 503

    try:
        resp = httpx.post(
            RESEND_CONTACTS_URL.format(audience_id=audience_id),
            headers={"Authorization": f"Bearer {api_key}"},
            json={"email": email, "unsubscribed": False},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("subscribe: Resend network error: %s", exc)
        return jsonify({"error": "could not reach the mail provider"}), 502

    if resp.status_code in (200, 201):
        return jsonify({"ok": True, "email": email}), 200

    # Resend returns 422 with name=validation_error for duplicates — treat as success.
    if resp.status_code == 422:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if "already exist" in (body.get("message") or "").lower():
            return jsonify({"ok": True, "email": email, "already_subscribed": True}), 200

    logger.warning("subscribe: Resend returned %s: %s", resp.status_code, resp.text[:200])
    return jsonify({"error": "could not save subscription"}), 502
