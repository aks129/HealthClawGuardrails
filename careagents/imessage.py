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
import math
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, unquote

from careagents.agent import GENERIC_FAILURE_TEXT

#: A texted sign-in link works once, for this long.
LINK_TTL_SECONDS = 30 * 60
#: A pairing code from settings works for this long.
CODE_TTL_SECONDS = 30 * 60
#: Wrong pairing codes a sender may try per window before we stop checking.
BIND_ATTEMPTS = 5
#: Sign-in links one sender can be sent per window. Past it, no reply.
LINKS_PER_WINDOW = 3
WINDOW_SECONDS = 30 * 60
#: A bound handle silent this long re-confirms before its next answer,
#: even inside the re-verify window (#871): a reassigned number goes quiet
#: before its new holder texts.
REVERIFY_SILENCE_SECONDS = 30 * 86400

CONTACT = "contactus@healthclaw.io"

# --- what the texter reads ------------------------------------------------
# Plain and short: the reader is on a phone and may never have seen the app.

#: Things to ask, for HELP and the welcome. Each one reaches a feature: the
#: health summary, labs, care gaps, the lab trend, the visit brief and the
#: intake form.
MENU = ("What medications am I on?",
        "What do my labs say?",
        "Any screenings due?",
        "Has my cholesterol changed?",
        "Get me ready for my visit",
        "Fill out my intake form")
MENU_TEXT = ("\n".join(f"{i}. {q}" for i, q in enumerate(MENU, start=1))
             + "\nJust type your question, or reply with a number.")

# A whole-message 1-6, optionally followed by "." or ")".
_MENU_NUMBER = re.compile(r"^([1-9])[.)]?$")


def menu_question(text: str) -> str | None:
    """The menu question a bare number stands for, or None."""
    m = _MENU_NUMBER.match(text.strip())
    if not m or not 1 <= int(m.group(1)) <= len(MENU):
        return None
    return MENU[int(m.group(1)) - 1]

WELCOME_TEXT = (
    "You're connected to CareAgents. Text me questions about your health "
    "records, for example:\n" + MENU_TEXT + "\n"
    "I can explain lab results and help with your next visit. If something "
    "needs your OK, I'll send a link so you can approve it. Text APPROVALS "
    "to see what's waiting, or STOP any time to stop.")

# A text that names the help address names both brands, so the address
# does not read as a stranger's.
#
# HELP and START read the same for every handle. A phone number is
# recycled: its new holder must not learn from our words that the last one
# used CareAgents, connected or stopped.
HELP_TEXT = (
    "CareAgents (by HealthClaw) answers questions about your health "
    "records. Try:\n" + MENU_TEXT + "\n"
    "Text START for a sign-in link, APPROVALS for requests waiting on you, "
    f"or STOP to stop. Need a person? Write to {CONTACT}.")

STOP_TEXT = ("Done. CareAgents won't text you anymore. Text START if you "
             "want to come back.")

START_BOUND_TEXT = ("You're connected. Ask me anything about your health "
                    "records.")

TAKEN_TEXT = ("This chat is already connected to another CareAgents "
              "account. Text STOP first, then try again.")

CODE_FAILED_TEXT = ("That code didn't work or has expired. Get a new one in "
                    "CareAgents settings and try again.")

CODE_LOCKED_TEXT = "Too many tries. Please wait 30 minutes and try again."

def busy_text(window_seconds: float) -> str:
    """The burst limit, in the limiter's own window."""
    minutes = max(1, math.ceil(window_seconds / 60))
    wait = "a minute" if minutes == 1 else f"about {minutes} minutes"
    return (f"You've sent a lot of messages quickly. Please wait {wait}, "
            "then try again.")

UNAVAILABLE_TEXT = ("Sorry, I can't answer right now. Please try again in a "
                    "few minutes.")

START_CAPPED_TEXT = ("I've sent several links in the last half hour. "
                     "Please wait 30 minutes, then text START again.")

TOO_LONG_TEXT = "That message is too long. Please send a shorter one."

