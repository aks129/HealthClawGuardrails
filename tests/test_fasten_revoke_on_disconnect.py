"""A CareAgents Disconnect tells HealthClaw, and HealthClaw holds it.

Disconnect used to flip only CareAgents' own row. The engine was never told,
so an old /connect/<tenant> link, a late patient.connection_success, a job
retry, the boot reaper or an ingest already running could still bring new
records into the tenant. Delete (purge) had the same hole.

POST /r6/fhir/internal/fasten-revoke writes a tenant tombstone, revokes the
tenant's Fasten connections and fails its unfinished jobs. Every path that
can import checks the tombstone. The tombstone, not the row flip, is what
covers a disconnect that lands before connection_success: there is no row
to flip yet, and the webhook would create a fresh authorized one.

Synthetic tenant and connection ids only.
"""

import json
import re
from unittest.mock import MagicMock, patch

import pytest

from models import db
from r6.fasten.models import (FastenConnection, FastenJob,
                              TenantClosure, tenant_closed)
from r6.models import AuditEventRecord, R6Resource

SECRET = "revoke-test-internal-secret"
TENANT = "revoke-tenant"            # not in PUBLIC_TENANTS
OTHER = "revoke-other-tenant"
REVOKE = "/r6/fhir/internal/fasten-revoke"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    return SECRET


def _revoke(client, tenant=TENANT, secret=SECRET):
    return client.post(REVOKE, json={"tenant_id": tenant},
                       headers={"X-Internal-Secret": secret})


def _webhook(client, event_type, data):
    payload = {"api_mode": "live", "type": event_type,
               "date": "2026-10-08T12:00:00Z", "id": "evt-rv", "data": data}
    with patch("r6.fasten.routes.verify_webhook", return_value=True), \
         patch("r6.fasten.routes.trigger_ehi_export") as trigger, \
         patch("r6.fasten.routes.threading.Thread") as thread:
        resp = client.post("/fasten/webhook", data=json.dumps(payload),
                           content_type="application/json")
    return resp, trigger, thread


def _connection(tenant=TENANT, org="oc-rv-1", status="authorized"):
    db.session.add(FastenConnection(org_connection_id=org, tenant_id=tenant,
                                    connection_status=status))
    db.session.commit()


def _job(tenant=TENANT, task="task-rv-1", status="ingesting", org="oc-rv-1"):
    db.session.add(FastenJob(task_id=task, org_connection_id=org,
                             tenant_id=tenant, status=status,
                             download_links_json=json.dumps(
                                 ["https://example.invalid/e.ndjson"])))
    db.session.commit()


def _tombstone(tenant=TENANT):
    db.session.add(TenantClosure(tenant_id=tenant))
    db.session.commit()


def _audits(event_type, tenant=TENANT):
    return AuditEventRecord.query.filter_by(event_type=event_type,
                                            tenant_id=tenant).all()


# --- the endpoint ------------------------------------------------------------

def test_revoke_needs_the_internal_secret(client, secret):
    """MUTATION: delete the `_internal_ingest_authorized` check in
    fasten_revoke_route -> 200 here, and a tombstone anyone could write."""
    assert _revoke(client, secret="wrong").status_code == 403
    assert client.post(REVOKE, json={"tenant_id": TENANT}).status_code == 403
    assert not tenant_closed(TENANT)


def test_revoke_has_no_public_tenant_exemption(client, secret):
    """test-tenant is public in the test env; the mint gate would wave it
    through. MUTATION: swap in `_internal_mint_authorized` -> 200 here."""
    resp = client.post(REVOKE, json={"tenant_id": "test-tenant"})
    assert resp.status_code == 403


@pytest.mark.parametrize("body", [{}, {"tenant_id": ""},
                                  {"tenant_id": "../../etc/passwd"},
                                  {"tenant_id": 42}])
def test_revoke_needs_a_well_formed_tenant(client, secret, body):
    resp = client.post(REVOKE, json=body,
                       headers={"X-Internal-Secret": SECRET})
    assert resp.status_code == 400
    assert TenantClosure.query.count() == 0


