"""Canary tests ensuring operational failures never persist or log PHI."""

import logging
from unittest.mock import patch

from models import db
from r6.fasten.ingester import stream_ingest
from r6.fasten.models import FastenJob


def test_fasten_failure_records_category_without_exception_text(app, caplog):
    canary = "Patient/secret-123?access_token=do-not-log"
    with app.app_context():
        job = FastenJob(
            task_id="phi-safe-job",
            org_connection_id="phi-safe-connection",
            tenant_id="test-tenant",
            status="pending",
        )
        db.session.add(job)
        db.session.commit()
        job_id = job.id

        with patch(
            "r6.fasten.ingester.httpx.stream",
            side_effect=RuntimeError(canary),
        ), caplog.at_level(logging.ERROR):
            stream_ingest(
                app,
                job_id,
                ["https://download.example.invalid/export.ndjson"],
                "test-tenant",
            )

        db.session.expire_all()
        failed = db.session.get(FastenJob, job_id)
        assert failed.status == "failed"
        assert failed.failure_reason == "RuntimeError"
        assert canary not in caplog.text
