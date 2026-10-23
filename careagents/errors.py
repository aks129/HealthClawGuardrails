"""Plain-language 404 and 500 pages, and the count of every 500.

Without these a missing page or a crash showed Flask's bare page: no way
back, nothing a tester could report, and nothing the digest counted.

A JSON route (`/api/...`) keeps answering JSON, since a page in place of
the body it parses would break the caller. Only 404, 405 and 500 are
handled here; every other status keeps the body its route chose. A 405 (a
link that only a form may use, say /logout) reads as a missing page: to a
person it is one.

A 500 is counted by the endpoint name, which the code fixes, and only
when it is one of this app's own endpoints. Never the URL, its query
string, or the exception's text: those can carry what a person typed.

The 500 handler must not fail in turn. The database may be what broke, so
it reads nothing from it (the home link comes from the session cookie
alone), the count is wrapped, and a template that will not render falls
back to one line of text.
"""

from __future__ import annotations

import logging

from flask import jsonify, render_template, request, session

from careagents import beta, funnel
from careagents.agent import (GENERIC_FAILURE_TEXT, RATE_LIMITED_TEXT,
                              UNAVAILABLE_TEXT)

logger = logging.getLogger("careagents.errors")

#: The screen a 404 reports. Fixed, never the endpoint: a route that hides
#: itself with a 404 (/admin/funnel) must not be named in the link.
NOT_FOUND_SCREEN = "not_found"


def _wants_json() -> bool:
    return request.path.startswith("/api/")


def _home() -> str:
    return "/home" if session.get("account_id") else "/"


#: The sentences a failed chat turn leaves in the transcript (agent.py).
FAILURE_TEXTS = frozenset({GENERIC_FAILURE_TEXT, RATE_LIMITED_TEXT,
                           UNAVAILABLE_TEXT})


def collapse_failed_turns(past: list[dict]) -> list[dict]:
    """The replayed history with each run of failed turns shown once.

    A tester who retried a broken chat came back to a wall of the same
    failure bubble. A failed turn is a question answered by
    one of FAILURE_TEXTS; of consecutive failed turns only the last is
    kept, and a failure bubble right after another is dropped. The kept
    failure is marked `failed`, so the page can offer "Report a problem".
    Nothing is deleted anywhere: this is only what the page shows.
    """
    def failure(m):
        # Every failure ends with the 911 line (beta.with_911); one led by
        # the emergency line instead is never collapsed, so it stays shown.
        return (m.get("role") != "user"
                and beta.without_trailer(str(m.get("content") or "").strip())
                in FAILURE_TEXTS)

    out: list[dict] = []
    for m in past:
        if not failure(m):
            out.append(m)
            continue
        if out and failure(out[-1]):
            continue                            # the same wall, once
        if (len(out) >= 3 and out[-1].get("role") == "user"
                and failure(out[-2]) and out[-3].get("role") == "user"):
            del out[-3:-1]      # question, failure, question: keep the last
        out.append(dict(m, failed=True))
    return out


def known_endpoint(app, endpoint) -> str | None:
    """The endpoint name if it is one of the app's own, else None."""
    if (isinstance(endpoint, str) and endpoint in app.view_functions
            and endpoint != "static"):
        return endpoint
    return None


def register(app, svc, cfg) -> None:

    @app.errorhandler(404)
    def not_found(_exc):
        if _wants_json():
            return jsonify({"error": "not_found"}), 404
        return render_template("error.html", status=404, home=_home(),
                               screen=NOT_FOUND_SCREEN), 404

    @app.errorhandler(405)
    def method_not_allowed(exc):
        # Werkzeug's Allow header goes with it, as the status requires.
        methods = getattr(exc, "valid_methods", None)
        allow = {"Allow": ", ".join(methods)} if methods else {}
        if _wants_json():
            return jsonify({"error": "method_not_allowed"}), 405, allow
        return render_template("error.html", status=405, home=_home(),
                               screen=NOT_FOUND_SCREEN), 405, allow

    @app.errorhandler(500)
    def server_error(_exc):
        endpoint = known_endpoint(app, request.endpoint) or "unknown"
        funnel.safely(funnel.count_detail, svc.session, "server_error",
                      endpoint)
        if _wants_json():
            return jsonify({"error": "server_error"}), 500
        try:
            return render_template(
                "error.html", status=500, home=_home(),
                screen=endpoint if endpoint != "unknown" else None), 500
        except Exception:                   # noqa: BLE001 - last resort
            logger.warning("error page failed to render")
            return ("Something broke on our side. Please try again.", 500,
                    {"Content-Type": "text/plain; charset=utf-8"})
