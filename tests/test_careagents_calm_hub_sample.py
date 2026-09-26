"""One sample per account (calm hub spec section 4 and 9)."""

from __future__ import annotations

from careagents.healthclaw import HealthClawError
from careagents.models import Connection
from tests.test_careagents import FakeClient, _login, _make_account
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

#: The CareAgents fixtures, re-exported under their own names.
cfg = _cfg_fixture
svc = _svc_fixture


def _app(cfg, svc, fake):
    from careagents.app import create_app
    a = create_app(config=cfg, client=fake, accounts=svc)
    a.config["TESTING"] = True
    return a


def _samples(svc):
    with svc.session() as s:
        return s.query(Connection).filter_by(kind="sample").count()


def _account_id(client):
    with client.session_transaction() as s:
        return s["account_id"]


class _TapDuringSeed(FakeClient):
    """Fires a second tap from inside the first tap's seed call.

    That is the window a double tap lands in: the first request has minted
    a tenant and is seeding it, and no connection row exists yet.
    """

    def __init__(self):
        super().__init__()
        self.tap = None
        self.second_tap = None

    def seed(self, tenant):
        tap, self.tap = self.tap, None
        if tap is not None:
            self.second_tap = tap()
        return super().seed(tenant)


def test_a_second_tap_returns_the_existing_sample(cfg, svc, monkeypatch):
    """MUTATION M1: drop the first active_sample early return -> red."""
    fake = FakeClient()
    c = _app(cfg, svc, fake).test_client()
    _login(c, svc, monkeypatch)
    first = c.post("/api/connections/sample")
    second = c.post("/api/connections/sample")
    assert first.status_code == 200 and second.status_code == 200
    assert first.get_json()["existing"] is False
    assert second.get_json()["existing"] is True
    assert second.get_json()["id"] == first.get_json()["id"]
    assert len(fake.tenants) == 1
    assert _samples(svc) == 1


def test_a_tap_during_seeding_mints_no_second_tenant(cfg, svc, monkeypatch):
    """MUTATION M2: make claim_sample_start always return True -> red."""
    fake = _TapDuringSeed()
    app = _app(cfg, svc, fake)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    other = app.test_client()
    with other.session_transaction() as s:
        s["account_id"] = _account_id(c)
    fake.tap = lambda: other.post("/api/connections/sample")

    first = c.post("/api/connections/sample")

    assert first.status_code == 200, first.get_data(as_text=True)
    assert fake.second_tap.status_code == 409
    assert fake.second_tap.get_json()["status"] == "connecting"
    assert "—" not in fake.second_tap.get_json()["error"]
    assert len(fake.tenants) == 1
    assert _samples(svc) == 1


def test_a_failed_seed_releases_the_lease(cfg, svc, monkeypatch):
    class _Down(FakeClient):
        def seed(self, tenant):
            raise HealthClawError("seed failed", 503)

    c = _app(cfg, svc, _Down()).test_client()
    _login(c, svc, monkeypatch)
    assert c.post("/api/connections/sample").status_code == 503
    assert svc.claim_sample_start(_account_id(c)) is True


def test_the_sample_lease_is_single_winner_and_expires(svc, monkeypatch):
    from careagents import accounts
    acct = _make_account(svc, monkeypatch, "lease@example.com")
    assert svc.claim_sample_start(acct.id) is True
    assert svc.claim_sample_start(acct.id) is False
    later = accounts.now() + accounts.SAMPLE_LEASE_SECONDS + 1
    monkeypatch.setattr(accounts, "now", lambda: later)
    assert svc.claim_sample_start(acct.id) is True
    svc.release_sample_start(acct.id)
    assert svc.claim_sample_start(acct.id) is True


def test_an_account_with_old_duplicate_samples_keeps_them_all(
        cfg, svc, monkeypatch):
    """Nothing is merged or deleted. The oldest active sample is the one
    a tap opens."""
    fake = FakeClient()
    c = _app(cfg, svc, fake).test_client()
    _login(c, svc, monkeypatch)
    aid = _account_id(c)
    oldest = svc.add_connection(aid, "sample", "ca-legacy-1", "Sample records")
    svc.add_connection(aid, "sample", "ca-legacy-2", "Sample records")

    r = c.post("/api/connections/sample")

    assert r.get_json()["id"] == oldest
    assert _samples(svc) == 2
    assert fake.tenants == []
    assert c.get("/home").status_code == 200


def test_a_revoked_sample_does_not_block_a_new_one(cfg, svc, monkeypatch):
    fake = FakeClient()
    c = _app(cfg, svc, fake).test_client()
    _login(c, svc, monkeypatch)
    first = c.post("/api/connections/sample").get_json()["id"]
    c.post(f"/api/connections/{first}/disconnect")
    second = c.post("/api/connections/sample").get_json()
    assert second["id"] != first and second["existing"] is False
