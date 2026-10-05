"""Security tester probes for #862 round 2 (atomic daily charge, unique day).

Postgres only (CARE_TEST_DATABASE_URL); skipped on SQLite, which serialises
writers and would pass by luck of locking.
"""

from __future__ import annotations

import multiprocessing
import os
import threading

import pytest
from sqlalchemy import create_engine, inspect, text

from careagents.accounts import AccountService
from careagents.config import Config
from careagents.models import Account, Base, UsageDay, make_engine

URL = os.environ.get("CARE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL.startswith("postgres"),
    reason="needs a database with concurrent writers")


def _svc():
    engine = make_engine(URL)
    Base.metadata.drop_all(engine)
    engine.dispose()
    cfg = Config(env={"CARE_DATABASE_URL": URL, "CARE_RP_ID": "localhost",
                      "CARE_ORIGIN": "http://localhost", "OPENAI_API_KEY": "k",
                      "HEALTHCLAW_MINT_SECRET": "m"})
    svc = AccountService(cfg)
    with svc.session() as s:
        s.add(Account(id="acct_r2", email="r2@example.com"))
    return svc


def _burst(svc, n, cap):
    barrier = threading.Barrier(n)
    out, errors = [], []

    def go():
        barrier.wait()
        try:
            out.append(svc.claim_daily_turn("acct_r2", cap)[0])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
    ts = [threading.Thread(target=go) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    with svc.session() as s:
        rows = s.query(UsageDay).filter_by(account_id="acct_r2").all()
    return out.count(True), sum(int(r.turns or 0) for r in rows), len(rows), errors


def test_first_charge_of_the_day_at_cap_one_lets_exactly_one_through():
    svc = _svc()
    for _ in range(25):
        with svc.session() as s:
            s.query(UsageDay).delete()          # no row: the insert path
        allowed, charged, rows, errors = _burst(svc, 16, cap=1)
        assert errors == []
        assert (allowed, charged, rows) == (1, 1, 1)


# Fixed in #862: a charge that loses the insert race retries the UPDATE.
def test_first_charge_burst_counts_every_turn_let_through():
    svc = _svc()
    for _ in range(25):
        with svc.session() as s:
            s.query(UsageDay).delete()
        allowed, charged, rows, errors = _burst(svc, 16, cap=10_000)
        assert errors == []
        assert (allowed, charged, rows) == (16, 16, 1)


def test_bursts_at_the_cap_never_pass_it():
    svc = _svc()
    allowed_total = 0
    for _ in range(10):
        a, charged, rows, errors = _burst(svc, 16, cap=20)
        allowed_total += a
        assert errors == [] and rows == 1
        assert charged == allowed_total <= 20


# --- migration: concurrent boots on a database with duplicates -------------

def _old_schema_with_duplicates():
    engine = create_engine(URL, future=True)
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ca_usage_days"))
    Base.metadata.create_all(
        engine, tables=[t for t in Base.metadata.sorted_tables
                        if t.name != "ca_usage_days"])
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE ca_usage_days (id VARCHAR(32) PRIMARY KEY, "
            "account_id VARCHAR(32), day VARCHAR(10) NOT NULL, "
            "turns INTEGER)"))
        rows = []
        for a in range(30):
            for k in range(3):
                rows.append({"i": f"u{a}_{k}", "a": f"acct_{a}",
                             "d": "2026-10-05", "t": k + 1})
        rows.append({"i": "unull", "a": "acct_n", "d": "2026-10-05", "t": None})
        rows.append({"i": "unull2", "a": "acct_n", "d": "2026-10-05", "t": 4})
        conn.execute(text("INSERT INTO ca_usage_days VALUES (:i, :a, :d, :t)"),
                     rows)
    engine.dispose()


def _boot(url, q):
    try:
        make_engine(url).dispose()
        q.put("ok")
    except Exception as exc:  # noqa: BLE001
        q.put(repr(exc)[:300])


# Fixed in #862: the migration runs under one Postgres advisory lock.
def test_four_processes_booting_at_once_collapse_once_and_keep_every_turn():
    for _ in range(5):
        _old_schema_with_duplicates()
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        ps = [ctx.Process(target=_boot, args=(URL, q)) for _ in range(4)]
        for p in ps:
            p.start()
        for p in ps:
            p.join(120)
        results = [q.get(timeout=5) for _ in ps]
        # Integrity first: whatever the boots did, no turn was lost.
        crashed = [r for r in results if r != "ok"]
        engine = create_engine(URL, future=True)
        with engine.connect() as conn:
            got = dict(conn.execute(text(
                "SELECT account_id, SUM(turns) FROM ca_usage_days "
                "GROUP BY account_id")).all())
            n = conn.execute(text(
                "SELECT COUNT(*) FROM ca_usage_days")).scalar()
        idx = [i for i in inspect(engine).get_indexes("ca_usage_days")
               if i["unique"] and i["column_names"] == ["account_id", "day"]]
        engine.dispose()
        assert n == 31
        assert all(got[f"acct_{a}"] == 6 for a in range(30))
        assert got["acct_n"] == 4
        assert idx
        assert crashed == [], crashed
