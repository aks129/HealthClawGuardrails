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


def test_step_five_label_is_on_the_hub_a_new_tester_sees(made, monkeypatch):
    app, svc = made()
    body = _signed_in(app, svc, monkeypatch).get("/home").get_data(
        as_text=True)
    (step,) = [s for s in beta_signup.NEXT_STEPS if "made-up records" in s]
    (label,) = re.findall(r'"([^"]+)"', step)
    buttons = re.findall(
        r'<button[^>]*data-connector="sample"[^>]*>\s*([^<]*?)\s*</button>',
        body)
    assert buttons, "no sample button on a fresh account's hub"
    assert all(b == label for b in buttons), buttons
