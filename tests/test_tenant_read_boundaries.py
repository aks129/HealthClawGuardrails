"""Tenant-bound authentication on non-FHIR read surfaces."""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

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


def test_fasten_first_token_uses_one_time_browser_enrollment(client, monkeypatch):
    """The registering browser needs no pre-existing tenant credential."""
    import json
    from unittest.mock import patch

    monkeypatch.delenv("REDIS_URL", raising=False)
    org_id = "browser-enrollment-org"
    registered = client.post(
        "/fasten/connections",
        headers={"X-Tenant-Id": PRIVATE_TENANT},
        json={"org_connection_id": org_id, "platform_type": "epic"},
    )
    assert registered.status_code == 201
    assert "HttpOnly" in registered.headers.get("Set-Cookie", "")

    pending = client.get(f"/fasten/connections/{org_id}/agent-access")
    assert pending.status_code == 202

    webhook = {
        "type": "patient.connection_success",
        "data": {"org_connection_id": org_id, "external_id": PRIVATE_TENANT},
    }
    with patch("r6.fasten.routes.verify_webhook", return_value=True):
        verified = client.post(
            "/fasten/webhook", data=json.dumps(webhook), content_type="application/json"
        )
    assert verified.status_code == 200

    minted = client.get(f"/fasten/connections/{org_id}/agent-access")
    assert minted.status_code == 200, minted.get_data(as_text=True)
    token = minted.get_json()["read_token"]

    replay = client.get(
        f"/fasten/connections/{org_id}/agent-access",
        headers={
            "X-Tenant-Id": PRIVATE_TENANT,
            "X-Step-Up-Token": token,
        },
    )
    assert replay.status_code == 410


