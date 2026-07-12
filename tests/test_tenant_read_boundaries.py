"""Tenant-bound authentication on non-FHIR read surfaces."""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone

import pytest

from r6.stepup import generate_step_up_token


PRIVATE_TENANT = "boundary-private"


@pytest.fixture(autouse=True)
def _private_only(monkeypatch):
    # Production requires this flag on. Local/test deployments may leave it
    # off for compatibility with the existing header-only demo workflow.
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_TENANTS", "desktop-demo")


def _read_headers(tenant=PRIVATE_TENANT):
    return {
        "X-Tenant-Id": tenant,
        "X-Step-Up-Token": generate_step_up_token(tenant, scope="read"),
    }


def _oauth_token(tenant=PRIVATE_TENANT):
    from r6 import oauth

    token = secrets.token_urlsafe(16)
    oauth._access_tokens[token] = {
        "client_id": "boundary-test",
        "scopes": ["patient/*.read"],
        "tenant_id": tenant,
        "exp": time.time() + 3600,
    }
    return token


def test_fasten_connection_list_rejects_bare_tenant_header(client):
    response = client.get(
        "/fasten/connections", headers={"X-Tenant-Id": PRIVATE_TENANT}
    )
    assert response.status_code == 401


def test_fasten_connection_list_accepts_tenant_bound_step_up(client):
    response = client.get("/fasten/connections", headers=_read_headers())
    assert response.status_code == 200


def test_fasten_connection_list_accepts_tenant_bound_oauth(client):
    response = client.get(
        "/fasten/connections",
        headers={
            "X-Tenant-Id": PRIVATE_TENANT,
            "Authorization": f"Bearer {_oauth_token()}",
        },
    )
    assert response.status_code == 200


def test_fasten_connection_list_accepts_tenant_bound_session(client):
    from r6.command_center import access

    signed_link = access.generate_access_token(PRIVATE_TENANT)
    login = client.get("/command-center", query_string={"t": signed_link})
    assert login.status_code == 302

    response = client.get(
        "/fasten/connections", headers={"X-Tenant-Id": PRIVATE_TENANT}
    )
    assert response.status_code == 200


def test_fasten_connection_list_keeps_explicit_demo_public(client):
    response = client.get(
        "/fasten/connections", headers={"X-Tenant-Id": "desktop-demo"}
    )
    assert response.status_code == 200


def test_fasten_connection_list_keeps_flag_off_local_compatibility(
    client, monkeypatch
):
    monkeypatch.delenv("READ_AUTH_ENABLED", raising=False)
    response = client.get(
        "/fasten/connections", headers={"X-Tenant-Id": PRIVATE_TENANT}
    )
    assert response.status_code == 200


def test_fasten_agent_access_authenticates_before_minting(client, app):
    from models import db
    from r6.fasten.models import FastenConnection

    with app.app_context():
        db.session.add(
            FastenConnection(
                org_connection_id="boundary-org",
                tenant_id=PRIVATE_TENANT,
                webhook_verified_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()

    bare = client.get(
        "/fasten/connections/boundary-org/agent-access",
        headers={"X-Tenant-Id": PRIVATE_TENANT},
    )
    assert bare.status_code == 401

    authorized = client.get(
        "/fasten/connections/boundary-org/agent-access",
        headers=_read_headers(),
    )
    assert authorized.status_code == 200
    assert authorized.get_json()["tenant_id"] == PRIVATE_TENANT


def test_wearable_status_rejects_bare_tenant_query(client):
    response = client.get(f"/wearables/sync-status?tenant_id={PRIVATE_TENANT}")
    assert response.status_code == 401


def test_wearable_status_accepts_bound_credential(client):
    response = client.get(
        f"/wearables/sync-status?tenant_id={PRIVATE_TENANT}",
        headers=_read_headers(),
    )
    assert response.status_code == 200
    assert response.get_json()["tenant_id"] == PRIVATE_TENANT


def test_wearable_status_rejects_credential_for_another_tenant(client):
    response = client.get(
        f"/wearables/sync-status?tenant_id={PRIVATE_TENANT}",
        headers=_read_headers("other-private"),
    )
    assert response.status_code == 401


def test_rx_transfer_proposal_rejects_bare_private_tenant(client):
    response = client.post(
        "/r6/actions/rx-transfer/propose",
        headers={"X-Tenant-Id": PRIVATE_TENANT},
        json={"to_pharmacy": {"name": "Local Pharmacy", "phone": "+15551234567"}},
    )
    assert response.status_code == 401


def test_rx_transfer_proposal_accepts_bound_credential(client):
    response = client.post(
        "/r6/actions/rx-transfer/propose",
        headers=_read_headers(),
        json={"to_pharmacy": {"name": "Local Pharmacy", "phone": "+15551234567"}},
    )
    # Auth passed; there simply are no medications to propose in this fixture.
    assert response.status_code == 422


def test_command_center_private_dashboard_accepts_bound_step_up(client):
    response = client.get(
        "/command-center",
        query_string={"tenant": PRIVATE_TENANT},
        headers=_read_headers(),
    )
    assert response.status_code == 200


def test_command_center_private_api_accepts_bound_oauth(client):
    response = client.get(
        "/command-center/api/overview",
        query_string={"tenant": PRIVATE_TENANT},
        headers={"Authorization": f"Bearer {_oauth_token()}"},
    )
    assert response.status_code == 200
