"""Tester feedback: /feedback asks four questions and emails the answers to
the team. It replaces the "tell us" mailto links, which sent a tester off to
their mail app and lost most answers.

The answers are kept nowhere. CareAgents stores no PHI, and a tester may
type some in spite of the note on the page, so the text goes into one email
and is never written to a table or a log. It is untrusted: each answer is
capped, the HTML part escapes it, and no answer reaches a header. The
subject and recipient are fixed; the reply-to is the signed-in account's
own address. Signed out, a tester may leave an address to be answered at;
it must pass `valid_email`, becomes the reply-to, and is kept nowhere.

JSON only, as every form here: a cross-site page cannot send it without a
preflight, which is the CSRF guard. Sends are rate-limited per account, or
per client address when signed out.

A report can carry context, so "it broke" arrives with where and when. It
is a fixed list, every value checked against a fixed set: the screen (an
endpoint name of this app, never a URL), the last error code, and a chat
answer's run id (a UUID, which cannot spell words). The server adds its
own build and the time; it takes neither from the page, and the
signed-in account's opaque id (never its address, which is only the
reply-to). A run id goes in only when it is the signed-in account's own
run, looked up the way the chat's own replay does; otherwise it is left
out and the rest still goes. The email puts the context in its own
block, apart from the tester's words.

After Send the thank-you page leads back where the tester came from: the
Referer of the form, rebuilt from a fixed list of our own pages
(`safe_back`), never echoed.

"This wasn't right" on a chat answer (POST /feedback/flag) goes the same
way: the answer's run id, a reason code and an optional short note. Never
the answer or the question; a body with any other field is refused. Each
flag is counted by its reason, for the digest.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import re
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from urllib.parse import parse_qs, urlsplit

from flask import jsonify, render_template, request, session
from markupsafe import escape
from werkzeug.exceptions import RequestEntityTooLarge

from careagents import funnel, mail
from careagents.beta_signup import valid_email
from careagents.errors import NOT_FOUND_SCREEN, known_endpoint
from careagents.healthclaw import HealthClawError, acting_for

logger = logging.getLogger("careagents.feedback")

FEEDBACK_TO = "contactus@healthclaw.io"
SUBJECT = "Tester feedback"
FLAG_SUBJECT = "Answer flagged"

#: (field name, question), in the order the page asks them.
QUESTIONS = (
    ("stuck", "Where did you get stuck or confused? Tell us which screen."),
    ("change", "What would you change, and why? The why matters most."),
    ("trust", "Did anything feel wrong, or make you not trust it?"),
    ("real", "Would you connect your real records to this? Why or why not?"),
)
ANSWER_MAX = 2000
#: The most an answer may be before cleaning. A browser's maxlength keeps a
#: tester's answer under ANSWER_MAX; the slack is for line breaks sent as
#: CRLF. Measured raw, so characters `clean` drops still count.
RAW_ANSWER_MAX = 2 * ANSWER_MAX
#: The largest request body read: four answers at RAW_ANSWER_MAX, every
#: character escaped in JSON as \uXXXX (six bytes), and room for the keys
#: and the context.
BODY_MAX = 4 * RAW_ANSWER_MAX * 6 + 4096

#: The pages a thank-you may lead back to, and the one query parameter a
#: return may keep (which assistant). Anything else goes back to the hub.
_BACK_PATHS = ("/", "/home", "/chat", "/brief", "/settings", "/safety")
_BACK_AGENT = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_APPROVALS = re.compile(r"\A/agents/[A-Za-z0-9_-]{1,64}/approvals\Z")

#: The context a report may carry, each checked by `read_context`.
CONTEXT_FIELDS = ("screen", "error", "run_id")
#: The error codes a page may report: the statuses our pages show.
ERROR_CODES = ("400", "403", "404", "405", "500", "503",
               "chat_error", "connection", "rate_limited")
#: An engine run id is str(uuid.uuid4()) (r6/agent_runs/models.py).
_RUN_ID = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")

#: A flag's note is short: what was wrong, in a line or two.
NOTE_MAX = 280
RAW_NOTE_MAX = 2 * NOTE_MAX
FLAG_FIELDS = ("run_id", "reason", "note")
FLAG_BODY_MAX = RAW_NOTE_MAX * 6 + 512

#: Emails sent per account (or client address) per window, and how many
#: keys the limiter remembers before it forgets the oldest.
SENDS_PER_WINDOW = 5
#: Flags have their own window, so flagging a few answers on a test call
#: does not use up the feedback form.
FLAGS_PER_WINDOW = 10
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
        if len(raw) > RAW_ANSWER_MAX:
            return None, "too_long"
        value = clean(raw)
        if len(value) > ANSWER_MAX:
            return None, "too_long"
        answers[name] = value
    if not any(answers.values()):
        return None, "empty"
    return answers, None


def read_context(app, raw) -> tuple[dict | None, str | None]:
    """(context, None), or (None, "invalid"). Only CONTEXT_FIELDS, each a
    string from its fixed set; a missing or empty field is left out."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict) or set(raw) - set(CONTEXT_FIELDS):
        return None, "invalid"
    out = {}
    for name in CONTEXT_FIELDS:
        value = raw.get(name)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            return None, "invalid"
        if name == "screen":
            ok = (value == NOT_FOUND_SCREEN
                  or known_endpoint(app, value) is not None)
        elif name == "error":
            ok = value in ERROR_CODES
        else:
            ok = bool(_RUN_ID.match(value))
        if not ok:
            return None, "invalid"
        out[name] = value
    return out, None