# The approvals page's own sentence (app.py, #215): a count we could not
# get is never said as zero.
APPROVALS_UNCHECKABLE_TEXT = (
    "I couldn't check for requests right now. Nothing has been approved or "
    "declined. Please try again in a moment.")


def _host(origin: str) -> str:
    return origin.split("://", 1)[-1].rstrip("/") or origin


def approvals_text(count: int, origin: str, agent_id: str) -> str:
    link = f"{origin}/agents/{agent_id}/approvals"
    if count == 0:
        return f"Nothing is waiting for your OK. Your requests: {link}"
    noun = "request" if count == 1 else "requests"
    return f"You have {count} {noun} waiting for your OK: {link}"


def connect_text(origin: str) -> str:
    """CONNECT points to the web: records are connected there, after the
    consent card, never by text."""
    return ("Connecting your own records is open to invited testers for now. "
            f"You can see where it will be at {origin}/home under Add "
            "records. Texting works with sample records.")


def link_text(url: str) -> str:
    return ("Hi, this is CareAgents. To get started, tap this link to sign "
            f"in or make an account: {url}\n"
            "The link works once, for 30 minutes. Text HELP for help or "
            "STOP to stop.")


def start_text(url: str) -> str:
    """START from a handle that is not connected: the same for everyone."""
    return (f"Tap this link to sign in to CareAgents: {url}\n"
            "The link works once, for 30 minutes.")


def connected_notice(handle: str) -> str:
    """The one line emailed to the account owner when a phone connects."""
    if "@" in handle:
        what = f"An Apple ID ({mask(handle)})"
    else:
        what = f"A phone ending in {handle[-4:]}"
    return (f"{what} was connected to your CareAgents account. If this "
            "wasn't you, open Settings and disconnect it.")


def display_handle(handle: str | None) -> str:
    """A US number as (555) 010-0177 on a page; anything else as stored."""
    s = str(handle or "")
    if re.fullmatch(r"\+1\d{10}", s):
        return f"({s[2:5]}) {s[5:8]}-{s[8:]}"
    return s


def masked_display(handle: str | None) -> str:
    """For Settings: enough to recognise, not the whole handle."""
    s = str(handle or "")
    if "@" in s:
        return f"Apple ID {mask(s)}"
    return f"phone ending in {s[-4:]}" if len(s) >= 4 else "a phone"


def reverify_notice(handle: str) -> str:
    """The one line emailed to the owner when a handle must re-confirm."""
    if "@" in handle:
        what = f"the Apple ID {mask(handle)}"
    else:
        what = f"the phone ending in {handle[-4:]}"
    return (f"We asked {what} to confirm it's still yours before "
            "answering texts.")


def no_agent_text(origin: str) -> str:
    return ("You're signed in, but your assistant isn't set up yet. Open "
            f"{origin}/home to start, then text me again.")


# --- parsing --------------------------------------------------------------

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def _is_email(s: str) -> bool:
    """One @, no whitespace, a dot inside the domain. A plain check rather
    than a regex: the overlapping classes in the old pattern could backtrack
    polynomially on a crafted handle (CodeQL py/polynomial-redos)."""
    local, at, domain = s.partition("@")
    if not at or not local or "@" in domain or any(c.isspace() for c in s):
        return False
    name, dot, tld = domain.rpartition(".")
    return bool(dot and name and tld)
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
        return s if len(s) <= 120 and _is_email(s) else None
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


#: The carriers' opt-out words (CTIA), all read as STOP. Only ever as the
#: whole message: "cancel my appointment" is a question for the assistant.
STOP_WORDS = frozenset({"stop", "stopall", "unsubscribe", "cancel", "end",
                        "quit"})


def keyword(text: str) -> str | None:
    """"stop", "help", "start", "approvals" or "connect" when the whole
    message is that word (or a STOP synonym), else None. Case-insensitive;
    trailing . or ! ignored."""
    word = text.strip().rstrip(".!").strip().lower()
    if word in STOP_WORDS:
        return "stop"
    return word if word in ("help", "start", "approvals", "connect") else None


def pairing_code(text: str) -> str | None:
    m = _CODE_LINE.match(text.strip())
    return m.group(1).lower() if m else None


