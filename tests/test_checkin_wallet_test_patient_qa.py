"""QA for #1027: the wallet's offer and the route's refusal must agree.

The page offers the connectathon test patient when the records relay
answers `no_records` or `sample_only` (careagents/static/checkin_wallet.js,
OFFER_TEST). The relay (`records_tenant`) counts only the account holder's
own *active* connections. The route refuses with `has_records` when
`has_real_connection` is true, which is any non-sample connection in any
state, for anyone on the account.

So an account whose only real-record connection is still waiting for its
first file (`direct`, status "empty", which a tap on "Upload a file"
creates), was revoked, is a Fasten connect still pending, or belongs to
someone the account holder cares for, is offered the test patient. The tap
then gets 409 `has_records`, a code the page has no words for, and the
person reads "We couldn't read your records just now. Try check-in again
in a few minutes." Retrying never helps. Shown live at 375px on
2026-10-22.

Fixed: the relay now answers `records_not_ready` for such an account,
which the page does not offer the test patient on.

Synthetic data only.
"""
from __future__ import annotations

import pytest

from tests.test_checkin_wallet_test_patient import (  # noqa: F401
    RELAY, _choose, tester)

#: The relay answers the page treats as "offer the test patient".
OFFER_REASONS = {"no_records", "sample_only"}


def _offer_then_refusal(browser):
    r = browser.post(RELAY, json={"resource_types": ["Patient"]})
    why = r.get_json().get("error") if r.status_code == 409 else None
    c = _choose(browser)
    refused = c.status_code == 409 and c.get_json() == {"error": "has_records"}
    return why, refused


@pytest.mark.parametrize("kind,status", [
    ("direct", "empty"),     # "Upload a file" tapped, no file yet
    ("direct", "revoked"),   # disconnected upload
    ("fasten", "pending"),   # a connect still on its way
])
def test_a_waiting_real_connection_is_not_offered_what_it_is_refused(
        tester, kind, status):  # noqa: F811
    browser, hc, svc, acct = tester
    svc.add_connection(acct.id, kind, "ca-real-waiting", "Records",
                       status=status)
    why, refused = _offer_then_refusal(browser)
    assert not (why in OFFER_REASONS and refused), (why, refused)


def test_a_cared_for_persons_records_do_not_offer_what_is_refused(
        tester):  # noqa: F811
    browser, hc, svc, acct = tester
    other = svc.add_person(acct.id, "parent")
    svc.add_connection(acct.id, "direct", "ca-real-parent", "Records",
                       person_id=other)
    why, refused = _offer_then_refusal(browser)
    assert not (why in OFFER_REASONS and refused), (why, refused)


def test_an_active_real_connection_is_neither_offered_nor_minted(
        tester):  # noqa: F811
    """The agreeing case, pinned so a fix for the above cannot open it: the
    relay answers from the real tenant and the route refuses."""
    browser, hc, svc, acct = tester
    svc.add_connection(acct.id, "direct", "ca-real", "Upload")
    why, refused = _offer_then_refusal(browser)
    assert why is None and refused
    assert hc.seeded == []
