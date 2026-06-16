"""
Flask application routes for HealthClaw Guardrails.

Web UI routes:
- / (landing page)
- /r6-dashboard (Health Data Dashboard — FHIR interactive showcase)
- /faq (Frequently Asked Questions)
- /wiki (Project Wiki)
- /skills (skill index — auto-generated from skills/*/SKILL.md)
- POST /api/subscribe (newsletter sign-up via Resend Audiences API + welcome email)
"""

import base64
import logging
import os
import re
from pathlib import Path

import httpx
import yaml
from email_validator import EmailNotValidError, validate_email
from flask import Response, jsonify, render_template, request
from main import app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Global request body cap — oversized payloads are rejected with 413 by
# Werkzeug before any handler runs. 16 MB headroom: full US Core R4 history
# Bundles ($ingest-context) and base64 PDF attachments in welcome/SHL flows
# can exceed 5 MB, so we use the larger cap to avoid breaking legitimate
# ingests while still stopping multi-hundred-MB body floods.
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Skill index — read once at import, cached for the process lifetime.
# ---------------------------------------------------------------------------
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _load_skills() -> list[dict]:
    skills_dir = Path(__file__).parent / "skills"
    if not skills_dir.is_dir():
        return []
    out: list[dict] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8")
            match = _FRONTMATTER_RE.search(text)
            if not match:
                continue
            meta = yaml.safe_load(match.group(1)) or {}
            out.append({
                "slug": skill_md.parent.name,
                "name": meta.get("name") or skill_md.parent.name,
                "description": (meta.get("description") or "").strip(),
                "version": meta.get("version"),
                "author": meta.get("author"),
                "license": meta.get("license"),
            })
        except Exception as exc:
            logger.warning("skills: failed to parse %s: %s", skill_md, exc)
    # Pin getting-started to the top — it's the entry-point.
    out.sort(key=lambda s: (s["slug"] != "getting-started", s["slug"]))
    return out


_SKILLS_CACHE: list[dict] = _load_skills()


# ---------------------------------------------------------------------------
# Security headers — applied to every response
# ---------------------------------------------------------------------------
# Content-Security-Policy. The dashboards (/r6-dashboard, /fhir-control-panel,
# /command-center) ship inline <style> and <script> blocks and fetch the
# tenant-scoped APIs same-origin, so 'unsafe-inline' is required for style/
# script and connect-src stays 'self'. img-src allows data: URIs (inline
# SVG/PNG badges). frame-ancestors 'none' mirrors X-Frame-Options: DENY.
#
# templates/base.html and templates/index.html load assets from external CDNs:
#   - Bootstrap CSS+JS  → cdn.jsdelivr.net
#   - FontAwesome CSS+fonts → cdnjs.cloudflare.com
#   - Google Fonts CSS  → fonts.googleapis.com (font files on fonts.gstatic.com)
# The previous "no external CDNs" policy silently broke styling/fonts on every
# deploy (flag-independent), so those hosts are explicitly allowed below.
#
# DEBT: script-src 'unsafe-inline' is a stopgap until inline <script> blocks are
# moved behind per-response nonces; tighten to 'self' + nonce when that lands.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
    "https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)