def new_link_token() -> str:
    return secrets.token_urlsafe(24)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- a finished run, as one text --------------------------------------------

#: Run statuses after which nothing more will be said.
FINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "waiting_for_human"})

def timeout_text(origin: str) -> str:
    return f"That took too long. Please try again or open {_host(origin)}."


# Markdown the model may still write on a text surface. Only at a line's
# start for headings and bullets, and only paired single asterisks around
# words: a URL's `#fragment`, an id's `_` and `2*3` must survive.
_MD_HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+")
_MD_BULLET = re.compile(r"(?m)^([ \t]*)\*[ \t]+")
_MD_EMPHASIS = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")


# A URL to an engine document, or any URL carrying a signature: a bearer
# link. The tool never hands one to the model on a text surface; this
# catches one the model still writes (#875 QA).
#
# Each run of non-space characters is tested on its percent-decoded form
# (twice, for a double encoding), because the server decodes `%73ig=` and
# `%64ocuments` and the link still works (R875-2).
_TOKEN = re.compile(r"[^\s<>\"']+")
_SIGNED_MARKER = re.compile(r"/r6/sdc/documents/|[?&]sig=", re.IGNORECASE)


def _is_signed_url(token: str) -> bool:
    return bool(_SIGNED_MARKER.search(unquote(unquote(token))))


def strip_signed_urls(text: str) -> str:
    stripped = _TOKEN.sub(
        lambda m: "" if _is_signed_url(m.group(0)) else m.group(0), text)
    if stripped == text:
        return text
    # Tidy the gap the URL left; text without one is left exactly as is.
    stripped = re.sub(r"[ \t]+([.,;:!?])", r"\1", stripped)
    return re.sub(r"[ \t]{2,}", " ", stripped).strip()


def plain_text(text: str) -> str:
    """Markdown stripped, as a backstop to the texting style prompt."""
    text = text.replace("**", "")
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub(r"\1- ", text)
    return _MD_EMPHASIS.sub(r"\1", text)


#: One text's length, and how many one answer may take. Past that, the
#: rest is on the web.
TEXT_PART_LIMIT = 1000
MAX_TEXT_PARTS = 4


def _break_long(paragraph: str, limit: int) -> list[str]:
    """A paragraph longer than `limit`, cut between words (or hard, for a
    run with no spaces)."""
    pieces = []
    while len(paragraph) > limit:
        cut = paragraph.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        pieces.append(paragraph[:cut].rstrip())
        paragraph = paragraph[cut:].lstrip()
    if paragraph:
        pieces.append(paragraph)
    return pieces


def split_reply(text: str, origin: str, agent_id: str = "") -> list[str]:
    """A reply as texts of at most TEXT_PART_LIMIT characters, cut at
    paragraph breaks where it can, in order. At most MAX_TEXT_PARTS; a
    longer answer ends by pointing to the web chat, where the whole answer
    is."""
    chunks: list[str] = []
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if not paragraph:
            continue
        for piece in _break_long(paragraph, TEXT_PART_LIMIT):
            if chunks and len(chunks[-1]) + 2 + len(piece) <= TEXT_PART_LIMIT:
                chunks[-1] += "\n\n" + piece
            else:
                chunks.append(piece)
    if len(chunks) <= MAX_TEXT_PARTS:
        return chunks or [text]
    tail = (f"The rest is in your chat: {origin}/chat?agent={agent_id}"
            if agent_id else f"The rest is in your chat at {origin}")
    last = _break_long(chunks[MAX_TEXT_PARTS - 1],
                       TEXT_PART_LIMIT - len(tail) - 2)[0]
    return [*chunks[:MAX_TEXT_PARTS - 1], f"{last}\n\n{tail}"]


