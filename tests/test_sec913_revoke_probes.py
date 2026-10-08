"""Security review of #913: attacks on POST /internal/fasten-revoke and the
tenant tombstone.

Rows V1, V2, V3 and V6 of docs/qa/sign-off-standard.md. The PR's own tests
fake `tenant_revoked` inside the ingest thread and fake the HealthClaw client
on the CareAgents side; these probes use a tombstone committed through a
separate connection and the real client. Synthetic tenant, connection and
task ids only.

A plain test is a property that holds. An xfail(strict=True) row is a gap
this review found; a fix flips it red so the marker comes off.
"""

import json
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from models import db
from r6.fasten.models import (FastenConnection, FastenJob,
                              FastenTenantRevocation, tenant_revoked)
from r6.models import AuditEventRecord, R6Resource

SECRET = "sec913-internal-secret"
VICTIM = "sec913-victim"
ATTACKER = "sec913-attacker"
REVOKE = "/r6/fhir/internal/fasten-revoke"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    return SECRET


def _revoke(client, tenant=VICTIM, headers=None):
    h = {"X-Internal-Secret": SECRET}
    h.update(headers or {})
    return client.post(REVOKE, json={"tenant_id": tenant}, headers=h)


def _connection(tenant=VICTIM, org="oc-sec913-1"):
    db.session.add(FastenConnection(org_connection_id=org, tenant_id=tenant,
                                    connection_status="authorized"))
    db.session.commit()


# --- the endpoint's gate ------------------------------------------------------

def test_header_tenant_is_ignored_body_tenant_is_revoked(client, secret):
    """By design the secret is infrastructure-wide, so the body names the
    tenant. Pinned so a later change cannot start revoking the header's."""
    _connection(tenant=ATTACKER, org="oc-sec913-att")
    r = _revoke(client, tenant=VICTIM, headers={"X-Tenant-Id": ATTACKER})
    assert r.status_code == 200
    assert tenant_revoked(VICTIM)
    assert not tenant_revoked(ATTACKER)
    assert db.session.get(FastenConnection,
                          "oc-sec913-att").connection_status == "authorized"


def test_production_without_a_configured_secret_refuses(client, monkeypatch):
    """Fail closed when INTERNAL_TOKEN_MINT_SECRET is unset in production."""
    monkeypatch.delenv("INTERNAL_TOKEN_MINT_SECRET", raising=False)
    monkeypatch.setattr("r6.internal_auth.resolve_app_env",
                        lambda: "production")
    r = client.post(REVOKE, json={"tenant_id": VICTIM})
    assert r.status_code == 403
    assert not tenant_revoked(VICTIM)


def test_secret_is_checked_before_the_body(client, secret):
    r = client.post(REVOKE, data=b"{not json", content_type="application/json")
    assert r.status_code == 403


@pytest.mark.parametrize("body", [[], "victim", {"tenant": VICTIM},
                                  {"tenant_id": [VICTIM]},
                                  {"tenant_id": {"id": VICTIM}}])
def test_odd_bodies_are_400_not_500(client, secret, body):
    r = client.post(REVOKE, json=body, headers={"X-Internal-Secret": SECRET})
    assert r.status_code == 400
    assert "Traceback" not in r.get_data(as_text=True)
    assert FastenTenantRevocation.query.count() == 0


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
def test_no_verb_on_the_route_clears_a_tombstone(client, secret, method):
    assert _revoke(client).status_code == 200
    r = client.open(REVOKE, method=method, json={"tenant_id": VICTIM,
                                                 "revoked": False},
                    headers={"X-Internal-Secret": SECRET})
    assert r.status_code == 405
    assert tenant_revoked(VICTIM)


def test_reregistering_or_purging_does_not_clear_the_tombstone(client,
                                                               secret):
    assert _revoke(client).status_code == 200
    r = client.post("/fasten/connections",
                    headers={"X-Tenant-Id": VICTIM},
                    json={"org_connection_id": "oc-sec913-new",
                          "connection_status": "authorized"})
    assert r.status_code == 409
    from r6.purge import purge_tenant
    purge_tenant(VICTIM)
    db.session.commit()
    assert tenant_revoked(VICTIM)


