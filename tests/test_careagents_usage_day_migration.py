"""One `ca_usage_days` row per account and day (#862 review F5).

A database made before the unique constraint may already hold two rows for
one account and day: two first-of-day inserts raced. Boot collapses them
into one, summing the turns, then adds the unique index, and does both
again harmlessly on every later boot. Runs on SQLite, and on Postgres too
when CARE_TEST_DATABASE_URL points at one.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from careagents.models import Account, Base, UsageDay, make_engine

PG = os.environ.get("CARE_TEST_DATABASE_URL", "")
URLS = ["sqlite"] + (["postgres"] if PG.startswith("postgres") else [])

OLD_ROWS = [("u1", "acct_a", "2026-10-01", 2), ("u2", "acct_a", "2026-10-01", 3),
            ("u3", "acct_a", "2026-10-02", 1),
            ("u4", "acct_b", "2026-10-01", 4), ("u5", "acct_b", "2026-10-01", 0),
            ("u6", "acct_b", "2026-10-01", 1)]


def _old_database(kind, tmp_path) -> str:
    """The schema as shipped before the constraint, with duplicate rows."""
    url = PG if kind == "postgres" else f"sqlite:///{tmp_path}/old.db"
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(
        engine, tables=[t for t in Base.metadata.sorted_tables
                        if t.name != "ca_usage_days"])
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE ca_usage_days (id VARCHAR(32) PRIMARY KEY, "
            "account_id VARCHAR(32), day VARCHAR(10) NOT NULL, "
            "turns INTEGER)"))
        for row in OLD_ROWS:
            conn.execute(text("INSERT INTO ca_usage_days VALUES "
                              "(:i, :a, :d, :t)"),
                         dict(zip("iadt", row)))
    engine.dispose()
    return url


def _rows(engine):
    with engine.connect() as conn:
        return sorted(tuple(r) for r in conn.execute(text(
            "SELECT account_id, day, turns FROM ca_usage_days")))


@pytest.mark.parametrize("kind", URLS)
def test_boot_collapses_duplicates_and_adds_the_unique_index(kind, tmp_path):
    url = _old_database(kind, tmp_path)
    engine = make_engine(url)
    want = [("acct_a", "2026-10-01", 5), ("acct_a", "2026-10-02", 1),
            ("acct_b", "2026-10-01", 5)]
    assert _rows(engine) == want
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO ca_usage_days VALUES "
                              "('u9', 'acct_a', '2026-10-02', 1)"))
    engine.dispose()
    # A second boot (web and worker both run this) changes nothing.
    again = make_engine(url)
    assert _rows(again) == want
    again.dispose()


@pytest.mark.parametrize("kind", URLS)
def test_a_new_database_has_the_constraint_from_the_model(kind, tmp_path):
    url = PG if kind == "postgres" else f"sqlite:///{tmp_path}/new.db"
    if kind == "postgres":
        e = create_engine(url, future=True)
        Base.metadata.drop_all(e)
        e.dispose()
    engine = make_engine(url)
    with engine.begin() as conn:      # a real account: only the pair repeats
        conn.execute(Account.__table__.insert().values(
            id="acct_n", email="n@example.com"))
        conn.execute(UsageDay.__table__.insert().values(
            id="n1", account_id="acct_n", day="2026-10-01", turns=0))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(UsageDay.__table__.insert().values(
                id="n2", account_id="acct_n", day="2026-10-01", turns=0))
    engine.dispose()
