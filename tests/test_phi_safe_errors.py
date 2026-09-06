"""Canary tests ensuring operational failures never persist or log PHI."""

import logging
from unittest.mock import patch

from models import db
from r6.fasten.ingester import stream_ingest
from r6.fasten.models import FastenJob
from r6.fhir_proxy import FHIRUpstreamProxy


def test_fasten_failure_records_category_without_exception_text(app, caplog):
    """The failure log names a category, never the exception text, at DEBUG or above.

    MUTATION: r6/fasten/ingester.py:354 ->
    `logger.warning('Fasten job %d failed: %s', job_id, exc)` — red on the
    canary. Capturing at ERROR (this test before #634 F2) raised the floor
    above the root logger's default of WARNING, so the identical leak one
    level quieter was invisible. Deleting the at_level line outright would
    have caught it: main.py's basicConfig is a no-op under pytest, which
    already owns root's handlers, so WARNING records reach caplog by default.
    MUTATION: keep the sanitized error and add
    `logger.warning('Fasten job %d detail: %s', job_id, exc)` — red on the
    canary. A second, chattier record is the shape the non-vacuity assertion
    alone cannot see.
    MUTATION: delete the logger.error call — red on the sanitized-line
    assertion. Without it the canary passes vacuously whenever the record
    never reaches caplog at all, which is how the sibling below stayed
    honest and this one did not.
    """
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
        ), caplog.at_level(logging.DEBUG):
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
        # Non-vacuity: the sanitized line was logged AND caplog saw it, so the
        # canary check above cannot be passing against an empty capture.
        assert f"Fasten job {job_id} failed: RuntimeError" in caplog.text


def test_upstream_proxy_logs_only_sanitized_failure_categories(caplog):
    url_canary = "https://example.invalid/fhir?access_token=url-secret"
    error_canary = "Patient/secret-123?access_token=error-secret"

    with caplog.at_level(logging.INFO):
        proxy = FHIRUpstreamProxy(url_canary)
        with patch.object(
            proxy._client,
            "get",
            side_effect=RuntimeError(error_canary),
        ):
            _body, status = proxy.operation("Patient/secret-123")
        proxy.close()

    assert status == 502
    assert "url-secret" not in caplog.text
    assert "error-secret" not in caplog.text
    assert "Patient/secret-123" not in caplog.text
    assert "RuntimeError" in caplog.text
