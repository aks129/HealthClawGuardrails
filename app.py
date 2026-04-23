"""
Flask application routes for HealthClaw Guardrails.

Web UI routes:
- / (landing page)
- /r6-dashboard (Health Data Dashboard — FHIR interactive showcase)
- /faq (Frequently Asked Questions)
- /wiki (Project Wiki)
"""

import os

from flask import Response, render_template, request
from main import app


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
