"""Ingest job recovery — a redeploy/crash mid-ingest must not strand records.

Found live 2026-07-08: the ingest runs in a daemon thread; a Railway redeploy
killed the process mid-run, leaving the job at status='ingesting' forever. The
webhook idempotency check then skipped any task that already had a job row, so
a replay could not recover it. Fix: persist download_links on the job, let the
webhook reprocess a non-complete job, and expose an explicit retry endpoint.
"""

import json
from unittest.mock import patch

from r6.fasten.models import FastenConnection, FastenJob
from models import db


def _register(client, tenant, org_id):
    return client.post("/fasten/connections",
                       headers={"X-Tenant-Id": tenant,
                                "Content-Type": "application/json"},
                       data=json.dumps({"org_connection_id": org_id}))


def _export_webhook(client, org_id, task_id):
    payload = {"type": "patient.ehi_export_success",
               "data": {"org_connection_id": org_id, "task_id": task_id,
                        "download_links": [
                            {"content_type": "application/fhir+ndjson",
                             "export_type": "jsonl",
                             "url": "https://example.com/e.jsonl"}]}}
    with patch("r6.fasten.routes.verify_webhook", return_value=True), \
         patch("r6.fasten.routes.threading.Thread") as t:
        r = client.post("/fasten/webhook", data=json.dumps(payload),
                        content_type="application/json")
    return r, t


class TestReprocessNonCompleteJob:
    def test_complete_job_is_not_reprocessed(self, client):
        _register(client, "rc-t1", "oc-rc-1")
        _export_webhook(client, "oc-rc-1", "task-rc-1")
        job = FastenJob.query.filter_by(task_id="task-rc-1").first()
        job.status = "complete"
        db.session.commit()
        r, t = _export_webhook(client, "oc-rc-1", "task-rc-1")
        assert r.status_code == 200
        assert not t.called  # idempotent: no re-run of a finished job

    def test_stuck_ingesting_job_is_reprocessed(self, client):
        _register(client, "rc-t2", "oc-rc-2")
        _export_webhook(client, "oc-rc-2", "task-rc-2")
        job = FastenJob.query.filter_by(task_id="task-rc-2").first()
        job.status = "ingesting"          # zombie from a killed thread
        job.ingested_resources = 12
        db.session.commit()
        r, t = _export_webhook(client, "oc-rc-2", "task-rc-2")
        assert r.status_code == 200
        assert t.called                    # re-run
        refreshed = FastenJob.query.filter_by(task_id="task-rc-2").first()
        assert refreshed.ingested_resources == 0   # counters reset for the re-run

    def test_download_links_persisted_on_job(self, client):
        _register(client, "rc-t3", "oc-rc-3")
        _export_webhook(client, "oc-rc-3", "task-rc-3")
        job = FastenJob.query.filter_by(task_id="task-rc-3").first()
        assert job.download_links_json
        assert json.loads(job.download_links_json) == ["https://example.com/e.jsonl"]


class TestRetryEndpoint:
    def test_retry_reruns_a_stuck_job(self, client):
        _register(client, "rc-t4", "oc-rc-4")
        _export_webhook(client, "oc-rc-4", "task-rc-4")
        FastenJob.query.filter_by(task_id="task-rc-4").first().status = "ingesting"
        db.session.commit()
        with patch("r6.fasten.routes.threading.Thread") as t:
            r = client.post("/fasten/jobs/task-rc-4/retry",
                            headers={"X-Tenant-Id": "rc-t4"})
        assert r.status_code == 202
        assert t.called

    def test_retry_requires_matching_tenant(self, client):
        _register(client, "rc-t5", "oc-rc-5")
        _export_webhook(client, "oc-rc-5", "task-rc-5")
        with patch("r6.fasten.routes.threading.Thread") as t:
            r = client.post("/fasten/jobs/task-rc-5/retry",
                            headers={"X-Tenant-Id": "someone-else"})
        assert r.status_code == 404
        assert not t.called

    def test_retry_refuses_complete_job(self, client):
        _register(client, "rc-t6", "oc-rc-6")
        _export_webhook(client, "oc-rc-6", "task-rc-6")
        FastenJob.query.filter_by(task_id="task-rc-6").first().status = "complete"
        db.session.commit()
        with patch("r6.fasten.routes.threading.Thread") as t:
            r = client.post("/fasten/jobs/task-rc-6/retry",
                            headers={"X-Tenant-Id": "rc-t6"})
        assert r.status_code == 409
        assert not t.called

    def test_retry_unknown_task_404(self, client):
        r = client.post("/fasten/jobs/no-such-task/retry",
                        headers={"X-Tenant-Id": "rc-t7"})
        assert r.status_code == 404
