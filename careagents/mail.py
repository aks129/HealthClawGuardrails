"""Transactional email via Resend — one-time codes only (no PHI, no marketing).

In development (no key) codes are logged to stderr instead of sent, so the
whole auth flow is exercisable locally without a provider.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger("careagents.mail")


def send_code(cfg, email: str, code: str, purpose: str) -> bool:
    verb = "Verify your email" if purpose == "verify" else "Your sign-in code"
    if not cfg.resend_api_key:
        logger.warning("DEV email — %s for %s: %s", verb, email, code)
        return True
    html = (
        f"<div style='font-family:system-ui,sans-serif;max-width:420px'>"
        f"<h2 style='color:#22190E'>CareAgents</h2>"
        f"<p>{verb}. Enter this code — it expires in 10 minutes:</p>"
        f"<p style='font-size:30px;font-weight:700;letter-spacing:.18em;"
        f"color:#C2532E'>{code}</p>"
        f"<p style='color:#5E5240;font-size:13px'>If you didn't request this, "
        f"you can ignore it.</p></div>")
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {cfg.resend_api_key}"},
            json={"from": cfg.resend_from, "to": [email],
                  "subject": f"{verb} — CareAgents", "html": html},
            timeout=15)
    except requests.RequestException as exc:
        logger.error("resend send failed: %s", type(exc).__name__)
        return False
    if r.status_code not in (200, 201):
        logger.error("resend send http %s", r.status_code)
        return False
    return True