def test_revoke_tombstones_flips_rows_and_fails_jobs(client, secret):
    _connection(org="oc-rv-1")
    _connection(org="oc-rv-2")
    _connection(tenant=OTHER, org="oc-rv-other")
    _job(task="task-running", status="ingesting")
    _job(task="task-pending", status="pending")
    _job(task="task-done", status="complete")
    _job(tenant=OTHER, task="task-other", status="ingesting",
         org="oc-rv-other")

    resp = _revoke(client)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["already_revoked"] is False
    assert body["connections_revoked"] == 2
    assert body["jobs_stopped"] == 2

    assert tenant_closed(TENANT)
    assert {c.connection_status for c in FastenConnection.query.filter_by(
        tenant_id=TENANT)} == {"revoked"}
    jobs = {j.task_id: j for j in FastenJob.query.filter_by(tenant_id=TENANT)}
    for task in ("task-running", "task-pending"):
        assert jobs[task].status == "failed"
        assert jobs[task].failure_reason == "disconnected"
    assert jobs["task-done"].status == "complete"

    # Another tenant is untouched.
    assert not tenant_closed(OTHER)
    assert db.session.get(FastenConnection, "oc-rv-other").connection_status \
        == "authorized"
    assert FastenJob.query.filter_by(task_id="task-other").one().status \
        == "ingesting"

    rows = _audits("fasten_connection_revoked")
    assert len(rows) == 1
    assert rows[0].agent_id == "careagents"
    assert rows[0].detail == "disconnected by account holder"


def test_revoke_is_idempotent(client, secret):
    """MUTATION: drop the `if not already or connections or jobs` condition
    on the audit write -> a second audit row here."""
    _connection()
    assert _revoke(client).get_json()["already_revoked"] is False
    again = _revoke(client)
    assert again.status_code == 200
    assert again.get_json()["already_revoked"] is True
    assert TenantClosure.query.filter_by(tenant_id=TENANT).count() == 1
    assert len(_audits("fasten_connection_revoked")) == 1


def test_revoke_with_no_fasten_rows_still_tombstones(client, secret):
    """A disconnect before connection_success, or of a sample tenant: no
    rows to flip, and the tombstone is still written."""
    resp = _revoke(client)
    assert resp.status_code == 200
    assert resp.get_json()["connections_revoked"] == 0
    assert tenant_closed(TENANT)
    assert len(_audits("fasten_connection_revoked")) == 1


# --- every import path checks the tombstone ---------------------------------

def test_connection_success_after_revoke_creates_nothing(client, secret):
    """The disconnect landed before Fasten's connection_success did.

    MUTATION: delete the tenant_closed check in _handle_connection_success
    -> a fresh authorized row and a trigger_ehi_export call here."""
    assert _revoke(client).status_code == 200
    resp, trigger, _ = _webhook(client, "patient.connection_success", {
        "org_connection_id": "oc-late-1", "external_id": TENANT})
    assert resp.status_code == 200
    assert not trigger.called, "an export was requested for a revoked tenant"
    assert db.session.get(FastenConnection, "oc-late-1") is None
    assert len(_audits("fasten_import_refused")) == 1


def test_connection_success_for_an_existing_row_after_revoke(client, secret):
    _connection(org="oc-rv-1")
    assert _revoke(client).status_code == 200
    resp, trigger, _ = _webhook(client, "patient.connection_success", {
        "org_connection_id": "oc-rv-1", "external_id": TENANT})
    assert resp.status_code == 200
    assert not trigger.called
    conn = db.session.get(FastenConnection, "oc-rv-1")
    assert conn.connection_status == "revoked"
    assert conn.webhook_verified_at is None
    assert len(_audits("fasten_import_refused")) == 1


def test_export_success_on_a_revoked_tenant_opens_no_job(client, secret):
    """The tenant check, not the row status: the row here is still
    authorized, as a row created after the revoke would be.

    MUTATION: delete `tenant_closed(conn.tenant_id)` from
    _handle_export_success -> a job and an ingest thread here."""
    _connection(org="oc-rv-1")
    _tombstone()
    resp, _, thread = _webhook(client, "patient.ehi_export_success", {
        "org_connection_id": "oc-rv-1", "task_id": "task-after",
        "download_links": ["https://example.com/e.ndjson"]})
    assert resp.status_code == 200
    assert not thread.called
    assert FastenJob.query.filter_by(task_id="task-after").first() is None
    assert len(_audits("fasten_import_refused")) == 1


def test_retry_after_revoke_is_refused(client, secret, monkeypatch):
    """MUTATION: delete the tenant_closed check in retry_job -> 202 and a
    relaunched ingest from the stored links."""
    from r6.fasten import routes as fasten_routes
    launched = MagicMock()
    monkeypatch.setattr(fasten_routes, "_launch_ingest", launched)
    _connection()
    _job(status="ingesting")
    assert _revoke(client).status_code == 200
    resp = client.post("/fasten/jobs/task-rv-1/retry",
                       headers={"X-Tenant-Id": TENANT})
    assert resp.status_code == 409
    assert not launched.called
    assert FastenJob.query.filter_by(task_id="task-rv-1").one().status \
        == "failed"