@app.after_request
def _security_headers(response):
    # setdefault throughout so we never clobber a header a handler already set.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), microphone=(), camera=(), usb=()",
    )
    response.headers.setdefault("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
    # HSTS is safe to emit always — browsers ignore it over plain HTTP, so it
    # never affects localhost dev but is present the moment we're behind TLS.
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


@app.route('/fhir-control-panel')
def fhir_control_panel():
    """
    FHIR Control Panel — live Dev Days demo surface.

    Public page shell (no auth). All data calls are made client-side and
    carry X-Tenant-Id, hitting the tenant-scoped read-only $inventory,
    $profile-adherence, and search endpoints under /r6/fhir.
    """
    return render_template('fhir_control_panel.html')


@app.route('/demo/intake-form')
def demo_intake_form():
    """
    A realistic (fictional) new-patient intake form for the form-fill demo.
    Blank form, no PHI — public. Pass this URL to the agent: it fetches the
    form, reads the field labels, and fills them from the patient's record.
    """
    return render_template('demo_intake_form.html')


# Valid tenant_id pattern: alphanumeric, hyphens, underscores, 1-64 chars
import re as _re
_TENANT_ID_PATTERN = _re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')


@app.route('/connect/<tenant_id>')
def fasten_connect(tenant_id):
    """
    Lean Fasten-Connect TEFCA verification page bound to a specific tenant.
    The Stitch widget renders only when FASTEN_PUBLIC_KEY is set; otherwise
    we show a "configure server first" banner so demos fail loudly.
    """
    if not _TENANT_ID_PATTERN.match(tenant_id):
        return 'Invalid tenant id', 400
    return render_template(
        'fasten_connect.html',
        tenant_id=tenant_id,
        fasten_public_key=os.environ.get('FASTEN_PUBLIC_KEY', '').strip(),
        tefca_mode=os.environ.get('FASTEN_TEFCA_MODE', 'true').strip().lower() == 'true',
    )


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


@app.route('/skills')
def skills_index():
    """Browseable index of every OpenClaw skill in this repo."""
    return render_template('skills.html', skills=_SKILLS_CACHE)


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
RESEND_EMAILS_URL = "https://api.resend.com/emails"
WELCOME_FROM = "HealthClaw <updates@healthclaw.io>"
WELCOME_SUBJECT = "Your HealthClaw quickstart is here 🩺"


def _welcome_html(pdf_url: str) -> str:
    return f"""\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
                   color:#0f172a; max-width:580px; margin:0 auto; padding:24px;">
  <div style="border-top:4px solid #22d3ee; padding-top:20px;">
    <h1 style="font-size:24px; margin:0 0 8px; color:#0f172a;">Welcome to HealthClaw 👋</h1>
    <p style="font-size:14px; color:#64748b; margin:0 0 24px;">
      Thanks for subscribing — here's the quickstart guide as promised.
    </p>
  </div>

  <div style="background:#f1f5f9; border-radius:10px; padding:18px 20px; margin-bottom:20px;">
    <strong style="display:block; font-size:15px; margin-bottom:6px;">📘 The 30-minute quickstart</strong>
    <p style="font-size:13px; color:#475569; margin:0 0 12px; line-height:1.55;">
      A 9-page walk from zero to a private, agent-mediated view of your own
      records — OpenClaw, a FHIR server, your EHR connection, and HealthClaw
      Guardrails, all running on your machine.
    </p>
    <a href="{pdf_url}"
       style="display:inline-block; background:#22d3ee; color:#001218;
              text-decoration:none; font-weight:700; font-size:13px;
              padding:10px 18px; border-radius:6px;">
      Download the PDF (attached too) →
    </a>
  </div>

  <p style="font-size:14px; line-height:1.6;">
    A few links to keep handy while you set up:
  </p>
  <ul style="font-size:14px; line-height:1.7; color:#0f172a;">
    <li><a href="https://github.com/aks129/HealthClawGuardrails" style="color:#0e9aaf;">GitHub repo</a> — the full codebase, MIT-licensed</li>
    <li><a href="https://healthclaw.io/skills" style="color:#0e9aaf;">Skill catalogue</a> — the eight OpenClaw skills</li>
    <li><a href="https://healthclaw.io/r6-dashboard" style="color:#0e9aaf;">Live dashboard</a> — try the guardrails against demo data</li>
  </ul>

  <p style="font-size:14px; line-height:1.6; margin-top:24px;">
    You'll hear from us when there's something worth your attention — a new MCP
    tool, a redaction rule, or a new upstream integration. Nothing else.
  </p>

  <hr style="border:0; border-top:1px solid #e2e8f0; margin:28px 0 16px;">
  <p style="font-size:11px; color:#94a3b8; line-height:1.5; margin:0;">
    HealthClaw is an open-source <a href="https://healthclaw.io" style="color:#94a3b8;">healthclaw.io</a>
    project. Records stay on your machine — this email is the only thing we ever send.
    Don't want it? <a href="{{{{RESEND_UNSUBSCRIBE_URL}}}}" style="color:#94a3b8;">Unsubscribe</a> in one click.
  </p>
</body></html>"""


def _welcome_text(pdf_url: str) -> str:
    return (
        "Welcome to HealthClaw\n"
        "=====================\n\n"
        "Thanks for subscribing — here's the quickstart guide as promised.\n\n"
        f"📘 Download the PDF: {pdf_url}\n"
        "(also attached to this email)\n\n"
        "Useful links while you're setting up:\n"
        "  • GitHub:  https://github.com/aks129/HealthClawGuardrails\n"
        "  • Skills:  https://healthclaw.io/skills\n"
        "  • Demo:    https://healthclaw.io/r6-dashboard\n\n"
        "You'll hear from us only when there's something worth your attention.\n"
        "Don't want it? Unsubscribe in one click — every email has the link.\n\n"
        "— HealthClaw  ·  healthclaw.io\n"
    )


def _send_welcome_email(email: str, api_key: str) -> None:
    """Fire-and-log welcome email. Failures are logged but never bubble up —
    the contact is already saved, that's the load-bearing part of /subscribe."""
    pdf_url = f"{request.url_root.rstrip('/')}/static/healthclaw-quickstart.pdf"
    pdf_path = Path(__file__).parent / "static" / "healthclaw-quickstart.pdf"
    payload: dict = {
        "from": WELCOME_FROM,
        "to": [email],
        "subject": WELCOME_SUBJECT,
        "html": _welcome_html(pdf_url),
        "text": _welcome_text(pdf_url),
        "tags": [{"name": "category", "value": "welcome"}],
    }
    if pdf_path.is_file():
        payload["attachments"] = [{
            "filename": "healthclaw-quickstart.pdf",
            "content": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
        }]
    try:
        resp = httpx.post(
            RESEND_EMAILS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=15.0,
        )
        if resp.status_code >= 400:
            logger.warning("welcome email failed: %s %s", resp.status_code, resp.text[:200])
    except httpx.HTTPError as exc:
        logger.warning("welcome email network error: %s", exc)


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
        _send_welcome_email(email, api_key)
        return jsonify({"ok": True, "email": email}), 200

    # Resend returns 422 with name=validation_error for duplicates — treat as success.
    if resp.status_code == 422:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if "already exist" in (body.get("message") or "").lower():
            return jsonify({"ok": True, "email": email, "already_subscribed": True}), 200

    logger.warning("subscribe: Resend returned %s: %s", resp.status_code, resp.text[:200])
    return jsonify({"error": "could not save subscription"}), 502