# --- fail closed on a failed audit write -------------------------------------

def test_an_operational_audit_failure_revokes_nothing_and_says_so(client,
                                                                  secret):
    _connection()
    with patch("r6.fasten.revoke.add_audit_event",
               side_effect=OperationalError("INSERT", {}, Exception("down"))):
        r = _revoke(client)
    assert r.status_code == 500
    assert r.get_json()["revoked"] is False
    db.session.expire_all()
    assert not tenant_revoked(VICTIM)
    assert db.session.get(FastenConnection,
                          "oc-sec913-1").connection_status == "authorized"


def test_an_integrity_error_that_is_not_the_tombstone_is_not_a_revoke(
        client, secret):
    """An IntegrityError raised by the audit insert (or any statement other
    than the tombstone insert) rolls back the tombstone, the row flips and
    the audit row, and the route still answers 200 revoked:true. CareAgents
    then marks the connection disconnected while the engine holds nothing."""
    _connection()
    with patch("r6.fasten.revoke.add_audit_event",
               side_effect=IntegrityError("INSERT INTO audit_event", {},
                                          Exception("not null"))):
        r = _revoke(client)
    db.session.expire_all()
    revoked_claim = r.status_code == 200 and r.get_json().get("revoked")
    assert not (revoked_claim and not tenant_revoked(VICTIM)), (
        "200 revoked:true with no tombstone, connection still "
        + db.session.get(FastenConnection, "oc-sec913-1").connection_status)


# --- an ingest that is running sees a tombstone committed elsewhere ----------

class _Stream:
    def __init__(self, lines, at, hook):
        self._lines, self._at, self._hook = lines, at, hook

    def raise_for_status(self):
        return None

    def iter_lines(self):
        for i, line in enumerate(self._lines, start=1):
            if i == self._at:
                self._hook()
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_running_ingest_sees_a_tombstone_from_another_connection(
        monkeypatch, tmp_path):
    """No fake tenant_revoked: the tombstone is INSERTed and committed on a
    separate DB connection while the ingest thread's session is mid-export,
    after the first progress commit. File-backed SQLite, so the two
    connections really are two."""
    from main import create_app
    from r6.fasten import ingester

    uri = f"sqlite:///{tmp_path / 'sec913.db'}"
    monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", uri)
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri,
                      "LEGACY_BOOT_ON_CREATE": False})
    with app.app_context():
        db.create_all()
        db.session.add(FastenJob(task_id="task-sec913", tenant_id=VICTIM,
                                 org_connection_id="oc-sec913-1",
                                 status="pending"))
        db.session.add(FastenConnection(org_connection_id="oc-sec913-1",
                                        tenant_id=VICTIM,
                                        connection_status="authorized"))
        db.session.commit()
        job_id = FastenJob.query.one().id
        engine = db.engine

    def tombstone_elsewhere():
        with engine.connect() as other:
            other.execute(text(
                "INSERT INTO fasten_tenant_revocations (tenant_id, revoked_at)"
                " VALUES (:t, CURRENT_TIMESTAMP)"), {"t": VICTIM})
            other.commit()

    lines = [json.dumps({"resourceType": "Observation", "id": f"obs-{i}",
                         "status": "final",
                         "code": {"coding": [{"system": "http://loinc.org",
                                              "code": "8867-4"}]}})
             for i in range(25)]
    monkeypatch.setattr(ingester.httpx, "stream",
                        lambda *a, **k: _Stream(lines, 11, tombstone_elsewhere))
    ingester.stream_ingest(app, job_id, ["https://example.invalid/e.ndjson"],
                           VICTIM)
    with app.app_context():
        db.session.expire_all()
        kept = R6Resource.query.filter_by(tenant_id=VICTIM).count()
        job = db.session.get(FastenJob, job_id)
        assert kept == 10, f"{kept} resources kept after the tombstone"
        assert job.status == "failed" and job.failure_reason == "disconnected"
        details = [a.detail for a in AuditEventRecord.query.filter_by(
            event_type="fasten_import_refused").all()]
        assert details == ["job=task-sec913 stopped: disconnected"]
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


