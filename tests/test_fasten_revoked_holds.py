"""A revoked Fasten connection takes no more exports.

`patient.authorization_revoked` marked the connection revoked, and a later
`patient.ehi_export_success` for the same org connection still opened a
job and ingested into the tenant (security review of #904, F2: nothing on
the engine side honoured a revocation). Synthetic ids only.
"""

import json
from unittest.mock import patch

from r6.fasten.models import FastenConnection, FastenJob


def _post_webhook(client, payload):
    with patch("r6.fasten.routes.verify_webhook", return_value=True):
        return client.post("/fasten/webhook", data=json.dumps(payload),
                           content_type="application/json")


def _envelope(event_type, data):
    return {"api_mode": "live", "type": event_type,
            "date": "2026-10-08T12:00:00Z", "id": "evt-r", "data": data}


def _register(client, tenant, org):
    client.post("/fasten/connections",
                headers={"X-Tenant-Id": tenant,
                         "Content-Type": "application/json"},
                data=json.dumps({"org_connection_id": org}))


def _export(client, org, task):
    with patch("r6.fasten.routes.threading.Thread") as t:
        resp = _post_webhook(client, _envelope("patient.ehi_export_success", {
            "org_connection_id": org, "task_id": task,
            "download_links": ["https://example.com/export.ndjson"]}))
    return resp, t


def test_export_after_revocation_opens_no_job(client):
    _register(client, "revoked-tenant", "oc-revoked-1")
    _post_webhook(client, _envelope("patient.authorization_revoked",
                                    {"org_connection_id": "oc-revoked-1"}))
    conn = FastenConnection.query.filter_by(
        org_connection_id="oc-revoked-1").first()
    assert conn.connection_status == "revoked"
    resp, thread = _export(client, "oc-revoked-1", "task-after-revoke")
    assert resp.status_code == 200          # acknowledged, so Fasten stops
    assert not thread.called, "ingest started on a revoked connection"
    assert FastenJob.query.filter_by(task_id="task-after-revoke").first() is None


def test_export_on_a_live_connection_still_ingests(client):
    _register(client, "live-tenant", "oc-live-1")
    resp, thread = _export(client, "oc-live-1", "task-live")
    assert resp.status_code == 200
    assert thread.called
    assert FastenJob.query.filter_by(task_id="task-live").first() is not None
