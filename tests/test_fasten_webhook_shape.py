"""Fasten webhook payload shape — fields are nested under `data`.

Ground truth from a live delivery (2026-07-07, portal delivery log): the
envelope is {api_mode, type, date, id, data: {...}} — org_connection_id,
external_id, task_id, download_links all live INSIDE `data`. The handlers
originally read them from the top level, silently dropping every event with
a 200. These tests pin the real shape.
"""

import json
from unittest.mock import patch

from r6.fasten.models import FastenConnection, FastenJob


def _post_webhook(client, payload):
    # verify_webhook is signature-checked against the Fasten portal secret;
    # bypass it here — shape handling is what's under test.
    with patch("r6.fasten.routes.verify_webhook", return_value=True):
        return client.post("/fasten/webhook",
                           data=json.dumps(payload),
                           content_type="application/json")


def _envelope(event_type, data):
    return {"api_mode": "live", "type": event_type,
            "date": "2026-07-07T23:31:09Z", "id": "evt-1", "data": data}


def test_connection_success_nested_data_registers(client, app_ctx=None):
    payload = _envelope("patient.connection_success", {
        "org_connection_id": "oc-nested-1",
        "external_id": "nested-tenant-1",
        "endpoint_id": "ep-1", "brand_id": "b-1", "portal_id": "p-1",
        "platform_type": "epic", "connection_status": "authorized",
    })
    resp = _post_webhook(client, payload)
    assert resp.status_code == 200
    conn = FastenConnection.query.filter_by(org_connection_id="oc-nested-1").first()
    assert conn is not None, "nested connection_success payload was dropped"
    assert conn.tenant_id == "nested-tenant-1"


def test_export_success_nested_data_creates_job(client):
    # Register the connection first (as the widget/page flow does)
    client.post("/fasten/connections",
                headers={"X-Tenant-Id": "nested-tenant-2",
                         "Content-Type": "application/json"},
                data=json.dumps({"org_connection_id": "oc-nested-2"}))

    payload = _envelope("patient.ehi_export_success", {
        "org_connection_id": "oc-nested-2",
        "task_id": "task-nested-2",
        "download_links": ["https://example.com/export.ndjson"],
    })
    # stream_ingest runs in a thread against real URLs — stub it out
    with patch("r6.fasten.routes.threading.Thread") as t:
        resp = _post_webhook(client, payload)
    assert resp.status_code == 200
    job = FastenJob.query.filter_by(task_id="task-nested-2").first()
    assert job is not None, "nested export_success payload was dropped"
    assert job.tenant_id == "nested-tenant-2"
    assert t.called, "ingest thread was never started"


def test_flat_payloads_still_work(client):
    # Back-compat: a flat payload (fields at top level) keeps working.
    client.post("/fasten/connections",
                headers={"X-Tenant-Id": "flat-tenant",
                         "Content-Type": "application/json"},
                data=json.dumps({"org_connection_id": "oc-flat-1"}))
    payload = {"type": "patient.ehi_export_success",
               "org_connection_id": "oc-flat-1",
               "task_id": "task-flat-1",
               "download_links": ["https://example.com/export.ndjson"]}
    with patch("r6.fasten.routes.threading.Thread"):
        resp = _post_webhook(client, payload)
    assert resp.status_code == 200
    assert FastenJob.query.filter_by(task_id="task-flat-1").first() is not None


def test_download_links_as_objects_are_normalized_to_urls(client):
    # Real payloads carry download_links as [{content_type, export_type, url}]
    # (live delivery 2026-07-08) — the ingest thread must receive URL strings.
    client.post("/fasten/connections",
                headers={"X-Tenant-Id": "dl-shape-tenant",
                         "Content-Type": "application/json"},
                data=json.dumps({"org_connection_id": "oc-dl-1"}))
    payload = _envelope("patient.ehi_export_success", {
        "org_connection_id": "oc-dl-1",
        "task_id": "task-dl-1",
        "download_links": [
            {"content_type": "application/fhir+ndjson",
             "export_type": "jsonl",
             "url": "https://example.com/export.jsonl"}],
    })
    with patch("r6.fasten.routes.threading.Thread") as t:
        resp = _post_webhook(client, payload)
    assert resp.status_code == 200
    assert FastenJob.query.filter_by(task_id="task-dl-1").first() is not None
    # thread args: (app, job_id, download_links, tenant_id)
    links = t.call_args.kwargs.get("args", t.call_args[1].get("args"))[2]
    assert links == ["https://example.com/export.jsonl"]
