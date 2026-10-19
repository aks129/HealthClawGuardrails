"""QA for #979: the confirmation-email quota pieces no other test pins.

Each test here was written against a mutation that the PR's own suite let
through: the day never resetting (a mailbox locked out for good after
CONFIRM_DAILY sends), `retry_after` rounding down (the form telling someone
to retry a moment early), and the conditional update that stops two
workers racing on one mailbox from both sending.

Synthetic data only.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from careagents import beta_signup
from tests.test_careagents_beta_signup import _cfg

EMAIL = "quinn.qa979@example.com"


@pytest.fixture
def svc():
    from careagents.accounts import AccountService
    service = AccountService(_cfg())
    yield service
    service.engine.dispose()


def _cap(svc):
    with svc.session() as s:
        return s.get(beta_signup.BetaMailCap,
                     beta_signup.hash_token(beta_signup.mailbox(EMAIL)))


def test_the_daily_cap_lifts_once_the_day_is_over(svc):
    t0 = 1_000_000.0
    step = beta_signup.CONFIRM_RESEND + 1
    for i in range(beta_signup.CONFIRM_DAILY):
        assert beta_signup.claim_mail(svc.session, EMAIL, t0 + i * step) == 0
    last = t0 + beta_signup.CONFIRM_DAILY * step
    assert beta_signup.claim_mail(svc.session, EMAIL, last) > 0
    # A day after the first email, the mailbox may be emailed again. The
    # day rolls (#979 security Low 1): the other four of the last 24 hours
    # still count, so the next waits until the second is a day old.
    later = t0 + beta_signup.CONFIRM_CAP + 1
    assert beta_signup.claim_mail(svc.session, EMAIL, later) == 0
    assert beta_signup.send_times(_cap(svc))[-1] == later
    assert beta_signup.claim_mail(
        svc.session, EMAIL, later + step) == pytest.approx(
            t0 + step + beta_signup.CONFIRM_CAP - (later + step), abs=1)
    # Once every one of them is a day old, all five are free again.
    fresh = later + beta_signup.CONFIRM_CAP
    for i in range(beta_signup.CONFIRM_DAILY):
        assert beta_signup.claim_mail(
            svc.session, EMAIL, fresh + i * step) == 0


def test_retry_after_never_says_a_moment_too_early(svc):
    t0 = 1_000_000.0
    assert beta_signup.claim_mail(svc.session, EMAIL, t0) == 0
    # 59.5 s left is said as 60, never 59.
    assert beta_signup.claim_mail(
        svc.session, EMAIL, t0 + 0.5) == beta_signup.CONFIRM_RESEND
    # A fraction of a second left is said as one second, never zero
    # (zero would read as "taken").
    assert beta_signup.claim_mail(
        svc.session, EMAIL, t0 + beta_signup.CONFIRM_RESEND - 0.1) == 1


def test_a_worker_that_read_a_stale_row_does_not_also_send(svc):
    """Two workers read the cap row; the other one claims first. The loser's
    update is conditional on the `sent_at` it read, so it must not win."""
    t0 = 1_000_000.0
    assert beta_signup.claim_mail(svc.session, EMAIL, t0) == 0
    peer = t0 + 100
    assert beta_signup.claim_mail(svc.session, EMAIL, peer) == 0

    # What this worker read before the peer's claim landed.
    stale = SimpleNamespace(sent_at=t0, sends=repr(t0))

    @contextmanager
    def stale_scope():
        with svc.session() as s:
            s.get = lambda *a, **k: stale
            yield s

    wait = beta_signup.claim_mail(stale_scope, EMAIL, peer + 1)
    assert wait > 0
    cap = _cap(svc)
    assert (cap.sent_at, cap.day_count) == (peer, 2)