def test_retry_checks_the_tenant_not_only_the_row(client, monkeypatch):
    """The row is still authorized, as one created after the revoke would
    be; only the tombstone says no.

    MUTATION: drop `tenant_closed(tenant_id)` from retry_job's refusal ->
    202 here."""
    from r6.fasten import routes as fasten_routes
    launched = MagicMock()
    monkeypatch.setattr(fasten_routes, "_launch_ingest", launched)
    _connection()
    _job(status="failed")
    _tombstone()
    resp = client.post("/fasten/jobs/task-rv-1/retry",
                       headers={"X-Tenant-Id": TENANT})
    assert resp.status_code == 409
    assert not launched.called
    assert len(_audits("fasten_import_refused")) == 1


def test_register_after_revoke_is_refused(client, secret):
    """MUTATION: delete the tenant_closed check in register_connection ->
    201 and a new authorized row on a disconnected tenant."""
    assert _revoke(client).status_code == 200
    resp = client.post("/fasten/connections",
                       headers={"X-Tenant-Id": TENANT},
                       json={"org_connection_id": "oc-new-1"})
    assert resp.status_code == 409
    assert db.session.get(FastenConnection, "oc-new-1") is None


def test_agent_access_after_revoke_is_refused(client, secret):
    """Registered and webhook-verified, then disconnected before the page
    collected its read token.

    MUTATION: delete the tenant_closed check in agent_access -> 200 and a
    30-day read token for a disconnected tenant."""
    assert client.post("/fasten/connections",
                       headers={"X-Tenant-Id": TENANT},
                       json={"org_connection_id": "oc-rv-1"}).status_code == 201
    _webhook(client, "patient.connection_success",
             {"org_connection_id": "oc-rv-1", "external_id": TENANT})
    assert db.session.get(FastenConnection, "oc-rv-1").webhook_verified_at
    assert _revoke(client).status_code == 200
    resp = client.get("/fasten/connections/oc-rv-1/agent-access",
                      headers={"X-Tenant-Id": TENANT})
    assert resp.status_code == 403
    assert "read_token" not in (resp.get_json() or {})
    assert db.session.get(FastenConnection, "oc-rv-1").agent_token_issued_at is None


def test_agent_access_unknown_connection_is_still_404(client, secret):
    _tombstone()
    resp = client.get("/fasten/connections/oc-missing/agent-access",
                      headers={"X-Tenant-Id": TENANT})
    assert resp.status_code in (401, 404)


# --- the boot reaper ---------------------------------------------------------

def test_reaper_fails_a_revoked_tenants_job_without_triggering(
        app, monkeypatch):
    """MUTATION: delete the tenant_closed check in reap_zombie_jobs ->
    trigger_ehi_export called and the job reset to pending."""
    from datetime import datetime, timedelta, timezone

    from r6.fasten import reaper
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "pk")
    monkeypatch.setenv("FASTEN_PRIVATE_KEY", "sk")
    trigger = MagicMock(return_value={"task_id": "fresh"})
    monkeypatch.setattr(reaper, "trigger_ehi_export", trigger)
    _job(status="ingesting")
    _job(tenant=OTHER, task="task-other", status="ingesting",
         org="oc-rv-other")
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    FastenJob.query.update({"created_at": old})
    db.session.commit()
    _tombstone()

    assert reaper.reap_zombie_jobs() == 1          # the other tenant's job
    trigger.assert_called_once_with("oc-rv-other")
    job = FastenJob.query.filter_by(task_id="task-rv-1").one()
    assert job.status == "failed"
    assert job.failure_reason == "disconnected"


# --- an ingest already running ----------------------------------------------

def _ndjson(n):
    return [json.dumps({"resourceType": "Observation", "id": f"obs-rv-{i}",
                        "status": "final"}) for i in range(n)]


