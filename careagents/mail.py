"""Transactional email via Resend — one-time codes only (no PHI, no marketing).

In development (no key) codes are logged to stderr instead of sent, so the
whole auth flow is exercisable locally without a provider.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger("careagents.mail")

# Three outcomes, not two. A provider call can also end without telling us
# anything — the request went out and the answer never came back — and that is
# NOT the same as a refusal. Collapsing it into "failed" makes the caller state
# something it did not observe, which is the whole shape of #220.
#
# All three are truthy strings on purpose: `if send_code(...)` cannot be
# written by accident the way `if not send_code(...)` could, and a stale fake
# that returns True/None/a code string lands in UNCONFIRMED rather than
# silently reading as success.
SENT = "sent"
NOT_SENT = "not_sent"
UNCONFIRMED = "unconfirmed"


def send_code(cfg, email: str, code: str, purpose: str) -> str:
    """Send a one-time code. Returns SENT, NOT_SENT, or UNCONFIRMED.

    The rule: a response is the only evidence of an outcome. The provider
    answering 2xx is a send; the provider answering anything else is a refusal
    (Resend returns a message id on acceptance, so a non-2xx did not queue);
    never reaching the provider is a refusal. A request that went out and was
    never answered — a read timeout — is UNCONFIRMED, because the mail may
    already be in the person's inbox.
    """
    verb = "Verify your email" if purpose == "verify" else "Your sign-in code"
    if not cfg.resend_api_key:
        logger.warning("DEV email — %s for %s: %s", verb, email, code)
        return SENT
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
    except requests.ConnectionError as exc:
        # DNS failure, refused connection, TLS failure — the request never
        # reached Resend, so nothing was sent.
        logger.error("resend send failed: %s", type(exc).__name__)
        return NOT_SENT
    except requests.RequestException as exc:
        # Chiefly a read timeout: the POST was written and the answer was
        # lost. Resend may well have accepted and delivered it.
        logger.error("resend send unconfirmed: %s", type(exc).__name__)
        return UNCONFIRMED
    if r.status_code not in (200, 201):
        logger.error("resend send http %s", r.status_code)
        return NOT_SENT
    return SENT
