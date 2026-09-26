"""A sample tap that loses the read but wins the lease (QA, PR #840).

`add_connection` checks `active_sample`, then `claim_sample_start`. If the
first tap commits its connection and releases the lease between those two
calls of a second tap, the second tap wins a free lease and mints another
tenant. The lease winner has to look for an active sample again.
"""

from __future__ import annotations

from careagents.models import Connection
from tests.test_careagents import FakeClient, _login
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

cfg = _cfg_fixture
svc = _svc_fixture


def test_a_tap_that_read_no_sample_does_not_mint_after_the_first_lands(
        cfg, svc, monkeypatch):
    from careagents.app import create_app
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    late = app.test_client()
    _login(late, svc, monkeypatch)
    first = app.test_client()
    with late.session_transaction() as s:
        account_id = s["account_id"]
    with first.session_transaction() as s:
        s["account_id"] = account_id

    read = svc.active_sample
    armed = {"on": True}

    def read_then_let_the_first_tap_finish(aid):
        seen = read(aid)
        if armed["on"]:
            armed["on"] = False
            assert first.post("/api/connections/sample").status_code == 200
        return seen

    monkeypatch.setattr(svc, "active_sample",
                        read_then_let_the_first_tap_finish)

    r = late.post("/api/connections/sample")

    assert r.status_code == 200
    assert len(fake.tenants) == 1, fake.tenants
    with svc.session() as s:
        assert s.query(Connection).filter_by(kind="sample").count() == 1
