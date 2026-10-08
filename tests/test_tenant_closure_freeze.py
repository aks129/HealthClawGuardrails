"""A closed tenant takes no new records, from any writer (CTO ruling on #913).

The freeze began at the FHIR create/update and ingest routes. These are the
writers that still bypassed it: an SMBP reading, the SMBP report's filed
DocumentReference, a reviewed form's QuestionnaireResponse, and an
action-rail action approved before the disconnect and executed after it.

What must stay open on a closed tenant: reads (with their audit rows),
and purge, so Delete after Disconnect still deletes.

Synthetic tenant ids and records only.
"""

import json

import pytest

from models import db
from r6.actions.models import ProposedAction
from r6.fasten.models import TenantClosure, tenant_closed
from r6.models import AuditEventRecord, R6Resource
from tests.approval_helpers import approval_headers

SECRET = "freeze-test-internal-secret"


def _close(tenant):
    db.session.add(TenantClosure(tenant_id=tenant))
    db.session.commit()


def _refusals(tenant):
    return [a for a in AuditEventRecord.query.filter_by(
        tenant_id=tenant, outcome="failure").all()
        if (a.detail or "").endswith("tenant disconnected")]


# --- SMBP --------------------------------------------------------------------

def test_an_smbp_reading_into_a_closed_tenant_is_refused(
        client, tenant_id, auth_headers):
    """MUTATION: drop require_open_tenant from smbp reading -> 201."""
    _close(tenant_id)
    r = client.post("/r6/smbp/reading", headers=auth_headers,
                    json={"systolic": 128, "diastolic": 82,
                          "patient_ref": "Patient/freeze-pt",
                          "effective": "2026-10-08T08:00:00Z"})
    assert r.status_code == 409
    assert R6Resource.query.filter_by(tenant_id=tenant_id).count() == 0
    assert len(_refusals(tenant_id)) == 1


def test_the_smbp_report_still_reads_but_files_nothing(
        client, tenant_id, auth_headers):
    """The report is a read and stays open; the DocumentReference it would
    file is a new record, so it is skipped.

    MUTATION: drop the tenant_closed check before
    _persist_document_reference -> a DocumentReference here."""
    enroll = client.post("/r6/smbp/enroll", headers=auth_headers,
                         json={"patient_ref": "Patient/freeze-pt"})
    assert enroll.status_code == 201
    _close(tenant_id)
    r = client.get("/r6/smbp/report/%s?format=pdf" % enroll.get_json()["id"],
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type="DocumentReference").count() == 0


# --- the action rail ---------------------------------------------------------

RID = "freeze-cond-1"


def _condition():
    return {"resourceType": "Condition", "id": RID,
            "subject": {"reference": "Patient/freeze-pt"},
            "code": {"coding": [{"system": "http://snomed.info/sct",
                                 "code": "44054006"}]},
            "clinicalStatus": {"coding": [{"code": "active"}]}}


