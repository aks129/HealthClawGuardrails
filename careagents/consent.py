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
import re
import secrets
import time
import unicodedata

from markupsafe import Markup, escape

#: What each requested scope means to the person, in their words.
SCOPE_WORDS = {
    "fhir.read": "Read your health records. Every read is redacted and "
                 "audited by HealthClaw.",
    "context.read": "Read the summaries HealthClaw builds from your records.",
}


def describe_scope(scope: str) -> str:
    return SCOPE_WORDS.get(scope, f"Use the permission named {scope!r}.")


#: Hosted connectors whose callback host we know, matched exactly. Source:
#: spec §13.1 (docs/specs/2026-08-16-mcp-authorization.md), Claude's
#: registered callbacks. Add a host only with a documented callback for it.
RECOGNIZED_REDIRECT_HOSTS = frozenset({"claude.ai", "claude.com"})
#: The name inside the recognized hosts. A host that carries it without being
#: one of them (claude.ai.secure-login.example, claudeai-help.example) is told
#: apart in plain words.
_RECOGNIZED_NAME = "claude"


#: What a host may be made of once it is ASCII: DNS letters, digits, dots and
#: hyphens, or an IPv6 literal. Anything else (a space, a control character)
#: is no host a browser resolves, so the page names none.
_SHOWABLE_HOST = re.compile(r"[a-z0-9.-]+|[0-9a-f:]+")


def _ascii_host(host) -> str | None:
    """The host as a browser resolves it, lowercased, with one trailing dot
    removed (claude.ai. is claude.ai in DNS): an IDNA A-label for a non-ASCII
    host (which also maps U+3002, U+FF0E and U+FF61 to '.'), so a lookalike
    is never shown in Unicode. Never stripped: 'claude.ai\\x1f' is not
    claude.ai, and is no host at all. None when there is none to show."""
    if not isinstance(host, str) or not host:
        return None
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
    return host if _SHOWABLE_HOST.fullmatch(host) else None


#: Letters from other scripts that read as the Latin letters of the
#: recognized name. Not a full confusables table: enough to name the
#: lookalike on an A-label host; the unusual-letters line shows regardless.
_CONFUSABLE = str.maketrans({
    "а": "a", "α": "a", "с": "c", "ϲ": "c", "ԁ": "d", "е": "e", "ε": "e",
    "ӏ": "l", "ι": "l", "о": "o", "ο": "o", "р": "p", "ս": "u", "υ": "u",
    "х": "x", "у": "y",
})


def _unusual_letters(host: str | None) -> bool:
    """An A-label anywhere in the host: letters outside plain ASCII, which a
    person sees as machine code (xn--…) and which can imitate another host."""
    return bool(host) and any(label.startswith("xn--") for label in host.split("."))


def _as_read(host: str) -> str:
    """The host as a person would read it: A-labels decoded, lookalike
    letters folded to Latin. Only used to spot a borrowed name."""
    labels = []
    for label in host.split("."):
        if label.startswith("xn--"):
            try:
                label = label.encode("ascii").decode("idna")
            except UnicodeError:
                pass
        labels.append(unicodedata.normalize("NFKC", label).lower().translate(_CONFUSABLE))
    return ".".join(labels)


def _compact(text: str) -> str:
    return "".join(ch for ch in text if ch.isascii() and ch.isalnum())


def _lookalike(host: str | None) -> tuple[str | None, str | None]:
    """The recognized host an unrecognized one borrows the name of, and how,
    so the page only says what the person can see for themselves:
    "contains" when that address appears, letter for letter, in the host on
    screen; "letters" when it only appears once xn-- labels are decoded and
    lookalike letters folded; "looks" for the name without its dot
    (claudeai-help.example, one long label)."""
    if not host or host in RECOGNIZED_REDIRECT_HOSTS:
        return None, None
    compact = _compact(_as_read(host))
    if _RECOGNIZED_NAME not in compact:
        return None, None
    known = next((k for k in sorted(RECOGNIZED_REDIRECT_HOSTS)
                  if k.replace(".", "") in compact),
                 sorted(RECOGNIZED_REDIRECT_HOSTS)[0])
    if known in host:
        return known, "contains"
    if _RECOGNIZED_NAME in _compact(host):
        return known, "looks"
    return known, "letters"


def _wrap_at_dots(host: str | None) -> Markup | None:
    """The host, escaped, with a line-break hint after each dot: at 375px a
    long host wraps between its labels. It carries its own consent-host
    wrapper, whose CSS breaks a single label wider than the screen, so no
    occurrence of the host on the page can go without that fallback."""
    if host is None:
        return None
    labels = Markup(".<wbr>").join(escape(label) for label in host.split("."))
    return Markup('<span class="consent-host">') + labels + Markup("</span>")


def app_identity(parked: dict) -> dict:
    """How the consent page names the app asking. Registration is open, so
    `client_name` is only what the app calls itself; the redirect host is
    where the code goes, and is what the page leads with. No host (a
    HealthClaw that predates it) is never recognized."""
    host = _ascii_host(parked.get("redirect_host"))
    lookalike_of, lookalike_how = _lookalike(host)
    return {"client_name": parked.get("client_name") or "An agent",
            "redirect_host": host,
            "host_html": _wrap_at_dots(host),
            "host_recognized": host in RECOGNIZED_REDIRECT_HOSTS,
            "lookalike_of": lookalike_of,
            "lookalike_how": lookalike_how,
            "unusual_letters": _unusual_letters(host)}


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
