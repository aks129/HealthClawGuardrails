"""A request still waits after its assistant is deleted (calm hub spec 3.1).

Deleting an assistant removes the assistant only; a committed request on
its connection's tenant stays awaiting confirmation in HealthClaw. The hub
must not then tell the person "Nothing yet": with requests pending it is a
band, and a count it cannot give is "Couldn't check", never zero.

Found by QA on PR #843 against a live HealthClaw: sample connect, commit a
form-fill action on the tenant, delete the assistant, and
/api/approvals/count answered 200 {"count": 0} while the engine still held
the request. `test_no_assistant_is_an_honest_zero` (no connection at all)
stays right; this is the account that has records and no assistant.
"""

from __future__ import annotations

import pytest

from tests.test_careagents import FakeClient, _login
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

cfg = _cfg_fixture
svc = _svc_fixture


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def app(cfg, svc, fake):
    from careagents.app import create_app
    a = create_app(config=cfg, client=fake, accounts=svc)
    a.config["TESTING"] = True
    return a


def test_a_request_waiting_after_its_assistant_is_deleted_is_not_zero(
        app, svc, fake, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    before = c.get("/api/approvals/count").get_json()
    assert before["count"] >= 1, "precondition: the tenant has a request"

    assert c.delete(f"/api/agents/{agent}").status_code == 200

    r = c.get("/api/approvals/count")
    body = r.get_json()
    # Either the waiting request is still counted, or the hub is told it
    # could not check. A 200 zero renders "Nothing yet" over a live request.
    assert not (r.status_code == 200 and body.get("count") == 0), body
