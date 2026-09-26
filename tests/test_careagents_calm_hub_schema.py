"""The calm hub's new account columns reach an existing database safely.

`create_all()` never adds a column to a table that already exists, so a live
database gets them from `_ensure_columns`. These pin the two things a deploy
depends on: an existing row survives with the new columns empty, and a peer
process that added the column first is not an error.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from careagents import models

NEW_COLUMNS = {"sample_claim_at", "first_agent_at"}


@pytest.fixture
def url(tmp_path):
    env = os.environ.get("CARE_TEST_DATABASE_URL", "")
    if env and not env.startswith("sqlite"):
        engine = create_engine(env)
        models.Base.metadata.drop_all(engine)
        engine.dispose()
        return env
    return f"sqlite:///{tmp_path / 'legacy.db'}"


def _legacy_accounts(url):
    """ca_accounts as it stood before the calm hub, with one account in it."""
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE ca_accounts (id VARCHAR(32) PRIMARY KEY, "
            "email VARCHAR(255) NOT NULL UNIQUE, email_verified_at FLOAT, "
            "created_at FLOAT, last_login_at FLOAT)"))
        conn.execute(text(
            "INSERT INTO ca_accounts (id, email, created_at) "
            "VALUES ('acct_legacy', 'legacy@example.com', 1.0)"))
    return engine


def test_an_existing_account_row_gains_the_columns_empty(url):
    legacy = _legacy_accounts(url)
    engine = models.make_engine(url)
    cols = {c["name"] for c in inspect(engine).get_columns("ca_accounts")}
    assert NEW_COLUMNS <= cols
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT email, " + ", ".join(sorted(NEW_COLUMNS))
            + " FROM ca_accounts WHERE id = 'acct_legacy'")).one()
    assert row[0] == "legacy@example.com"
    assert all(v is None for v in row[1:])
    engine.dispose()
    legacy.dispose()


def test_a_column_a_peer_already_added_is_not_an_error(url):
    """Web and worker start together; the loser of the ALTER must live."""
    engine = models.make_engine(url)
    for name in NEW_COLUMNS:
        models._add_column(engine, "ca_accounts", name, "FLOAT")
    with pytest.raises(Exception):
        models._add_column(engine, "ca_no_such_table", "x", "FLOAT")
    engine.dispose()
