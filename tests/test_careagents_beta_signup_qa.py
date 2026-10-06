"""QA additions for PR #874: two guards the suite did not pin.

Both were found by mutation: dropping `promote(s)` from `purge`, and making
`purge` delete caps that are still live, left every existing test green.
Synthetic data only.
"""

from __future__ import annotations

import time

from careagents import beta_signup
from tests.test_careagents_beta_signup import (  # noqa: F401 (fixtures)
    _post, _queue, _row, _rows, made, sent)


def test_purge_gives_a_freed_spot_to_the_queue(made, sent):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    _queue(app, sent, svc)
    assert _row(svc, "w1@example.com")["status"] == "waitlist"
    # t0 holds a spot, has no account, and is past REQUEST_DAYS.
    with svc.session() as s:
        s.get(beta_signup.BetaRequest, "t0@example.com").created_at = (
            time.time() - (beta_signup.REQUEST_DAYS + 1) * 86400)
    r = app.test_cli_runner().invoke(args=["beta-requests", "purge"])
    assert r.exit_code == 0, r.output
    assert "t0@example.com" not in {r["email"] for r in _rows(svc)}
    assert _row(svc, "w1@example.com")["status"] == "new"
    assert _row(svc, "w2@example.com")["status"] == "waitlist"


def test_purge_keeps_a_cap_that_is_still_live(made, sent):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _post(c, ip="198.51.100.1")
    assert len(sent) == 1
    r = app.test_cli_runner().invoke(args=["beta-requests", "purge"])
    assert r.exit_code == 0, r.output
    # Same address, same day, after the weekly purge: still capped.
    _post(c, ip="198.51.100.2", first_name="Mallory")
    assert len(sent) == 1
    assert _row(svc)["first_name"] != "Mallory"