def test_fasten_enrollment_proof_is_browser_bound(client, app, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    org_id = "browser-bound-org"
    assert client.post(
        "/fasten/connections",
        headers={"X-Tenant-Id": PRIVATE_TENANT},
        json={"org_connection_id": org_id},
    ).status_code == 201

    from models import db
    from r6.fasten.models import FastenConnection

    with app.app_context():
        conn = db.session.get(FastenConnection, org_id)
        conn.webhook_verified_at = datetime.now(timezone.utc)
        db.session.commit()

    other_browser = app.test_client()
    denied = other_browser.get(f"/fasten/connections/{org_id}/agent-access")
    assert denied.status_code == 401
    assert client.get(f"/fasten/connections/{org_id}/agent-access").status_code == 200


def test_bare_reregistration_cannot_rotate_browser_enrollment(client, app, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    org_id = "no-proof-rotation-org"
    assert client.post(
        "/fasten/connections",
        headers={"X-Tenant-Id": PRIVATE_TENANT},
        json={"org_connection_id": org_id},
    ).status_code == 201

    attacker = app.test_client()
    repeated = attacker.post(
        "/fasten/connections",
        headers={"X-Tenant-Id": PRIVATE_TENANT},
        json={"org_connection_id": org_id},
    )
    assert repeated.status_code == 200

    from models import db
    from r6.fasten.models import FastenConnection

    with app.app_context():
        conn = db.session.get(FastenConnection, org_id)
        conn.webhook_verified_at = datetime.now(timezone.utc)
        db.session.commit()

    assert attacker.get(f"/fasten/connections/{org_id}/agent-access").status_code == 401
    assert client.get(f"/fasten/connections/{org_id}/agent-access").status_code == 200


def test_enrollment_issuance_db_failure_rolls_back_and_retries(
    client, app, monkeypatch
):
    import json
    from unittest.mock import patch

    monkeypatch.delenv("REDIS_URL", raising=False)
    org_id = "enrollment-rollback-org"
    assert client.post(
        "/fasten/connections",
        headers={"X-Tenant-Id": PRIVATE_TENANT},
        json={"org_connection_id": org_id},
    ).status_code == 201

    webhook = {
        "type": "patient.connection_success",
        "data": {"org_connection_id": org_id, "external_id": PRIVATE_TENANT},
    }
    with patch("r6.fasten.routes.verify_webhook", return_value=True):
        assert client.post(
            "/fasten/webhook", data=json.dumps(webhook), content_type="application/json"
        ).status_code == 200

    with patch(
        "r6.fasten.routes.db.session.commit",
        side_effect=RuntimeError("injected commit failure"),
    ):
        try:
            failed = client.get(f"/fasten/connections/{org_id}/agent-access")
        except RuntimeError:
            pytest.fail("issuance database failure escaped the route")
    assert failed.status_code == 503

    from models import db
    from r6.fasten.models import FastenConnection

    db.session.expire_all()
    conn = db.session.get(FastenConnection, org_id)
    assert conn.agent_token_issued_at is None
    assert conn.enrollment_proof_hash is not None
    assert client.get(f"/fasten/connections/{org_id}/agent-access").status_code == 200


def test_enrollment_conditional_consume_allows_exactly_one_claim(
    client, app, monkeypatch
):
    monkeypatch.delenv("REDIS_URL", raising=False)
    org_id = "enrollment-conditional-org"
    assert client.post(
        "/fasten/connections",
        headers={"X-Tenant-Id": PRIVATE_TENANT},
        json={"org_connection_id": org_id},
    ).status_code == 201

    cookie_name = app.config["SESSION_COOKIE_NAME"]
    enrollment_cookie = client.get_cookie(cookie_name)
    assert enrollment_cookie is not None
    competing_browser = app.test_client()
    competing_browser.set_cookie(cookie_name, enrollment_cookie.value)

    from models import db
    from r6.fasten.models import FastenConnection

    conn = db.session.get(FastenConnection, org_id)
    assert getattr(conn, "enrollment_proof_hash", None) is not None
    conn.webhook_verified_at = datetime.now(timezone.utc)
    db.session.commit()

    first = client.get(f"/fasten/connections/{org_id}/agent-access")
    second = competing_browser.get(f"/fasten/connections/{org_id}/agent-access")
    assert first.status_code == 200
    assert second.status_code == 410

    db.session.expire_all()
    consumed = db.session.get(FastenConnection, org_id)
    assert consumed.agent_token_issued_at is not None
    assert consumed.enrollment_proof_hash is None
    assert consumed.enrollment_expires_at is None


def test_connect_page_polls_with_same_origin_enrollment_cookie():
    page = (
        Path(__file__).resolve().parents[1] / "templates" / "fasten_connect.html"
    ).read_text(encoding="utf-8")
    assert "credentials: 'same-origin'" in page
    poll_start = page.index("async function _pollAgentAccess")
    poll_end = page.index("function _showAgentAccess", poll_start)
    assert "X-Tenant-Id" not in page[poll_start:poll_end]


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


def test_rx_read_token_cannot_persist_proposal(client, app):
    import json
    from r6.actions.models import ProposedAction

    write_token = generate_step_up_token(PRIVATE_TENANT)
    medication = {
        "resourceType": "MedicationRequest",
        "status": "active",
        "intent": "order",
        "medicationCodeableConcept": {"text": "Metformin 500 mg"},
        "subject": {"reference": "Patient/rx-boundary"},
    }
    created = client.post(
        "/r6/fhir/MedicationRequest",
        headers={
            "X-Tenant-Id": PRIVATE_TENANT,
            "X-Step-Up-Token": write_token,
            "X-Human-Confirmed": "true",
            "Content-Type": "application/fhir+json",
        },
        data=json.dumps(medication),
    )
    assert created.status_code == 201

    before = ProposedAction.query.filter_by(tenant_id=PRIVATE_TENANT).count()
    read_token = generate_step_up_token(PRIVATE_TENANT, scope="read")
    response = client.post(
        "/r6/actions/rx-transfer/propose",
        headers={
            "X-Tenant-Id": PRIVATE_TENANT,
            "X-Step-Up-Token": read_token,
        },
        json={"to_pharmacy": {"name": "Local Pharmacy", "phone": "+15551234567"}},
    )
    assert response.status_code == 401
    assert ProposedAction.query.filter_by(tenant_id=PRIVATE_TENANT).count() == before


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