def run_reply(events: list[dict], origin: str, agent_id: str) -> str:
    """A finished run's events as the text sent back: the agent's words,
    then a link for each card a phone cannot show (a form to review, a
    signed document, the lab chart, the visit brief). Both the relay's runs
    endpoint and the Sendblue deliverer send this."""
    parts: list[str] = []
    extras: list[str] = []
    for event in events or []:
        kind = event.get("type")
        payload = event.get("payload") or {}
        if kind == "agent.text" and payload.get("text"):
            parts.append(plain_text(strip_signed_urls(payload["text"])))
        elif kind == "agent.card" and payload.get("type") == "card":
            card = payload.get("kind")
            if card == "lab-timeline":
                topic = str(payload.get("topic") or "")[:64]
                link = (f"See the chart: {origin}/chat?agent={agent_id}"
                        f"&chart={quote(topic, safe='')}")
                if link not in extras:
                    extras.append(link)
            elif card == "brief":
                link = f"Your full visit brief: {origin}/brief?agent={agent_id}"
                if link not in extras:
                    extras.append(link)
            elif card == "review":
                extras.append(
                    "I've prepared a form for your review — approve each "
                    f"item here: {origin}/review/{agent_id}/"
                    f"{payload.get('action_id', '')}")
            elif card == "pdf":
                # The review page has the PDF button. The engine's signed
                # URL is a bearer link to the document: never texted.
                action_id = str(payload.get("action_id") or "")
                extras.append(
                    f"Your intake form is ready: {origin}/review/{agent_id}/"
                    f"{quote(action_id, safe='')}" if action_id else
                    "Your intake form is ready in your CareAgents chat.")
        elif kind == "agent.error":
            parts.append(payload.get("text") or GENERIC_FAILURE_TEXT)
    return "\n\n".join([*parts, *extras]).strip() or (
        GENERIC_FAILURE_TEXT + " Please try again.")


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
    #: The burst limiter's window, for the sentence that names it.
    burst_window_seconds: float = 600
    #: (account_id, handle) after a handle is newly bound. Tells the owner.
    on_connected: Callable[[str, str], None] | None = None
    #: ctx -> how many requests wait for this person's OK. Raises
    #: `queue_error` when that could not be found out; never 0 for it.
    pending_count: Callable[[dict], int] | None = None
    #: How long a binding holds before its owner re-confirms it (#871).
    reverify_seconds: float = 60 * 86400
    #: (account_id, handle) the first time a handle must re-confirm.
    #: Tells the owner.
    on_reverify: Callable[[str, str], None] | None = None


def link_url(origin: str, token: str) -> str:
    """The token rides in the query string: the access log records the
    path only (deploy/careagents/Dockerfile), so it never sees the token."""
    return f"{origin}/link?t={token}"


def notify_connected(deps: Deps, account_id: str, handle: str) -> None:
    if deps.on_connected is not None:
        deps.on_connected(account_id, handle)


def bind_by_code(deps: Deps, handle: str, code: str,
                 raw: str | None = None) -> tuple[dict, int]:
    """Pair `handle` with the pending surface behind `code`. `raw` is the
    handle as it arrived, so a row bound under it before normalization
    still counts as bound."""
    svc = deps.svc
    if svc.imessage_bind_locked(handle):
        return {"error": "too many attempts", "reply": CODE_LOCKED_TEXT}, 429
    surface = svc.find_surface_by_code(code, kind="imessage")
    if not surface:
        svc.imessage_note_bind_failure(handle)
        return {"error": "unknown code", "reply": CODE_FAILED_TEXT}, 404
    outcome = svc.bind_imessage_handle(
        surface["account_id"], handle, pending_surface_id=surface["id"],
        also=raw)
    if outcome == "taken":
        return {"error": "handle taken", "reply": TAKEN_TEXT}, 409
    notify_connected(deps, surface["account_id"], handle)
    return {"ok": True, "reply": WELCOME_TEXT}, 200


def reverify_due(surface: dict, now: float, window_seconds: float) -> bool:
    """True when a bound handle must re-confirm before it is answered:
    already asked, verified longer ago than the window, or silent for
    REVERIFY_SILENCE_SECONDS. A binding with no verified time counts from
    when it was bound, as the migration backfills; with neither, it is due
    (fail closed). One with no recorded text counts from its verification.
    """
    if surface.get("reverify_notified_at"):
        return True
    verified = surface.get("verified_at") or surface.get("bound_at")
    if not verified:
        return True
    last = max(verified, surface.get("last_inbound_at") or 0)
    return (now - verified >= window_seconds
            or now - last >= REVERIFY_SILENCE_SECONDS)