# --- ways in that do not ask the tombstone (outside the PR's claim) ----------

def test_ingest_bundle_into_a_purged_tenant_is_refused(client, secret):
    from r6.purge import purge_tenant
    purge_tenant(VICTIM)
    db.session.commit()
    assert tenant_revoked(VICTIM)
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Observation", "id": "obs-refill",
                      "status": "final",
                      "code": {"coding": [{"system": "http://loinc.org",
                                           "code": "8867-4"}]}}}]}
    client.post("/r6/fhir/internal/ingest-bundle",
                json={"bundle": bundle},
                headers={"X-Tenant-Id": VICTIM, "X-Internal-Secret": SECRET})
    assert R6Resource.query.filter_by(tenant_id=VICTIM).count() == 0


def test_direct_fhir_write_into_a_revoked_tenant_is_refused(client, secret):
    from r6.stepup import generate_step_up_token
    token = generate_step_up_token(VICTIM)
    assert _revoke(client).status_code == 200
    client.post("/r6/fhir/Observation",
                json={"resourceType": "Observation", "status": "final",
                      "code": {"coding": [{"system": "http://loinc.org",
                                           "code": "8867-4"}]}},
                headers={"X-Tenant-Id": VICTIM, "X-Step-Up-Token": token,
                         "X-Human-Confirmed": "true",
                         "Content-Type": "application/fhir+json"})
    assert R6Resource.query.filter_by(tenant_id=VICTIM).count() == 0


def test_wearable_oauth_callback_after_revoke_brings_no_data(client, secret,
                                                             monkeypatch):
    """The callback refuses a revoked tenant (409, no row), and the poller
    skips the tenant regardless, so nothing is ingested. Pinned so neither
    check can quietly go.

    MUTATION: drop require_open_tenant from wearables oauth_callback -> 200
    and a WearableConnection row here."""
    from r6.wearables import poller
    from r6.wearables.models import WearableConnection
    from r6.wearables.routes import _sign_state
    import time

    assert _revoke(client).status_code == 200
    state = _sign_state({"tenant_id": VICTIM, "provider": "oura",
                         "ow_user_id": f"hc-{VICTIM}",
                         "exp": int(time.time()) + 300,
                         "iat": int(time.time())})
    r = client.get(f"/wearables/oauth/callback?state={state}")
    assert r.status_code == 409, r.get_data(as_text=True)[:200]
    assert WearableConnection.query.filter_by(tenant_id=VICTIM).count() == 0
    # A row that predates the revoke is still never polled.
    db.session.add(WearableConnection(tenant_id=VICTIM, provider="oura",
                                      ow_user_id=f"hc-{VICTIM}"))
    db.session.commit()

    class _WC:
        def enabled(self):
            return True

        def fetch_deltas(self, **_):
            raise AssertionError("poller fetched for a revoked tenant")

    out = poller.run_once(client.application, client=_WC())
    assert out["connections_checked"] == 0
    assert R6Resource.query.filter_by(tenant_id=VICTIM).count() == 0


# --- audit rows stay PHI-free ------------------------------------------------

def test_revoke_audit_rows_carry_no_payload_text(client, secret):
    _connection()
    db.session.add(FastenJob(task_id="task-sec913-a", tenant_id=VICTIM,
                             org_connection_id="oc-sec913-1",
                             status="ingesting"))
    db.session.commit()
    assert _revoke(client).status_code == 200
    rows = AuditEventRecord.query.filter_by(tenant_id=VICTIM).all()
    assert [r.event_type for r in rows] == ["fasten_connection_revoked"]
    assert rows[0].detail == "disconnected by account holder"
    assert rows[0].resource_id is None