class _Resp:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        return None

    def iter_lines(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_ingest(app, monkeypatch, revoked_after_calls):
    """Run stream_ingest on 25 resources; tenant_closed answers True from
    call number `revoked_after_calls + 1` onward."""
    from r6.fasten import ingester
    calls = {"n": 0}

    def fake_revoked(tenant_id):
        calls["n"] += 1
        return calls["n"] > revoked_after_calls

    opened = []
    monkeypatch.setattr(ingester, "tenant_closed", fake_revoked)
    monkeypatch.setattr(ingester.httpx, "stream",
                        lambda *a, **k: opened.append(1) or _Resp(_ndjson(25)))
    _job(status="pending")
    job_id = FastenJob.query.filter_by(task_id="task-rv-1").one().id
    ingester.stream_ingest(app, job_id, ["https://example.invalid/e.ndjson"],
                           TENANT)
    db.session.expire_all()
    return db.session.get(FastenJob, job_id), calls["n"], len(opened)


def test_a_running_ingest_stops_at_the_next_commit(app, monkeypatch):
    """Checks: before 'downloading', before 'ingesting', then before each
    progress commit (every 10). Revoked from the 4th check on, so the first
    batch of 10 is kept and the second is rolled back unwritten.

    MUTATION: delete the check before the progress commit -> 25 resources
    stored and the job 'complete'."""
    job, _, _ = _run_ingest(app, monkeypatch, revoked_after_calls=3)
    assert R6Resource.query.filter_by(tenant_id=TENANT).count() == 10
    assert job.status == "failed"
    assert job.failure_reason == "disconnected"
    assert len(_audits("fasten_import_complete")) == 0
    assert len(_audits("fasten_import_refused")) == 1


def test_an_ingest_revoked_before_it_starts_writes_nothing(app, monkeypatch):
    """MUTATION: delete the check before the 'downloading' commit -> the job
    walks to 'downloading' and the stream is opened."""
    job, calls, opened = _run_ingest(app, monkeypatch, revoked_after_calls=0)
    assert calls == 1
    assert opened == 0, "the export download was opened after revoke"
    assert R6Resource.query.filter_by(tenant_id=TENANT).count() == 0
    assert job.status == "failed"


def test_an_ingest_revoked_on_the_last_commit_is_not_complete(
        app, monkeypatch):
    """Two progress commits (10, 20), then the final 'complete' commit is
    the 5th check. MUTATION: delete the check before the 'complete' commit
    -> 25 resources and 'complete'."""
    job, _, _ = _run_ingest(app, monkeypatch, revoked_after_calls=4)
    assert R6Resource.query.filter_by(tenant_id=TENANT).count() == 20
    assert job.status == "failed"
    assert len(_audits("fasten_import_complete")) == 0


def test_an_unrevoked_ingest_still_completes(app, monkeypatch):
    job, _, _ = _run_ingest(app, monkeypatch, revoked_after_calls=99)
    assert R6Resource.query.filter_by(tenant_id=TENANT).count() == 25
    assert job.status == "complete"


# --- the connect page --------------------------------------------------------

def test_connect_page_for_a_revoked_tenant_has_no_widget(client, monkeypatch):
    """MUTATION: delete the tenant_closed check in fasten_connect -> the
    Stitch widget renders for a disconnected tenant."""
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public-test-key")
    live = client.get("/connect/revoke-live-tenant").get_data(as_text=True)
    assert "public-test-key" in live          # the probe sees the widget
    # The widget script is on the page (a page check, not URL validation).
    assert re.search(r"embed\.connect\.fastenhealth\.com", live)
    _tombstone()
    resp = client.get(f"/connect/{TENANT}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "public-test-key" not in html
    assert not re.search(r"embed\.connect\.fastenhealth\.com", html)
    assert "open-stitch" not in html
    assert "disconnected" in html.lower()


# --- purge -------------------------------------------------------------------

def test_purge_writes_the_tombstone(client, secret):
    """Delete has the same hole as disconnect: after a purge, a late
    connection_success must not start a new import.

    MUTATION: drop the tombstone write from purge_tenant -> the webhook
    creates a row and requests an export."""
    _connection()
    resp = client.post("/r6/fhir/internal/purge-tenant",
                       json={"tenant_id": TENANT},
                       headers={"X-Internal-Secret": SECRET})
    assert resp.status_code == 200
    assert tenant_closed(TENANT)
    _, trigger, _ = _webhook(client, "patient.connection_success", {
        "org_connection_id": "oc-after-purge", "external_id": TENANT})
    assert not trigger.called
    assert db.session.get(FastenConnection, "oc-after-purge") is None


def test_purge_twice_keeps_one_tombstone(client, secret):
    from r6.purge import purge_tenant
    purge_tenant(TENANT)
    db.session.commit()
    purge_tenant(TENANT)
    db.session.commit()
    assert TenantClosure.query.filter_by(tenant_id=TENANT).count() == 1


def test_purging_a_public_tenant_leaves_it_open(client):
    """QA on #913: an anonymous purge of a public tenant is allowed by
    design (the mint gate exempts it), so a tombstone there would close the
    demo for good: no connect widget, no polling, no writes.

    MUTATION: drop the is_public check in purge_tenant -> tombstoned."""
    resp = client.post("/r6/fhir/internal/purge-tenant",
                       json={"tenant_id": "test-tenant"})
    assert resp.status_code == 200
    assert not tenant_closed("test-tenant")


def test_a_stop_never_rewrites_a_finished_job(app):
    """The conditional UPDATE in _stopped_by_disconnect touches only an
    unfinished job: a revoke landing after 'complete' leaves it complete.

    MUTATION: drop the TERMINAL_STATUSES filter -> 'failed' here."""
    from r6.fasten.ingester import _stopped_by_disconnect
    _job(status="complete")
    job_id = FastenJob.query.filter_by(task_id="task-rv-1").one().id
    _tombstone()
    assert _stopped_by_disconnect(job_id, "task-rv-1", TENANT, "oc-rv-1")
    db.session.expire_all()
    job = db.session.get(FastenJob, job_id)
    assert job.status == "complete"
    assert job.failure_reason is None


# --- every other way records arrive (security review of #913, F3) -----------

_OBS = {"resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]}}


def _refusals():
    return [a for a in AuditEventRecord.query.filter_by(
        tenant_id=TENANT, outcome="failure").all()
        if (a.detail or "").endswith("tenant disconnected")]


def _write_headers():
    from r6.stepup import generate_step_up_token
    return {"X-Tenant-Id": TENANT,
            "X-Step-Up-Token": generate_step_up_token(TENANT),
            "X-Human-Confirmed": "true",
            "Content-Type": "application/fhir+json"}


def test_update_into_a_closed_tenant_is_refused_and_audited(client, secret):
    """MUTATION: drop require_open_tenant from update_resource -> 200."""
    headers = _write_headers()
    created = client.post("/r6/fhir/Observation", json=_OBS, headers=headers)
    assert created.status_code == 201
    rid = created.get_json()["id"]
    _tombstone()
    r = client.put(f"/r6/fhir/Observation/{rid}",
                   json={**_OBS, "id": rid, "status": "amended"},
                   headers=headers)
    assert r.status_code == 409
    assert r.get_json()["issue"][0]["code"] == "conflict"
    rows = _refusals()
    assert len(rows) == 1
    assert rows[0].event_type == "update"
    assert rows[0].detail == ("write refused at r6.update_resource: "
                              "tenant disconnected")


def test_ingest_context_into_a_closed_tenant_is_refused(client, secret):
    """MUTATION: drop require_open_tenant from ingest_context -> 201."""
    _tombstone()
    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [{"resource": {**_OBS, "id": "obs-ctx"}}]}
    r = client.post("/r6/fhir/Bundle/$ingest-context", json=bundle,
                    headers={"X-Tenant-Id": TENANT})
    assert r.status_code == 409
    assert R6Resource.query.filter_by(tenant_id=TENANT).count() == 0
    assert len(_refusals()) == 1


def test_shc_ingest_into_a_closed_tenant_is_refused(client, monkeypatch):
    """MUTATION: drop require_open_tenant from shc ingest -> 200 and a
    background ingest."""
    monkeypatch.setenv("SHC_WEBHOOK_SECRET", "shc-test-secret")
    _tombstone()
    with patch("r6.shc.routes.threading.Thread") as thread:
        r = client.post("/shc/ingest",
                        json={"resourceType": "Bundle", "type": "collection",
                              "entry": [{"resource": {**_OBS, "id": "o-1"}}]},
                        headers={"X-Tenant-Id": TENANT,
                                 "Authorization": "Bearer shc-test-secret"})
    assert r.status_code == 409
    assert not thread.called
    assert len(_refusals()) == 1


def test_the_closed_tenant_check_runs_after_authorization(client, secret):
    """An anonymous caller learns nothing about which tenants are closed:
    it gets the auth refusal, never the 409."""
    _tombstone()
    r = client.post("/r6/fhir/internal/ingest-bundle",
                    json={"bundle": {"resourceType": "Bundle", "entry": []}},
                    headers={"X-Tenant-Id": TENANT})
    assert r.status_code == 403


def test_an_open_tenant_still_takes_writes(client, secret):
    _tombstone(tenant=OTHER)
    r = client.post("/r6/fhir/Observation", json=_OBS,
                    headers=_write_headers())
    assert r.status_code == 201
