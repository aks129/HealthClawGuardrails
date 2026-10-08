"""Tester feedback: /feedback asks four questions and emails the answers to
the team. It replaces the "tell us" mailto links, which sent a tester off to
their mail app and lost most answers.

The answers are kept nowhere. CareAgents stores no PHI, and a tester may
type some in spite of the note on the page, so the text goes into one email
and is never written to a table or a log. It is untrusted: each answer is
capped, the HTML part escapes it, and no answer reaches a header. The
subject and recipient are fixed; the reply-to is the signed-in account's
own address, and is left out when nobody is signed in.

JSON only, as every form here: a cross-site page cannot send it without a
preflight, which is the CSRF guard. Sends are rate-limited per account, or
per client address when signed out.
"""

from __future__ import annotations

import ipaddress
import logging
import time
import unicodedata
from collections import OrderedDict, deque

from flask import jsonify, render_template, request, session
from markupsafe import escape

from careagents import mail
from careagents.beta_signup import valid_email

logger = logging.getLogger("careagents.feedback")

FEEDBACK_TO = "contactus@healthclaw.io"
SUBJECT = "Tester feedback"

#: (field name, question), in the order the page asks them.
QUESTIONS = (
    ("stuck", "Where did you get stuck or confused? Tell us which screen."),
    ("change", "What would you change, and why? The why matters most."),
    ("trust", "Did anything feel wrong, or make you not trust it?"),
    ("real", "Would you connect your real records to this? Why or why not?"),
)
ANSWER_MAX = 2000

#: Emails sent per account (or client address) per window, and how many
#: keys the limiter remembers before it forgets the oldest.
SENDS_PER_WINDOW = 5
WINDOW_SECONDS = 3600
LIMITER_KEYS = 512

_KEEP = ("\n", "\t")


def clean(value: str) -> str:
    """One answer as sent: line breaks normalized, control and format
    characters other than newline and tab dropped, ends stripped."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in value if c in _KEEP or unicodedata.category(c)
                   not in ("Cc", "Cf")).strip()


def read_answers(body) -> tuple[dict | None, str | None]:
    """(answers, None), or (None, the error code). Every field is optional
    and must be a string; at least one must say something."""
    if not isinstance(body, dict):
        return None, "invalid"
    answers = {}
    for name, _ in QUESTIONS:
        raw = body.get(name, "")
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            return None, "invalid"
        value = clean(raw)
        if len(value) > ANSWER_MAX:
            return None, "too_long"
        answers[name] = value
    if not any(answers.values()):
        return None, "empty"
    return answers, None


def compose(answers: dict, signed_in: bool) -> tuple[str, str]:
    """(html, text) for the email. Only the body carries answer text."""
    who = ("From a signed-in tester. Reply to this email to answer them."
           if signed_in else
           "From someone not signed in, so there is no address to reply to.")
    text_parts = ["Tester feedback from CareAgents.", who]
    html_parts = []
    for name, question in QUESTIONS:
        answer = answers[name] or "(no answer)"
        text_parts.append(f"{question}\n{answer}")
        html_parts.append(
            f"<p><b>{escape(question)}</b><br>"
            f"{str(escape(answer)).replace(chr(10), '<br>')}</p>")
    html = (f"<div style='font-family:system-ui,sans-serif;max-width:560px'>"
            f"<h2 style='color:#22190E'>Tester feedback</h2>"
            f"<p>{escape(who)}</p>{''.join(html_parts)}</div>")
    return html, "\n\n".join(text_parts)


def register(app, svc, cfg) -> None:
    sends: OrderedDict[str, deque] = OrderedDict()

    def _client_key() -> str:
        # As /beta does: X-Real-IP, which Railway's edge sets, and never
        # X-Forwarded-For, whose entries can be the client's own words.
        raw = (request.headers.get("X-Real-IP") or "").strip()
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            return request.remote_addr or ""

    def _allow(key: str) -> bool:
        moment = time.time()
        window = sends.pop(key, None) or deque()
        while window and moment - window[0] > WINDOW_SECONDS:
            window.popleft()
        allowed = len(window) < SENDS_PER_WINDOW
        if allowed:
            window.append(moment)
        sends[key] = window                 # newest last
        while len(sends) > LIMITER_KEYS:
            sends.popitem(last=False)       # forget the oldest key
        return allowed

    def _account():
        aid = session.get("account_id")
        return svc.get_account(aid) if aid else None

    @app.get("/feedback")
    def feedback_page():
        return render_template("feedback.html", done=False,
                               questions=QUESTIONS, answer_max=ANSWER_MAX,
                               signed_in=_account() is not None)

    @app.get("/feedback/thanks")
    def feedback_thanks():
        return render_template("feedback.html", done=True,
                               signed_in=_account() is not None)

    @app.post("/feedback")
    def feedback_submit():
        if not request.is_json:
            return jsonify({"error": "json_required"}), 415
        answers, error = read_answers(request.get_json(silent=True))
        if error:
            return jsonify({"error": error}), 400
        acct = _account()
        key = f"acct:{acct.id}" if acct else f"ip:{_client_key()}"
        if not _allow(key):
            return jsonify({"error": "rate_limited"}), 429
        reply_to = None
        if acct and valid_email(acct.email or ""):
            reply_to = acct.email
        html, text = compose(answers, signed_in=acct is not None)
        try:
            outcome = mail.send_message(cfg, FEEDBACK_TO, SUBJECT, html, text,
                                        reply_to=reply_to)
        except Exception:                   # pragma: no cover - defensive
            # No exc_info: a traceback can carry the answers.
            logger.warning("feedback email failed to send")
            outcome = mail.NOT_SENT
        if outcome == mail.NOT_SENT:
            # Nothing left, so the page keeps the answers and says so.
            return jsonify({"error": "not_sent"}), 503
        # SENT, or UNCONFIRMED: the request went out and the answer was
        # lost, so the mail may have arrived. The thank-you page claims
        # nothing about delivery.
        return jsonify({"ok": True, "redirect": "/feedback/thanks"})
