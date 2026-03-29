"""
Standard-Webhooks signature verification for Fasten Connect events.

Spec: https://www.standardwebhooks.com/
Algorithm:
  signed_content = f"{webhook-id}.{webhook-timestamp}.{raw_body}"
  signature      = base64( HMAC-SHA256( base64_decode(secret), signed_content ) )

Secret format: "whsec_<base64-encoded-bytes>" (from Fasten Developer Portal).
If FASTEN_WEBHOOK_SECRET is not set, verification is skipped (dev/test mode).
"""
import base64
import hashlib
import hmac
import logging
import os
import time

logger = logging.getLogger(__name__)

_REPLAY_TOLERANCE_SECONDS = 300  # reject webhooks older than 5 minutes


def verify_webhook(headers: dict, raw_body: bytes) -> bool:
    """
    Verify a Standard-Webhooks HMAC signature.

    Returns True if the signature is valid (or if FASTEN_WEBHOOK_SECRET is
    not configured — allows dev/test without a real secret).
    Returns False if the signature is present but invalid or the timestamp
    is outside the replay window.
    """
    secret = os.environ.get('FASTEN_WEBHOOK_SECRET', '').strip()
    if not secret:
        logger.warning(
            'FASTEN_WEBHOOK_SECRET not set — skipping webhook verification (dev mode)'
        )
        return True

    msg_id = headers.get('webhook-id') or headers.get('Webhook-Id', '')
    msg_timestamp = headers.get('webhook-timestamp') or headers.get('Webhook-Timestamp', '')
    msg_signature = headers.get('webhook-signature') or headers.get('Webhook-Signature', '')

    if not all([msg_id, msg_timestamp, msg_signature]):
        logger.warning('Fasten webhook: missing Standard-Webhooks headers')
        return False

    # Replay attack protection: reject if timestamp is outside the tolerance window
    try:
        ts = int(msg_timestamp)
        if abs(time.time() - ts) > _REPLAY_TOLERANCE_SECONDS:
            logger.warning('Fasten webhook: timestamp outside replay window')
            return False
    except ValueError:
        logger.warning('Fasten webhook: invalid timestamp')
        return False

    # Decode the signing secret (strip optional "whsec_" prefix, then base64-decode)
    try:
        raw_secret = base64.b64decode(secret.removeprefix('whsec_'))
    except Exception:
        logger.error('Fasten webhook: could not decode FASTEN_WEBHOOK_SECRET')
        return False

    # Build signed content string
    try:
        body_str = raw_body.decode('utf-8')
    except UnicodeDecodeError:
        logger.warning('Fasten webhook: body is not valid UTF-8')
        return False

    signed_content = f'{msg_id}.{msg_timestamp}.{body_str}'.encode('utf-8')

    # Compute expected signature
    computed = base64.b64encode(
        hmac.new(raw_secret, signed_content, hashlib.sha256).digest()
    ).decode('utf-8')

    # The header may contain multiple space-separated signatures (e.g. "v1,abc v1,def")
    for sig_entry in msg_signature.split(' '):
        parts = sig_entry.split(',', 1)
        if len(parts) == 2 and parts[0] == 'v1':
            if hmac.compare_digest(parts[1], computed):
                return True

    logger.warning('Fasten webhook: signature mismatch')
    return False