def ask_to_reverify(deps: Deps, surface: dict, word: str | None
                    ) -> tuple[dict, int]:
    """No run: a sign-in link, under the same allowance as any other. On
    confirm by the same account, accounts.bind_imessage_handle refreshes
    the binding; another account meets the usual "taken".

    The words are START's to a number we have never seen: the reader may
    be the number's new holder, who must not learn it was connected
    (#866). The owner's email is where this is explained."""
    svc = deps.svc
    handle = surface["handle"]
    if svc.imessage_mark_reverify(surface["id"]) and deps.on_reverify:
        deps.on_reverify(surface["account_id"], handle)
    token = svc.issue_imessage_link(handle)
    if token is None:            # past the per-window link allowance
        return ({"reply": START_CAPPED_TEXT} if word == "start" else {}), 200
    return {"reply": start_text(link_url(deps.origin, token))}, 200


def handle_inbound(deps: Deps, raw_handle: str, text: str,
                   request_id: str | None = None,
                   conversation_id: str | None = None,
                   transport_block: Callable[[dict], str | None] | None = None,
                   ) -> tuple[dict, int]:
    """One inbound text, from any transport. See the module docstring.

    `transport_block(ctx)` lets a transport refuse a turn for its own
    reasons: a sentence answered instead, and no run is queued."""
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
        # Unbind (either spelling of the handle), void its unused links,
        # and remember the choice. Every STOP is confirmed (CTIA: one
        # confirmation per STOP), a repeat included, so the reply never
        # hints whether this number stopped texts before.
        svc.imessage_stop(handle or raw, also=raw)
        return {"reply": STOP_TEXT}, 200
    code = pairing_code(text)
    if code:
        if not handle:
            return {"error": "unsupported handle"}, 400
        return bind_by_code(deps, handle, code, raw=raw)

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
            # START is always answered; anything else stays quiet.
            return ({"reply": START_CAPPED_TEXT} if word == "start"
                    else {}), 200
        url = link_url(deps.origin, token)
        return {"reply": start_text(url) if word == "start"
                else link_text(url)}, 200

    # Before anything that speaks for the account: START would say it is
    # connected, APPROVALS would give its count. STOP and HELP stay above.
    if reverify_due(surface, time.time(), deps.reverify_seconds):
        return ask_to_reverify(deps, surface, word)
    # Only once let through, so a stranger's texts never reset the clock.
    svc.imessage_note_inbound(surface["id"])

    if word == "start":
        return {"reply": START_BOUND_TEXT}, 200

    if word == "connect":
        return {"reply": connect_text(deps.origin)}, 200

    ctx = svc.imessage_agent_context(surface)
    if not ctx:
        return {"reply": no_agent_text(deps.origin)}, 200
    if word == "approvals" and deps.pending_count is not None:
        # A count and a link, no record content, so it comes before the
        # transport's own refusal: the approvals page is where a person on
        # real records has to go anyway.
        try:
            count = deps.pending_count(ctx)
        except deps.queue_error:
            return {"reply": APPROVALS_UNCHECKABLE_TEXT}, 200
        return {"reply": approvals_text(count, deps.origin,
                                        ctx["agent"]["id"])}, 200
    refused = transport_block(ctx) if transport_block else None
    if refused:
        return {"reply": refused}, 200
    # A bare 1-6 answers the menu in HELP and the welcome. Mapped here,
    # before admission, so it is admitted and charged as the question.
    text = menu_question(text) or text
    if not text:
        return {"error": "empty message"}, 400
    if len(text) > 2000:
        return {"error": "message must be 1-2000 characters",
                "reply": TOO_LONG_TEXT}, 400
    if not deps.workers_ready():
        return {"error": "run_workers_unavailable",
                "reply": UNAVAILABLE_TEXT}, 503
    if not deps.allow_turn(surface["account_id"]):
        return {"reply": busy_text(deps.burst_window_seconds)}, 200
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
