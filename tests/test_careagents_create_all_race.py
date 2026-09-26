"""Web and worker starting together on a fresh database must both come up.

Found filming the demo: CareAgents web and worker launched at the same time
on a new SQLite file. Both ran create_all(), both saw ca_accounts missing,
and web died on "table ca_accounts already exists". The collision means the
table is there, which is the goal, so it is not an error.

A real paired start fails too rarely to gate on (4 in 40 before the fix), so
the race is reproduced deterministically. Every table's create is replaced
with one that loses: the peer creates the table first, then this process's
CREATE TABLE raises the error SQLite raises.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import Table, inspect
from sqlalchemy.exc import OperationalError

import careagents.models as models


def _error(message, table="ca_accounts"):
    return OperationalError("CREATE TABLE %s" % table, {},
                            sqlite3.OperationalError(message))


def _peer_wins_every_table(monkeypatch, message=None):
    real = Table.create
    lost = []

    def _create(self, bind, checkfirst=False):
        real(self, bind, checkfirst=True)          # the peer got there first
        lost.append(self.name)
        raise _error(message or "table %s already exists" % self.name,
                     self.name)

    monkeypatch.setattr(Table, "create", _create)
    return lost


def test_losing_the_race_on_every_table_still_brings_the_app_up(
        monkeypatch, tmp_path):
    """MUTATION: re-raise every error in _create_tables -> red."""
    lost = _peer_wins_every_table(monkeypatch)
    engine = models.make_engine("sqlite:///%s" % (tmp_path / "ca.db"))
    assert set(lost) == set(models.Base.metadata.tables)
    assert set(inspect(engine).get_table_names()) >= set(
        models.Base.metadata.tables)


def test_a_different_database_error_is_still_raised(monkeypatch, tmp_path):
    """MUTATION: drop the message check -> red. Only a peer's table is
    tolerated; a locked or unreachable database still fails loudly."""
    _peer_wins_every_table(monkeypatch, message="database is locked")
    with pytest.raises(OperationalError, match="database is locked"):
        models.make_engine("sqlite:///%s" % (tmp_path / "ca.db"))


def test_a_second_start_on_an_existing_database_is_unchanged(tmp_path):
    url = "sqlite:///%s" % (tmp_path / "ca.db")
    models.make_engine(url)
    engine = models.make_engine(url)
    assert set(inspect(engine).get_table_names()) >= set(
        models.Base.metadata.tables)
