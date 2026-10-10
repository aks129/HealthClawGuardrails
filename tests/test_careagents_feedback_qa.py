"""QA additions for the tester feedback form and next steps (PR #905).

Gaps the PR's own tests leave open:
- the signed session cookie is a place answers could be kept without
  touching a table;
- the steps test checks a label is somewhere in home.html, which has two
  "Explore with made-up records" buttons; renaming the one a new tester
  actually sees (real records closed) went unnoticed. This renders the hub
  a fresh account reaches and checks the label there.
Synthetic data only.
"""

from __future__ import annotations

import re

from careagents import beta_signup
from tests import test_careagents_feedback as fb
from tests.test_careagents_feedback import MARKER, _send, _signed_in

# The PR's fixtures, shared by name (pytest finds them as module globals).
made = fb.made
sent = fb.sent


def test_answers_are_not_kept_in_the_session_cookie(made, sent, monkeypatch):
    # Flask's session is a signed, not encrypted, cookie: anything put in it
    # is kept on the tester's device and readable there.
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    r = _send(c, stuck=MARKER, change=MARKER, trust=MARKER, real=MARKER)
    assert r.status_code == 200
    assert MARKER not in " ".join(r.headers.getlist("Set-Cookie"))
    with c.session_transaction() as sess:
        assert MARKER not in repr(dict(sess))


def test_a_signed_out_send_keeps_nothing_in_a_cookie(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    r = _send(app.test_client(), stuck=MARKER)
    assert r.status_code == 200
    assert MARKER not in " ".join(r.headers.getlist("Set-Cookie"))


def test_the_chat_step_names_only_what_the_chat_shows(made, monkeypatch):
    """The step that opens the made-up records quotes no hub button (a
    first sign-in opens the chat, tests/test_careagents_first_run.py); its
    one quoted label is the chat's feedback link."""
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    chat = c.get(c.post("/api/connections/sample").get_json()["redirect"])
    (step,) = [s for s in beta_signup.NEXT_STEPS if "made-up records" in s]
    assert re.findall(r'"([^"]+)"', step) == ["Tell us"]
    assert '<a href="/feedback">Tell us</a>' in chat.get_data(as_text=True)
