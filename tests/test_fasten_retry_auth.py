"""POST /fasten/jobs/<task_id>/retry needs a write grant for the tenant (#912).

A retry relaunches a full ingest into the tenant from the job's stored
download links, so it is a write. It now needs what every other engine write
needs: a step-up token bound to the tenant named in X-Tenant-Id. The tenant
header alone no longer authorizes it, and a successful retry is audited.
Synthetic tenant, connection and task ids only.
"""

import json
from unittest.mock import patch

from models import db
from r6.fasten.models import FastenJob
from r6.models import AuditEventRecord
from r6.stepup import generate_step_up_token

OWNER = "retry-auth-owner"
OTHER = "retry-auth-other"


def _post_webhook(client, payload):
    with patch("r6.fasten.routes.verify_webhook", return_value=True):
        return client.post("/fasten/webhook", data=json.dumps(payload),
                           content_type="application/json")


def _failed_job(client, tenant, org, task):
    """A registered connection and an export that failed after its links
    were stored: the case the retry route exists for."""
    r = client.post("/fasten/connections", headers={"X-Tenant-Id": tenant},
                    json={"org_connection_id": org})
    assert r.status_code in (200, 201), r.get_json()
    with patch("r6.fasten.routes.threading.Thread"):
        _post_webhook(client, {
            "api_mode": "live", "type": "patient.ehi_export_success",
            "date": "2026-10-16T12:00:00Z", "id": f"evt-{task}",
            "data": {"org_connection_id": org, "task_id": task,
                     "download_links": ["https://example.com/retry.ndjson"]}})
    job = FastenJob.query.filter_by(task_id=task).first()
    job.status = "failed"
    db.session.commit()


def _retry(client, task, headers):
    with patch("r6.fasten.routes.threading.Thread") as thread:
        r = client.post(f"/fasten/jobs/{task}/retry", headers=headers)
    return r, thread


def _status(task):
    db.session.expire_all()
    return FastenJob.query.filter_by(task_id=task).one().status


def test_an_unauthenticated_retry_is_refused(client):
    _failed_job(client, OWNER, "oc-retry-auth-1", "task-retry-auth-1")

    r, thread = _retry(client, "task-retry-auth-1", {"X-Tenant-Id": OWNER})

    assert r.status_code == 401
    assert not thread.called
    assert _status("task-retry-auth-1") == "failed"


def test_another_accounts_token_cannot_retry_the_owners_job(client):
    """Naming the owner's tenant with a token for a different tenant."""
    _failed_job(client, OWNER, "oc-retry-auth-2", "task-retry-auth-2")

    r, thread = _retry(client, "task-retry-auth-2", {
        "X-Tenant-Id": OWNER,
        "X-Step-Up-Token": generate_step_up_token(OTHER)})

    assert r.status_code == 401
    assert not thread.called
    assert _status("task-retry-auth-2") == "failed"


def test_the_owners_read_token_cannot_retry(client):
    """A retry is a write; the read token CareAgents mints for chat is not
    enough."""
    _failed_job(client, OWNER, "oc-retry-auth-5", "task-retry-auth-5")

    r, thread = _retry(client, "task-retry-auth-5", {
        "X-Tenant-Id": OWNER,
        "X-Step-Up-Token": generate_step_up_token(OWNER, scope="read")})

    assert r.status_code == 401
    assert not thread.called
    assert _status("task-retry-auth-5") == "failed"


def test_another_account_cannot_reach_the_owners_job_from_its_own_tenant(
        client):
    """A fully authorized caller for its own tenant still cannot see a job
    that belongs to someone else."""
    _failed_job(client, OWNER, "oc-retry-auth-3", "task-retry-auth-3")

    r, thread = _retry(client, "task-retry-auth-3", {
        "X-Tenant-Id": OTHER,
        "X-Step-Up-Token": generate_step_up_token(OTHER)})

    assert r.status_code == 404
    assert not thread.called
    assert _status("task-retry-auth-3") == "failed"


def test_the_owner_can_retry_their_own_job_and_it_is_audited(client):
    _failed_job(client, OWNER, "oc-retry-auth-4", "task-retry-auth-4")
    before = AuditEventRecord.query.filter_by(
        tenant_id=OWNER, event_type="fasten_import_retried").count()

    r, thread = _retry(client, "task-retry-auth-4", {
        "X-Tenant-Id": OWNER,
        "X-Step-Up-Token": generate_step_up_token(OWNER)})

    assert r.status_code == 202, r.get_json()
    assert thread.called
    assert _status("task-retry-auth-4") == "pending"
    rows = AuditEventRecord.query.filter_by(
        tenant_id=OWNER, event_type="fasten_import_retried").all()
    assert len(rows) == before + 1
    assert rows[-1].outcome == "success"
