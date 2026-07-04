"""Inbound email relay — Resend receiving webhook -> forward to the maintainer.

support@ / privacy@ healthclaw.io are MX-routed to Resend; Resend POSTs an
`email.received` (svix-signed) webhook here. We verify the signature, fetch
the received message from the Resend API, and re-send it to FORWARD_TO with
the original sender as Reply-To.

Fail-closed: RESEND_INBOUND_WEBHOOK_SECRET unset -> 503; bad signature -> 403.
No PHI concerns beyond ordinary support mail; message bodies are relayed, not
stored, and nothing is logged beyond ids/status.
"""

import base64
import hashlib
import hmac
import json
import logging
import os

import requests
from flask import Blueprint, request

logger = logging.getLogger(__name__)

email_blueprint = Blueprint("email_inbound", __name__)

RESEND_API = "https://api.resend.com"
FORWARD_TO = os.environ.get("SUPPORT_FORWARD_TO", "eugene.vestel@gmail.com")
_SIG_TOLERANCE_SECONDS = 5 * 60


def _verify_svix(secret: str, headers, payload: bytes) -> bool:
    """Verify a svix-style webhook signature (v1, HMAC-SHA256)."""
    msg_id = headers.get("svix-id", "")
    timestamp = headers.get("svix-timestamp", "")
    signatures = headers.get("svix-signature", "")
    if not (msg_id and timestamp and signatures):
        return False
    try:
        import time
        if abs(time.time() - int(timestamp)) > _SIG_TOLERANCE_SECONDS:
            return False
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except Exception:
        return False
    signed = f"{msg_id}.{timestamp}.{payload.decode()}".encode()
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in signatures.split(" "):
        if "," in part and hmac.compare_digest(part.split(",", 1)[1], expected):
            return True
    return False


@email_blueprint.route("/email/inbound", methods=["POST"])
def inbound_email():
    secret = os.environ.get("RESEND_INBOUND_WEBHOOK_SECRET")
    if not secret:
        return {"error": "inbound relay not configured"}, 503

    payload = request.get_data()
    if not _verify_svix(secret, request.headers, payload):
        logger.warning("inbound email webhook: signature rejected")
        return {"error": "invalid signature"}, 403

    event = json.loads(payload)
    if event.get("type") != "email.received":
        return {"status": "ignored"}, 200

    data = event.get("data", {})
    email_id = data.get("email_id") or data.get("id")
    api_key = os.environ.get("RESEND_API_KEY", "")
    auth = {"Authorization": f"Bearer {api_key}"}

    # Pull the full message (webhook payload carries metadata only).
    body_text, body_html = "", None
    sender = data.get("from", "unknown")
    subject = data.get("subject", "(no subject)")
    if email_id:
        resp = requests.get(f"{RESEND_API}/emails/{email_id}", headers=auth, timeout=15)
        if resp.status_code == 200:
            msg = resp.json()
            sender = msg.get("from") or sender
            subject = msg.get("subject") or subject
            body_text = msg.get("text") or ""
            body_html = msg.get("html")

    to_addr = (data.get("to") or ["support@healthclaw.io"])[0]
    fwd = {
        "from": "HealthClaw Support <support@healthclaw.io>",
        "to": [FORWARD_TO],
        "reply_to": sender,
        "subject": f"[{to_addr}] {subject}",
        "text": f"From: {sender}\nTo: {to_addr}\n\n{body_text}",
    }
    if body_html:
        fwd["html"] = body_html
    resp = requests.post(f"{RESEND_API}/emails", headers=auth, json=fwd, timeout=15)
    logger.info("inbound email %s forwarded: %s", email_id, resp.status_code)
    return {"status": "forwarded", "code": resp.status_code}, 200
