"""Two Fasten connects from one account at the same moment make one pending
row (#847). The second arrives while the first holds the lease."""

from __future__ import annotations

from careagents.models import Connection
from tests.careagents_stage1_helpers import allowlist_cfg, approve_terms
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, cfg, svc)


def _two_tabs(cfg, svc, monkeypatch, email="gene@example.com"):  # noqa: F811
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    first = app.test_client()
    _login(first, svc, monkeypatch, email=email)
    second = app.test_client()
    with first.session_transaction() as s:
        snapshot = dict(s)
    with second.session_transaction() as s:
        s.update(snapshot)
    return first, second


def test_a_connect_that_arrives_mid_insert_does_not_add_a_row(
        cfg, svc, monkeypatch):  # noqa: F811
    first, second = _two_tabs(cfg, svc, monkeypatch)
    read = svc.pending_connection
    fired = {"r": None}

    def read_then_second_tab(aid, kind):
        seen = read(aid, kind)
        if fired["r"] is None:
            fired["r"] = "in flight"      # the second tab reads once, too
            fired["r"] = second.post("/api/connections/fasten",
                                     json={"consent": True})
        return seen

    monkeypatch.setattr(svc, "pending_connection", read_then_second_tab)
    r = first.post("/api/connections/fasten", json={"consent": True})

    assert r.status_code == 200
    assert fired["r"] is not None, "the second tab never ran"
    assert fired["r"].status_code == 409
    assert "—" not in fired["r"].get_json()["error"]
    with svc.session() as s:
        assert s.query(Connection).filter_by(kind="fasten").count() == 1


def test_the_lease_is_released_after_a_connect(cfg, svc, monkeypatch):  # noqa: F811
    first, second = _two_tabs(cfg, svc, monkeypatch)
    first.post("/api/connections/fasten", json={"consent": True})
    from careagents.models import Account
    with svc.session() as s:
        assert s.query(Account).one().sample_claim_at is None


def test_a_second_connect_after_the_first_reuses_the_pending_row(
        cfg, svc, monkeypatch):  # noqa: F811
    first, second = _two_tabs(cfg, svc, monkeypatch)
    a = first.post("/api/connections/fasten", json={"consent": True})
    b = second.post("/api/connections/fasten", json={"consent": True})
    assert a.get_json()["id"] == b.get_json()["id"]
    assert b.get_json()["existing"] is True


def test_a_revoked_invite_cannot_reuse_a_pending_row(monkeypatch):
    """Review Focus 3: the reuse path hands out a connect URL too."""
    from careagents.accounts import AccountService
    from careagents.app import create_app
    stage1 = allowlist_cfg()
    accounts = AccountService(stage1)
    approve_terms(monkeypatch)
    accounts.invite_real_records("gene@example.com", "operator")
    app = create_app(config=stage1, client=FakeClient(), accounts=accounts)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, accounts, monkeypatch)
    assert c.post("/api/connections/fasten",
                  json={"consent": True}).status_code == 200
    accounts.revoke_real_records_invite("gene@example.com")
    r = c.post("/api/connections/fasten", json={"consent": True})
    assert r.status_code == 503
    assert "connect_url" not in (r.get_json() or {})


def test_a_paused_account_cannot_reuse_a_pending_row(cfg, svc, monkeypatch):  # noqa: F811
    first, _ = _two_tabs(cfg, svc, monkeypatch)
    assert first.post("/api/connections/fasten",
                      json={"consent": True}).status_code == 200
    svc.set_paused("gene@example.com", True)
    r = first.post("/api/connections/fasten", json={"consent": True})
    assert r.status_code == 503
    assert "connect_url" not in (r.get_json() or {})
