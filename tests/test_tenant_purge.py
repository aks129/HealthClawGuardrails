"""Tenant deletion — "delete my records" must actually delete them (#173).

The bar the beta-tester guide sets: "if deleting is ever harder than
connecting was, that's a bug." These tests hold the mechanism to it —
including the negative test that matters most: after deletion the resources
are UNREADABLE through the API, not merely unlinked from an account.
"""

import json
from datetime import datetime, timedelta, timezone

from models import db
from r6.models import AuditEventRecord, ContextEnvelope, ContextItem, R6Resource
from r6.purge import purge_summary, purge_tenant


def _seed_resource(tenant, rid="purge-me-1", rtype="Observation"):
    r = R6Resource(resource_type=rtype,
                   resource_json=json.dumps({"resourceType": rtype, "id": rid}),
                   resource_id=rid, tenant_id=tenant)
    db.session.add(r)
    db.session.commit()
    return r


def test_purge_removes_clinical_resources(client, tenant_id):
    _seed_resource(tenant_id)
    assert R6Resource.query.filter_by(tenant_id=tenant_id).count() >= 1

    deleted = purge_tenant(tenant_id)
    db.session.commit()

    assert R6Resource.query.filter_by(tenant_id=tenant_id).count() == 0
    assert purge_summary(deleted) >= 1


def test_purge_retains_the_audit_trail(client, tenant_id):
    # Audit is the immutable record of prior access and is PHI-free by
    # contract; a deletion must not be able to erase evidence of the access
    # that preceded it.
    db.session.add(AuditEventRecord(event_type="read", resource_type="Patient",
                                    resource_id="p-1", tenant_id=tenant_id,
                                    agent_id="test", outcome="success"))
    db.session.commit()
    before = AuditEventRecord.query.filter_by(tenant_id=tenant_id).count()
    assert before >= 1

    purge_tenant(tenant_id)
    db.session.commit()

    assert AuditEventRecord.query.filter_by(tenant_id=tenant_id).count() >= before


def test_purge_takes_context_items_with_their_envelopes(client, tenant_id):
    # Items link by context_id, not tenant — purging only by tenant would
    # orphan them and leave resource references behind.
    now = datetime.now(timezone.utc)
    env = ContextEnvelope(context_id="ctx-purge-1", tenant_id=tenant_id,
                          patient_ref="Patient/p-1",
                          expires_at=now + timedelta(days=1))
    db.session.add(env)
    db.session.flush()
    db.session.add(ContextItem(context_id="ctx-purge-1",
                               resource_ref="Observation/o-1",
                               slice_name="labs"))
    db.session.commit()

    purge_tenant(tenant_id)
    db.session.commit()

    assert ContextEnvelope.query.filter_by(tenant_id=tenant_id).count() == 0
    assert ContextItem.query.filter_by(context_id="ctx-purge-1").count() == 0


def test_purge_leaves_other_tenants_untouched(client, tenant_id):
    _seed_resource(tenant_id, rid="mine-1")
    _seed_resource("someone-elses-tenant", rid="theirs-1")

    purge_tenant(tenant_id)
    db.session.commit()

    assert R6Resource.query.filter_by(tenant_id=tenant_id).count() == 0
    assert R6Resource.query.filter_by(
        tenant_id="someone-elses-tenant").count() == 1


def test_purged_records_are_unreadable_through_the_api(client, tenant_id,
                                                       tenant_headers):
    # The negative test #173 asks for: gone from the API surface, not just
    # unlinked from an account.
    _seed_resource(tenant_id, rid="api-visible-1")
    before = client.get("/r6/fhir/Observation", headers=tenant_headers)
    assert before.status_code == 200
    assert before.get_json().get("total", 0) >= 1

    purge_tenant(tenant_id)
    db.session.commit()

    after = client.get("/r6/fhir/Observation", headers=tenant_headers)
    assert after.status_code == 200
    assert after.get_json().get("total", 0) == 0

    single = client.get("/r6/fhir/Observation/api-visible-1",
                        headers=tenant_headers)
    assert single.status_code == 404


def test_purge_endpoint_refuses_non_public_tenants_without_the_secret(
        client, monkeypatch):
    # Deletion is at least as sensitive as creation, so it rides the same
    # fail-closed gate: with a mint secret configured, a non-public tenant
    # cannot be purged without presenting it.
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "s3cret")
    denied = client.post("/r6/fhir/internal/purge-tenant",
                         json={"tenant_id": "someones-real-tenant"})
    assert denied.status_code == 403

    allowed = client.post("/r6/fhir/internal/purge-tenant",
                          json={"tenant_id": "someones-real-tenant"},
                          headers={"X-Internal-Secret": "s3cret"})
    assert allowed.status_code == 200
    assert allowed.get_json()["deleted"] is True


def test_purge_endpoint_needs_a_tenant(client):
    resp = client.post("/r6/fhir/internal/purge-tenant", json={})
    assert resp.status_code == 400
