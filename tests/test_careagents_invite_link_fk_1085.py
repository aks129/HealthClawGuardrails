"""Deleting an account that has an invite link, with foreign keys enforced
(#1085).

The hub now makes the account's invite link on the first visit, as
Settings already did, so every account that opens the hub has a
ca_invite_links row pointing at ca_accounts. Postgres enforces that key;
SQLite does not unless asked, which is how a bare row delete in a test got
past local runs and failed only in CI's Postgres lane. These tests turn the
key on for SQLite too (PRAGMA foreign_keys, on the one in-memory
connection), so either database catches a delete that leaves the link
behind. Inviter deletion with someone already joined:
tests/test_careagents_invite_links.py::test_deleting_the_inviter_removes_their_link.

Synthetic data only.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from careagents import invites
from careagents.models import Account
from tests.test_careagents import (  # noqa: F401  (fixtures)
    _login, app, cfg, svc)

pytestmark = pytest.mark.extension

EMAIL = "fk.check@example.com"


@pytest.fixture
def enforced(svc):  # noqa: F811
    """Foreign keys on, whichever database this lane runs."""
    if svc.engine.dialect.name == "sqlite":
        # In-memory SQLite keeps one connection per thread, and the test
        # client runs in this one, so the pragma holds for the test.
        with svc.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    return svc


def _signed_in_with_link(app, svc, monkeypatch):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    assert c.get("/home").status_code == 200
    assert c.get("/settings").status_code == 200
    with svc.session() as s:
        aid = s.query(Account).filter_by(email=EMAIL).one().id
        assert s.query(invites.InviteLink).filter_by(account_id=aid).count() == 1
    return c, aid


def test_the_key_is_enforced_here(app, enforced, monkeypatch):  # noqa: F811
    # The control: a bare row delete, leaving the link, is refused, so the
    # test below would see a delete path that forgot the link.
    svc = enforced  # noqa: F811
    _, aid = _signed_in_with_link(app, svc, monkeypatch)
    with pytest.raises(IntegrityError):
        with svc.session() as s:
            s.delete(s.get(Account, aid))


def test_deleting_an_account_that_opened_the_hub_removes_its_link(
        app, enforced, monkeypatch):  # noqa: F811
    svc = enforced  # noqa: F811
    c, aid = _signed_in_with_link(app, svc, monkeypatch)
    assert svc.delete_account(aid) is True
    with svc.session() as s:
        assert s.get(Account, aid) is None
        assert s.query(invites.InviteLink).filter_by(account_id=aid).count() == 0
    # And the old session is signed out, not a 500.
    r = c.get("/home")
    assert r.status_code == 302 and r.headers["Location"].endswith("/auth")