def context_from_args(app, args) -> dict:
    """The context a link to /feedback carries, each field kept only if it
    passes `read_context` on its own. Nothing else reaches the page."""
    out = {}
    for name in CONTEXT_FIELDS:
        one, error = read_context(app, {name: args.get(name, "")})
        if not error:
            out.update(one)
    return out


_CONTEXT_LABELS = (("account", "Account"), ("screen", "Screen"),
                   ("error", "Error code"),
                   ("run_id", "Chat answer (run id)"), ("reason", "Reason"),
                   ("build", "Build"), ("time", "Sent (UTC)"))

#: The most of an account's assistants a run id is looked for under.
RUN_LOOKUP_AGENTS = 12

#: Characters a bare address never needs, and a display name does.
_NOT_BARE = frozenset('<>"')


def _stamp(context: dict, build: str) -> dict:
    """The context with the server's own build and the time added."""
    return {**context, "build": build or "unknown",
            "time": _dt.datetime.now(_dt.timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S")}


def safe_back(raw, host: str) -> str | None:
    """A return path to one of our own pages, rebuilt from checked parts,
    or None. Never the raw value: it may come from a Referer header."""
    if not isinstance(raw, str) or not raw or len(raw) > 512:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.netloc and parts.netloc != host:
        return None
    if parts.scheme not in ("", "http", "https"):
        return None
    path = parts.path or "/"
    if path not in _BACK_PATHS and not _APPROVALS.match(path):
        return None
    agent = (parse_qs(parts.query).get("agent") or [""])[0]
    if agent and _BACK_AGENT.match(agent):
        return f"{path}?agent={agent}"
    return path


def _who(signed_in: bool, reply_to: str | None = None) -> str:
    if signed_in:
        return "From a signed-in tester. Reply to this email to answer them."
    if reply_to:
        return ("From someone not signed in who left an address. Reply to "
                "this email to answer them.")
    return "From someone not signed in, so there is no address to reply to."


def _wrap(title: str, who: str, context: dict,
          words: list[tuple[str, str]]) -> tuple[str, str]:
    """(html, text): who sent it, the app's context block, then the
    tester's own words under a rule. Every value is escaped in the HTML."""
    ctx = [(label, context[k]) for k, label in _CONTEXT_LABELS
           if context.get(k)]
    text_parts = [f"{title} from CareAgents.", who,
                  "== Added by the app (no health details) ==\n" + "\n".join(
                      f"{label}: {value}" for label, value in ctx),
                  "== In the tester's own words =="]
    html_words = []
    for heading, said in words:
        text_parts.append(f"{heading}\n{said}")
        html_words.append(
            f"<p><b>{escape(heading)}</b><br>"
            f"{str(escape(said)).replace(chr(10), '<br>')}</p>")
    html_ctx = "".join(f"<li>{escape(label)}: {escape(value)}</li>"
                       for label, value in ctx)
    html = (f"<div style='font-family:system-ui,sans-serif;max-width:560px'>"
            f"<h2 style='color:#22190E'>{escape(title)}</h2>"
            f"<p>{escape(who)}</p>"
            f"<h3>Added by the app (no health details)</h3>"
            f"<ul style='font-family:monospace'>{html_ctx}</ul>"
            f"<hr><h3>In the tester's own words</h3>"
            f"{''.join(html_words)}</div>")
    return html, "\n\n".join(text_parts)


def compose(answers: dict, signed_in: bool, context: dict | None = None,
            build: str = "unknown", reply_to: str | None = None,
            account: str | None = None) -> tuple[str, str]:
    """(html, text) for the email. Only the body carries answer text."""
    words = [(question, answers[name] or "(no answer)")
             for name, question in QUESTIONS]
    return _wrap("Tester feedback", _who(signed_in, reply_to),
                 _stamp({"account": account, **(context or {})}, build),
                 words)


def read_reply_email(body) -> tuple[str | None, str | None]:
    """(address or None, None), or (None, "bad_email"). The address a
    signed-out tester may leave to be answered at. Optional."""
    raw = body.get("reply_email") if isinstance(body, dict) else None
    if raw is None or raw == "":
        return None, None
    if not isinstance(raw, str):
        return None, "bad_email"
    value = raw.strip()
    if not value:
        return None, None
    # A bare address only: no display name ("Name <a@b.c>") rides along
    # into the reply-to.
    if _NOT_BARE & set(value) or not valid_email(value):
        return None, "bad_email"
    return value, None


def read_flag(body) -> tuple[dict | None, str | None]:
    """(flag, None), or (None, the error code). Exactly FLAG_FIELDS: a run
    id, a reason code and an optional note. Any other field is refused, so
    an answer's text can never ride along."""
    if not isinstance(body, dict) or set(body) - set(FLAG_FIELDS):
        return None, "invalid"
    run_id, reason = body.get("run_id"), body.get("reason")
    note = body.get("note") or ""
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        return None, "invalid"
    if reason not in funnel.FLAG_REASONS:
        return None, "invalid"
    if not isinstance(note, str):
        return None, "invalid"
    if len(note) > RAW_NOTE_MAX:
        return None, "too_long"
    note = clean(note)
    if len(note) > NOTE_MAX:
        return None, "too_long"
    return {"run_id": run_id, "reason": reason, "note": note}, None


def compose_flag(flag: dict, signed_in: bool, build: str = "unknown",
                 account: str | None = None) -> tuple[str, str]:
    """(html, text) for a flagged answer: the account, the pointer (when
    it checked out) and the reason as context, the note as the tester's
    words."""
    label = dict(funnel.FLAG_LABELS)[flag["reason"]]
    context = _stamp({"account": account, "run_id": flag.get("run_id"),
                      "reason": f"{label} ({flag['reason']})"}, build)
    return _wrap("Answer flagged", _who(signed_in), context,
                 [("Their note", flag["note"] or "(no note)")])


def register(app, svc, cfg) -> None:
    sends: OrderedDict[str, deque] = OrderedDict()
    # gunicorn runs several threads per worker: reading, counting and
    # re-inserting a window is one step, or a racing burst beats the limit.
    lock = threading.Lock()

    def _client_key() -> str:
        # As /beta does: X-Real-IP, which Railway's edge sets, and never
        # X-Forwarded-For, whose entries can be the client's own words.
        raw = (request.headers.get("X-Real-IP") or "").strip()
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            return request.remote_addr or ""

    def _allow(key: str, limit: int = SENDS_PER_WINDOW) -> bool:
        moment = time.time()
        with lock:
            window = sends.pop(key, None) or deque()
            while window and moment - window[0] > WINDOW_SECONDS:
                window.popleft()
            allowed = len(window) < limit
            if allowed:
                window.append(moment)
            sends[key] = window             # newest last
            while len(sends) > LIMITER_KEYS:
                sends.popitem(last=False)   # forget the oldest key
            return allowed

    def _refund(key: str) -> None:
        # A send that did not go out gives its slot back, so "try again in
        # a few minutes" is not answered with an hour's lockout. The cap
        # still bounds mail that went out, and the size check runs first.
        with lock:
            window = sends.get(key)
            if window:
                window.pop()

    def _account():
        aid = session.get("account_id")
        return svc.get_account(aid) if aid else None

    def _run_is_theirs(acct, run_id: str) -> bool:
        """Is `run_id` a run of one of this account's own assistants? The
        same rule as the chat's replay (app.py api_chat_events): the
        account's agent, its tenant, then the engine's tenant-scoped
        lookup. Any doubt, an engine error included, is a no."""
        hc = app.extensions["careagents_runtime"]["client"]
        agents = svc.list_home(acct.id)["agents"][:RUN_LOOKUP_AGENTS]
        for agent in agents:
            ctx = svc.get_agent_context(acct.id, agent["id"])
            if not ctx:
                continue
            try:
                with acting_for(acct.id):
                    run = hc.get_agent_run(ctx["tenant"], run_id)
            except HealthClawError:
                continue
            if (run.get("tenant_id") == ctx["tenant"]
                    and run.get("agent_id") == agent["id"]):
                return True
        return False

    def _own_run_only(acct, context: dict) -> dict:
        """The context with its run id kept only if it is the account's.
        Signed out, never: nothing says whose it is."""
        run_id = context.get("run_id")
        if run_id and (acct is None or not _run_is_theirs(acct, run_id)):
            return {k: v for k, v in context.items() if k != "run_id"}
        return context

    def _send(subject, html, text, reply_to) -> bool:
        """Mail one report to the team. False when it surely did not go."""
        try:
            outcome = mail.send_message(cfg, FEEDBACK_TO, subject, html, text,
                                        reply_to=reply_to)
        except Exception:                   # pragma: no cover - defensive
            # No exc_info: a traceback can carry the answers.
            logger.warning("feedback email failed to send")
            outcome = mail.NOT_SENT
        if outcome == mail.NOT_SENT:
            funnel.note_mail(svc.session, cfg, outcome)
            return False
        # SENT, or UNCONFIRMED: the request went out and the answer was
        # lost, so the mail may have arrived.
        return True

    @app.get("/feedback")
    def feedback_page():
        return render_template("feedback.html", done=False,
                               questions=QUESTIONS, answer_max=ANSWER_MAX,
                               signed_in=_account() is not None,
                               context=context_from_args(app, request.args),
                               back=safe_back(request.referrer, request.host))

    @app.get("/feedback/thanks")
    def feedback_thanks():
        signed_in = _account() is not None
        back = (safe_back(request.args.get("back"), request.host)
                or ("/home" if signed_in else "/"))
        path = back.split("?")[0]
        label = ("Back to your chat" if path == "/chat" else
                 "Back to your visit brief" if path == "/brief" else
                 "Back to your hub" if path == "/home" else
                 "Back to CareAgents" if path == "/" else
                 "Back to where you were")
        return render_template(
            "feedback.html", done=True, signed_in=signed_in,
            back=back, back_label=label,
            will_reply=signed_in or request.args.get("reply") == "1")

    @app.post("/feedback")
    def feedback_submit():
        if not request.is_json:
            return jsonify({"error": "json_required"}), 415
        # Size before parsing, with this route's own cap: an app-wide one
        # would have to admit a records upload. A body with no
        # Content-Length (chunked) is cut off at the same size by the
        # request's limit.
        clen = request.content_length
        if clen is not None and clen > BODY_MAX:
            return jsonify({"error": "too_long"}), 413
        request.max_content_length = BODY_MAX
        try:
            body = request.get_json(silent=True)
        except RequestEntityTooLarge:
            return jsonify({"error": "too_long"}), 413
        answers, error = read_answers(body)
        if error:
            return jsonify({"error": error}), 400
        context, error = read_context(app, body.get("context"))
        if error:
            return jsonify({"error": error}), 400
        acct = _account()
        # Signed out, the address a tester left to be answered at; signed
        # in, the account's own. Checked before the rate limit is spent.
        reply_to = None
        if acct is None:
            reply_to, error = read_reply_email(body)
            if error:
                return jsonify({"error": error}), 400
        key = f"acct:{acct.id}" if acct else f"ip:{_client_key()}"
        if not _allow(key):
            funnel.safely(funnel.count_issue, svc.session, "rate_limited")
            return jsonify({"error": "rate_limited"}), 429
        if acct and valid_email(acct.email or ""):
            reply_to = acct.email
        # After the rate limit, so the lookups behind it are bounded too.
        context = _own_run_only(acct, context)
        html, text = compose(answers, signed_in=acct is not None,
                             context=context, build=cfg.build_sha,
                             reply_to=reply_to,
                             account=acct.id if acct else None)
        if not _send(SUBJECT, html, text, reply_to):
            # Nothing left, so the page keeps the answers and says so.
            _refund(key)
            return jsonify({"error": "not_sent"}), 503
        # SENT, or UNCONFIRMED: the request went out and the answer was
        # lost, so the mail may have arrived. The thank-you page claims
        # nothing about delivery.
        if acct is not None:
            funnel.safely(funnel.mark, svc.session, acct.id, "feedback_sent")
        return jsonify({"ok": True, "redirect": "/feedback/thanks"})

    @app.post("/feedback/flag")
    def feedback_flag():
        """'This wasn't right' on one chat answer. Signed in only: the
        answers are on the chat page, which is. The same gates as the form:
        JSON only, and the size checked before the body is parsed."""
        if not request.is_json:
            return jsonify({"error": "json_required"}), 415
        clen = request.content_length
        if clen is not None and clen > FLAG_BODY_MAX:
            return jsonify({"error": "too_long"}), 413
        request.max_content_length = FLAG_BODY_MAX
        try:
            body = request.get_json(silent=True)
        except RequestEntityTooLarge:
            return jsonify({"error": "too_long"}), 413
        acct = _account()
        if acct is None:
            return jsonify({"error": "sign_in_required"}), 401
        flag, error = read_flag(body)
        if error:
            return jsonify({"error": error}), 400
        key = f"flag:acct:{acct.id}"
        if not _allow(key, FLAGS_PER_WINDOW):
            funnel.safely(funnel.count_issue, svc.session, "rate_limited")
            return jsonify({"error": "rate_limited"}), 429
        reply_to = acct.email if valid_email(acct.email or "") else None
        # Not this account's run: the note and the reason still go.
        flag = _own_run_only(acct, flag)
        html, text = compose_flag(flag, signed_in=True, build=cfg.build_sha,
                                  account=acct.id)
        if not _send(FLAG_SUBJECT, html, text, reply_to):
            _refund(key)
            return jsonify({"error": "not_sent"}), 503
        funnel.safely(funnel.count_detail, svc.session, "answer_flagged",
                      flag["reason"])
        return jsonify({"ok": True})
