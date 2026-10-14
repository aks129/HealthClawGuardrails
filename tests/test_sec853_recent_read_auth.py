"""Security sign-off of PR #853: `GET /r6/actions?status=recent` is read-gated.

Moving the `status == 'recent'` branch above `authenticate_tenant_read`
survived every existing test at e90d30b: the suites run with the read-auth
flag off, and the shared `test-tenant` is in PUBLIC_TENANTS, so the gate is
never exercised. This pins it with the flag on and a private tenant: no
credential, another tenant's token, and another tenant's token as a bearer
are all refused, and the owner's own token is served.
"""

from __future__ import annotations

import pytest

PRIVATE = "sec853-private"


@pytest.fixture
def read_auth_on(monkeypatch):
    monkeypatch.setenv("READ_AUTH_ENABLED", "1")


def _token(tenant):
    from r6.stepup import generate_step_up_token
    return generate_step_up_token(tenant)


def _done(client, app, headers):
    r = client.post("/r6/actions/propose", json={
        "kind": "sms", "payload": {"to": "CVS Pharmacy", "phone": "617-555-0100",
                                   "body": "Reminder."}}, headers=headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    from models import db
    from r6.actions.models import ProposedAction
    with app.app_context():
        ProposedAction.query.filter_by(id=r.get_json()["id"]).update(
            {"status": "completed"}, synchronize_session=False)
        db.session.commit()


def test_the_recent_list_needs_the_tenants_own_credential(
        client, app, other_tenant_headers, read_auth_on):
    own_headers = {"X-Tenant-Id": PRIVATE, "X-Step-Up-Token": _token(PRIVATE)}
    _done(client, app, own_headers)
    url = "/r6/actions?status=recent"
    foreign = other_tenant_headers["X-Step-Up-Token"]
    assert client.get(url, headers={"X-Tenant-Id": PRIVATE}).status_code == 401
    assert client.get(url, headers={"X-Tenant-Id": PRIVATE,
                                    "X-Step-Up-Token": foreign}).status_code == 401
    assert client.get(url, headers={"X-Tenant-Id": PRIVATE,
                                    "Authorization": "Bearer " + foreign}
                      ).status_code == 401
    own = client.get(url, headers=own_headers)
    assert own.status_code == 200 and own.get_json()["count"] == 1
    other = client.get(url, headers=other_tenant_headers)
    assert other.status_code == 200 and other.get_json()["count"] == 0
