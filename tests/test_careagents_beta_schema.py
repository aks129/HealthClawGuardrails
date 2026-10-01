"""Stage 1's table and column reach an existing database safely.

Same shape as test_careagents_calm_hub_schema.py: a legacy ca_accounts row
survives with the new column empty, the new table is created, and a peer
that added the column first is not an error. The invite table itself
shipped with #852; this file pins only what stage 1 adds, plus the delete
rule that now covers both.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from careagents import models
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)


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


def test_an_existing_account_gains_an_empty_pause_column(url):
    legacy = _legacy_accounts(url)
    engine = models.make_engine(url)
    cols = {c["name"] for c in inspect(engine).get_columns("ca_accounts")}
    assert "real_paused_at" in cols
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT real_paused_at FROM ca_accounts "
            "WHERE id = 'acct_legacy'")).one()
    assert row[0] is None
    engine.dispose()
    legacy.dispose()


def test_the_stage1_tables_carry_only_their_listed_columns(url):
    engine = models.make_engine(url)
    insp = inspect(engine)
    assert {c["name"] for c in insp.get_columns("ca_real_record_invites")} == {
        "email", "invited_at", "invited_by", "revoked_at"}
    assert {c["name"] for c in insp.get_columns("ca_activity_days")} == {
        "id", "account_id", "day", "asked", "approved"}
    engine.dispose()


def test_a_peer_that_added_the_pause_column_first_is_not_an_error(url):
    legacy = _legacy_accounts(url)
    with legacy.begin() as conn:
        conn.execute(text(
            "ALTER TABLE ca_accounts ADD COLUMN real_paused_at FLOAT"))
    engine = models.make_engine(url)
    models._add_column(engine, "ca_accounts", "real_paused_at", "FLOAT")
    engine.dispose()
    legacy.dispose()


def test_deleting_an_account_removes_its_invite_and_activity(svc, monkeypatch):  # noqa: F811
    from careagents.models import ActivityDay
    from tests.test_careagents import _make_account
    acct = _make_account(svc, monkeypatch, "leaver@example.com")
    acct_id = getattr(acct, "id", acct)
    svc.invite_real_records("leaver@example.com", "operator")
    svc.invite_real_records("stayer@example.com", "operator")
    with svc.session() as s:
        s.add(ActivityDay(account_id=acct_id, day="2026-10-01", asked=1))
    assert svc.delete_account(acct_id) is True
    assert [r["email"] for r in svc.real_record_invites()] == [
        "stayer@example.com"]
    with svc.session() as s:
        assert s.query(ActivityDay).count() == 0


def test_a_connection_reports_its_consent_version(svc, monkeypatch):  # noqa: F811
    from tests.test_careagents import _make_account
    acct = _make_account(svc, monkeypatch, "c@example.com")
    acct_id = getattr(acct, "id", acct)
    cid = svc.add_connection(acct_id, "fasten", "t-1", "My records",
                             status="pending", consent_version="2026-08-01")
    assert svc.get_connection(acct_id, cid)["consent_version"] == "2026-08-01"
