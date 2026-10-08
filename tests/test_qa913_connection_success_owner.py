"""QA on #913: connection_success asks the REGISTERED row's tenant, not the payload's.

`_handle_connection_success` picks `owner = existing.tenant_id if existing
else tenant_id`. No other test sends an `external_id` that differs from the
registered row's tenant, so replacing that line with `owner = tenant_id`
survived the whole suite (QA mutation M02). A signed event naming another,
live tenant must not verify or re-export a connection whose own tenant was
disconnected.

Synthetic tenants and ids only.
"""

from tests.test_fasten_revoke_on_disconnect import (  # noqa: F401  (fixture)
    OTHER, TENANT, _audits, _connection, _revoke, _webhook, secret)

from models import db
from r6.fasten.models import FastenConnection, tenant_revoked


def test_connection_success_naming_another_tenant_still_holds(client, secret):  # noqa: F811
    """MUTATION: `owner = tenant_id` in _handle_connection_success -> the
    export is requested and the revoked row is stamped verified."""
    _connection(tenant=TENANT, org="oc-qa913-owner")
    assert _revoke(client).status_code == 200
    assert not tenant_revoked(OTHER)

    resp, trigger, _ = _webhook(client, "patient.connection_success", {
        "org_connection_id": "oc-qa913-owner", "external_id": OTHER})

    assert resp.status_code == 200
    assert not trigger.called, "an export was requested for a revoked row"
    db.session.expire_all()
    conn = db.session.get(FastenConnection, "oc-qa913-owner")
    assert conn.tenant_id == TENANT
    assert conn.webhook_verified_at is None
    assert len(_audits("fasten_import_refused", tenant=TENANT)) == 1