@pytest.fixture
def awaiting_fix(app, client, tenant_id, tenant_headers, auth_headers,
                 action_registry, monkeypatch):
    """A curatr-fix action committed and awaiting the person's Approve."""
    monkeypatch.setenv("CURATR_FIX_RAIL_ENABLED", "1")
    db.session.add(R6Resource("Condition", json.dumps(_condition()),
                              resource_id=RID, tenant_id=tenant_id))
    db.session.commit()
    body = {"kind": "curatr-fix",
            "payload": {"to": "Condition/%s" % RID,
                        "body": "Mark this condition as resolved.",
                        "curatr_fix": {
                            "resource_type": "Condition", "resource_id": RID,
                            "record_version": 1,
                            "patient_intent": "it cleared up",
                            "fixes": [{"field_path":
                                       "Condition.clinicalStatus.coding[0].code",
                                       "new_value": "resolved"}]}}}
    r = client.post("/r6/actions/propose", json=body, headers=tenant_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    action_id = r.get_json()["id"]
    assert client.post("/r6/actions/%s/commit" % action_id,
                       headers=auth_headers).status_code == 202
    return action_id


def test_an_action_approved_before_disconnect_does_not_execute_after(
        app, client, tenant_id, auth_headers, awaiting_fix):
    """The Approve credential was minted while the tenant was open; the
    tenant closes; the tap lands. Nothing is claimed and nothing changes.

    MUTATION: drop require_open_tenant from confirm_action -> 200 and the
    condition marked resolved."""
    headers = approval_headers(app, auth_headers, awaiting_fix)
    _close(tenant_id)
    r = client.post("/r6/actions/%s/confirm" % awaiting_fix, json={},
                    headers=headers)
    assert r.status_code == 409
    db.session.expire_all()
    row = R6Resource.query.filter_by(resource_type="Condition", id=RID,
                                     tenant_id=tenant_id).one()
    assert json.loads(row.resource_json) == _condition()
    assert R6Resource.query.filter_by(tenant_id=tenant_id,
                                      resource_type="Provenance").count() == 0
    assert db.session.get(ProposedAction, awaiting_fix).status \
        == "awaiting_confirmation"
    assert len(_refusals(tenant_id)) == 1


def test_a_review_submitted_after_disconnect_writes_nothing(
        client, tenant_id, auth_headers):
    """MUTATION: drop require_open_tenant from review_submit -> the
    unknown-action 404 instead (the check runs before any load)."""
    _close(tenant_id)
    r = client.post("/r6/actions/no-such-action/review", json={},
                    headers=auth_headers)
    assert r.status_code == 409
    assert R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type="QuestionnaireResponse").count() == 0


# --- what stays open ---------------------------------------------------------

def test_a_closed_tenant_can_still_be_read_and_the_read_is_audited(
        client, tenant_id, auth_headers):
    db.session.add(R6Resource("Condition", json.dumps(_condition()),
                              resource_id=RID, tenant_id=tenant_id))
    db.session.commit()
    _close(tenant_id)
    before = AuditEventRecord.query.filter_by(tenant_id=tenant_id).count()
    r = client.get("/r6/fhir/Condition/%s" % RID, headers=auth_headers)
    assert r.status_code == 200
    assert AuditEventRecord.query.filter_by(
        tenant_id=tenant_id).count() > before


def test_delete_after_disconnect_still_purges(client, monkeypatch):
    """Disconnect, then Delete: the purge is not a new record and must run.

    MUTATION: add require_open_tenant to purge_tenant_route -> 409."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    tenant = "freeze-private-tenant"
    db.session.add(R6Resource("Condition", json.dumps(_condition()),
                              resource_id=RID, tenant_id=tenant))
    db.session.commit()
    headers = {"X-Internal-Secret": SECRET}
    assert client.post("/r6/fhir/internal/fasten-revoke",
                       json={"tenant_id": tenant},
                       headers=headers).status_code == 200
    r = client.post("/r6/fhir/internal/purge-tenant",
                    json={"tenant_id": tenant}, headers=headers)
    assert r.status_code == 200
    assert r.get_json()["deleted"] is True
    assert R6Resource.query.filter_by(tenant_id=tenant).count() == 0
    assert tenant_closed(tenant)


# --- the revoke endpoint refuses a public tenant ----------------------------

def test_revoke_refuses_a_public_tenant_and_writes_nothing(client,
                                                           monkeypatch):
    """Matching purge: a public tenant is never closed.

    MUTATION: drop the is_public check in fasten_revoke_route -> 200 and a
    closure row."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    before = AuditEventRecord.query.filter_by(tenant_id="test-tenant").count()
    r = client.post("/r6/fhir/internal/fasten-revoke",
                    json={"tenant_id": "test-tenant"},
                    headers={"X-Internal-Secret": SECRET})
    assert r.status_code == 422
    assert not tenant_closed("test-tenant")
    assert AuditEventRecord.query.filter_by(
        tenant_id="test-tenant").count() == before
