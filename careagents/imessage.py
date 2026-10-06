"""The text-message surface, independent of how a text arrives.

`handle_inbound` takes a sender handle and a message body and answers
`(body, http_status)` where body is `{reply?: str, run_id?: str}`: a
sentence to send back now, a durable run whose reply follows, or both
(the welcome, then the answer). The Mac-mini relay endpoints in app.py are
one thin adapter over it; a hosted iMessage provider's webhook is another.

What lives here: handle normalization, the STOP / HELP / START keywords,
the `care <code>` pairing line, the sign-in link for a sender we don't know
yet, and every sentence the texter reads. Storage stays in accounts.py.

Nothing here logs a message body or a full handle. Use `mask()`.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Callable

#: A texted sign-in link works once, for this long.
LINK_TTL_SECONDS = 30 * 60
#: A pairing code from settings works for this long.
CODE_TTL_SECONDS = 30 * 60
#: Wrong pairing codes a sender may try per window before we stop checking.
BIND_ATTEMPTS = 5
#: Sign-in links one sender can be sent per window. Past it, no reply.
LINKS_PER_WINDOW = 3
WINDOW_SECONDS = 30 * 60

CONTACT = "contactus@healthclaw.io"

# --- what the texter reads ------------------------------------------------
# Plain and short: the reader is on a phone and may never have seen the app.

WELCOME_TEXT = (
    "You're connected to CareAgents. Text me questions about your health "
    "records. I can explain lab results and help you write questions for "
    "your next visit. If something needs your OK, I'll send a link so you "
    "can approve it. Text STOP any time to stop.")

HELP_TEXT = ("CareAgents answers questions about your health records. Text "
             f"STOP to stop. Need a person? Write to {CONTACT}.")

STOP_TEXT = ("Done. CareAgents won't text you anymore. Text START if you "
             "want to come back.")

START_BOUND_TEXT = ("You're connected. Ask me anything about your health "
                    "records.")

TAKEN_TEXT = ("This chat is already connected to another CareAgents "
              "account. Text STOP first, then try again.")

CODE_FAILED_TEXT = ("That code didn't work or has expired. Get a new one in "
                    "CareAgents settings and try again.")

CODE_LOCKED_TEXT = "Too many tries. Please wait 30 minutes and try again."

BUSY_TEXT = "One moment — too many messages just now. Try again in a bit."

UNAVAILABLE_TEXT = ("Sorry, I can't answer right now. Please try again in a "
                    "few minutes.")

TOO_LONG_TEXT = "That message is too long. Please send a shorter one."


def link_text(url: str) -> str:
    return ("Hi, this is CareAgents. To get started, tap this link to sign "
            f"in or make an account: {url}\n"
            "The link works once, for 30 minutes. Text HELP for help or "
            "STOP to stop.")


def no_agent_text(origin: str) -> str:
    return ("You're signed in, but your assistant isn't set up yet. Open "
            f"{origin}/home to start, then text me again.")


# --- parsing --------------------------------------------------------------

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# `care <code>` or `care_<code>`, the whole message. Codes are lowercase
# base32 (accounts.new_binding_code); the match is case-insensitive.
_CODE_LINE = re.compile(r"^care[ _]+([a-z2-7]{6,16})$", re.IGNORECASE)


def normalize_handle(raw: object) -> str | None:
    """A phone number as E.164 (10 digits read as US, +1), an Apple ID email
    lowercased, or None for anything else (a short code, garbage)."""
    s = str(raw or "").strip()
    if not s:
        return None
    if s.lower().startswith(("mailto:", "tel:")):
        s = s.split(":", 1)[1].strip()
    if "@" in s:
        s = s.lower()
        return s if _EMAIL.match(s) and len(s) <= 120 else None
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if re.search(r"[^\d\s().+\-]", s):
        return None
    if not plus:
        if len(digits) == 10:
            digits = "1" + digits
        elif not (len(digits) == 11 and digits.startswith("1")):
            return None
    e164 = "+" + digits
    return e164 if _E164.match(e164) else None


def handle_key(handle: str) -> str:
    return hashlib.sha256(handle.encode()).hexdigest()


def mask(handle: object) -> str:
    """Enough of a handle to tell two apart in a log, no more."""
    s = str(handle or "")
    if "@" in s:
        name, _, domain = s.partition("@")
        return f"{name[:1]}***@{domain}"
    return f"***{s[-2:]}" if len(s) > 2 else "***"


def keyword(text: str) -> str | None:
    """STOP, HELP or START when that is the whole message, else None.
    Case-insensitive; trailing punctuation is ignored."""
    word = text.strip().rstrip(".!").strip().lower()
    return word if word in ("stop", "help", "start") else None


def pairing_code(text: str) -> str | None:
    m = _CODE_LINE.match(text.strip())
    return m.group(1).lower() if m else None


def new_link_token() -> str:
    return secrets.token_urlsafe(24)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- the transport-agnostic core -----------------------------------------

@dataclass
class Deps:
    """What the core needs from the app. Built once in create_app."""
    origin: str
    svc: object                      # accounts.AccountService
    workers_ready: Callable[[], bool]
    allow_turn: Callable[[str], bool]
    #: (account_id, ctx) -> a sentence the turn answers before queueing,
    #: or None. Holds the admission daily cap, as /api/chat does.
    admission_block: Callable[[str, dict], str | None]
    #: (ctx, text, request_id, conversation_id) -> run dict, or raises.
    queue_turn: Callable[[dict, str, str, str | None], dict]
    queue_error: type = Exception


def bind_by_code(deps: Deps, handle: str, code: str) -> tuple[dict, int]:
    """Pair `handle` with the pending surface behind `code`."""
    svc = deps.svc
    if svc.imessage_bind_locked(handle):
        return {"error": "too many attempts", "reply": CODE_LOCKED_TEXT}, 429
    surface = svc.find_surface_by_code(code, kind="imessage")
    if not surface:
        svc.imessage_note_bind_failure(handle)
        return {"error": "unknown code", "reply": CODE_FAILED_TEXT}, 404
    outcome = svc.bind_imessage_handle(
        surface["account_id"], handle, pending_surface_id=surface["id"])
    if outcome == "taken":
        return {"error": "handle taken", "reply": TAKEN_TEXT}, 409
    return {"ok": True, "reply": WELCOME_TEXT}, 200


def handle_inbound(deps: Deps, raw_handle: str, text: str,
                   request_id: str | None = None,
                   conversation_id: str | None = None) -> tuple[dict, int]:
    """One inbound text, from any transport. See the module docstring."""
    svc = deps.svc
    raw = str(raw_handle or "").strip()
    if not raw:
        return {"error": "missing handle"}, 400
    handle = normalize_handle(raw)
    text = (text or "").strip()
    word = keyword(text)
    surface = svc.find_surface_by_handle(handle or raw, kind="imessage",
                                         also=raw)

    if word == "help":
        return {"reply": HELP_TEXT}, 200
    if word == "stop":
        # Unbind and remember the choice. Said once: a second STOP from a
        # handle already opted out gets nothing.
        first = svc.imessage_stop(handle or raw)
        return ({"reply": STOP_TEXT} if first or surface else {}), 200
    code = pairing_code(text)
    if code:
        if not handle:
            return {"error": "unsupported handle"}, 400
        return bind_by_code(deps, handle, code)

    if surface is None:
        if not handle:
            # A short code or something we cannot text back: stay silent.
            return {"error": "unbound handle"}, 404
        if svc.imessage_opted_out(handle):
            if word != "start":
                return {}, 200
            svc.imessage_opt_in(handle)
        token = svc.issue_imessage_link(handle)
        if token is None:            # past the per-window link allowance
            return {}, 200
        return {"reply": link_text(f"{deps.origin}/link/{token}")}, 200

    if word == "start":
        return {"reply": START_BOUND_TEXT}, 200

    ctx = svc.imessage_agent_context(surface)
    if not ctx:
        return {"reply": no_agent_text(deps.origin)}, 200
    if not text:
        return {"error": "empty message"}, 400
    if len(text) > 2000:
        return {"error": "message must be 1-2000 characters",
                "reply": TOO_LONG_TEXT}, 400
    if not deps.workers_ready():
        return {"error": "run_workers_unavailable",
                "reply": UNAVAILABLE_TEXT}, 503
    if not deps.allow_turn(surface["account_id"]):
        return {"reply": BUSY_TEXT}, 200
    blocked = deps.admission_block(surface["account_id"], ctx)
    if blocked:
        return {"reply": blocked}, 200
    try:
        run = deps.queue_turn(ctx, text, request_id, conversation_id)
    except deps.queue_error:
        return {"error": "run queue unavailable",
                "reply": UNAVAILABLE_TEXT}, 503
    body = {"run_id": run["id"], "status": run.get("status"),
            "duplicate": bool(run.get("duplicate"))}
    if svc.take_imessage_welcome(surface["id"]):
        body["reply"] = WELCOME_TEXT
    return body, 202
