"""The CareAgents half of the consent handoff (HealthClaw spec §13.3).

HealthClaw parks an authorization request and sends the browser here with a
signed handle; a person signs in, chooses which records to share, and proves
presence with a fresh passkey; this module builds the signed decision that
goes back. The key is derived from the mint secret both services already
hold, with domain separation, so nothing new is provisioned and a forged
grant needs the secret that already grants everything.

No PHI passes through here: a request id, a tenant pointer, a client name.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

#: What each requested scope means to the person, in their words.
SCOPE_WORDS = {
    "fhir.read": "Read your health records. Every read is redacted and "
                 "audited by HealthClaw.",
    "context.read": "Read the summaries HealthClaw builds from your records.",
}


def describe_scope(scope: str) -> str:
    return SCOPE_WORDS.get(scope, f"Use the permission named {scope!r}.")


def handoff_key(mint_secret: str) -> bytes:
    if not mint_secret:
        raise ValueError("HEALTHCLAW_MINT_SECRET is required for the consent handoff")
    return hashlib.sha256(b"healthclaw-consent-handoff:"
                          + mint_secret.encode("utf-8")).digest()


def tag(mint_secret: str, message: str) -> str:
    return hmac.new(handoff_key(mint_secret), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def parse_handle(req: str, mint_secret: str, now: float | None = None) -> str | None:
    """The request id inside a `<request_id>.<exp>.<tag>` handle whose tag
    verifies and whose expiry is in the future; None for anything else, and
    nothing about why. A forged link bounces here before anything is fetched."""
    if not isinstance(req, str) or req.count(".") != 2 or not mint_secret:
        return None
    request_id, exp_text, presented = req.split(".")
    if not request_id or not exp_text.isdigit() or not presented:
        return None
    if not hmac.compare_digest(presented.encode("ascii", "ignore"),
                               tag(mint_secret, f"{request_id}.{exp_text}").encode()):
        return None
    if int(exp_text) < (now if now is not None else time.time()):
        return None
    return request_id


def build_grant(mint_secret: str, request_id: str, decision: str,
                tenant_id: str | None = None, ttl_seconds: int = 300) -> tuple[str, str]:
    """The signed decision, `<base64url(JSON)>.<tag>`, and its consent id.

    Same bytes HealthClaw's `decode_grant` expects: sorted keys, no
    whitespace, a fresh nonce, a short expiry. `tenant_id` is required for an
    approval and absent from a denial.
    """
    if decision not in ("approved", "denied"):
        raise ValueError("decision must be approved or denied")
    if decision == "approved" and not tenant_id:
        raise ValueError("an approval names a tenant")
    consent_id = f"consent_{secrets.token_hex(12)}"
    payload = {
        "request_id": request_id,
        "tenant_id": tenant_id if decision == "approved" else None,
        "consent_id": consent_id,
        "nonce": secrets.token_hex(16),
        "exp": int(time.time()) + ttl_seconds,
        "decision": decision,
    }
    body = base64.urlsafe_b64encode(json.dumps(
        payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"{body}.{tag(mint_secret, body)}", consent_id
