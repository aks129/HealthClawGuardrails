"""Security tester probes for PR #862 (daily cap charged in the worker).

The worker now charges with `claim_daily_turn` from up to CARE_RUN_WORKERS
concurrent slots. That function reads the row and writes `turns = used + 1`
as a literal, so two slots that read the same value both pass and the count
rises by one. Run against Postgres to see it (SQLite serialises writers):

    CARE_TEST_DATABASE_URL=postgresql://... uv run pytest -q \
        tests/test_careagents_cap_race_862.py

On SQLite these tests are skipped: they would pass by luck of locking.
"""

from __future__ import annotations

import os
import threading

import pytest

from careagents.accounts import AccountService
from careagents.config import Config
from careagents.models import Account, Base, UsageDay, make_engine

URL = os.environ.get("CARE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL.startswith("postgres"),
    reason="the lost update needs a database with concurrent writers")


def _svc():
    engine = make_engine(URL)
    Base.metadata.drop_all(engine)
    engine.dispose()
    cfg = Config(env={"CARE_DATABASE_URL": URL, "CARE_RP_ID": "localhost",
                      "CARE_ORIGIN": "http://localhost", "OPENAI_API_KEY": "k",
                      "HEALTHCLAW_MINT_SECRET": "m"})
    svc = AccountService(cfg)
    with svc.session() as s:
        s.add(Account(id="acct_race", email="race@example.com"))
    return svc


def _hammer(svc, n, cap):
    barrier = threading.Barrier(n)
    allowed = []

    def go():
        barrier.wait()
        ok, _ = svc.claim_daily_turn("acct_race", cap)
        allowed.append(ok)
    ts = [threading.Thread(target=go) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    with svc.session() as s:
        rows = s.query(UsageDay).filter_by(account_id="acct_race").all()
        charged = sum(int(r.turns or 0) for r in rows)
    return allowed.count(True), charged, len(rows)


# Fixed in #862: one conditional UPDATE charges, and a unique constraint on
# (account_id, day) makes the first-of-day insert one row.
def test_exploit_concurrent_charges_let_more_turns_through_than_counted():
    svc = _svc()
    passed, charged = 0, 0
    for _ in range(10):                        # ten bursts of eight
        p, c, rows = _hammer(svc, 8, cap=10_000)
        passed, charged = passed + p, c
        assert rows == 1, f"{rows} rows for one account and day"
    assert charged == passed, (
        f"{passed} turns allowed to the model, {charged} charged")


def test_concurrent_first_charges_of_the_day_are_all_let_through():
    """Eight slots charging an account's first turns of the day race the
    insert of its row. The losers charge the winner's row; none of them
    is refused while the day is far under the cap."""
    svc = _svc()
    for _ in range(10):
        with svc.session() as s:
            s.query(UsageDay).delete()
        p, c, rows = _hammer(svc, 8, cap=10_000)
        assert (p, c, rows) == (8, 8, 1), (
            f"{p} let through, {c} charged, {rows} rows")


def test_exploit_concurrent_charges_exceed_the_cap():
    svc = _svc()
    worst = 0
    for _ in range(10):
        with svc.session() as s:
            s.query(UsageDay).delete()
        p, c, rows = _hammer(svc, 8, cap=1)
        worst = max(worst, p)
        assert (c, rows) == (1, 1), f"{c} charged over {rows} rows"
    assert worst <= 1, f"{worst} turns let through against a cap of 1"
