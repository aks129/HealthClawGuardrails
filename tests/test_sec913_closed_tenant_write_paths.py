"""Security re-review of #913 at dc419ca: write paths into a closed tenant
that `require_open_tenant` does not cover yet.

A tenant is closed once it carries a FastenTenantRevocation tombstone (a
CareAgents Disconnect, or a purge). The PR gates FHIR create and update,
$ingest-context, /internal/ingest-bundle, /shc/ingest and the wearables
OAuth callback. Every other route that stores an R6Resource is probed
here with a valid credential for the tenant. Synthetic ids only.

Plain tests are properties that hold. Each xfail(strict=True) row is a
path that still stores a record in a closed tenant; a fix flips it red.
"""

import pytest

from models import db
from r6.fasten.models import FastenTenantRevocation
from r6.models import AuditEventRecord, R6Resource

SECRET = "sec913-reverify-secret"
CLOSED = "sec913-closed"
OPEN = "sec913-open"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    return SECRET


def _close(tenant=CLOSED):
    db.session.add(FastenTenantRevocation(tenant_id=tenant))
    db.session.commit()


def _token(tenant=CLOSED):
    from r6.stepup import generate_step_up_token
    return generate_step_up_token(tenant)


def _count(tenant=CLOSED, rtype=None):
    q = R6Resource.query.filter_by(tenant_id=tenant)
    if rtype:
        q = q.filter_by(resource_type=rtype)
    return q.count()


def _obs():
    return {"resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": "8867-4"}]}}


# --- covered paths hold ------------------------------------------------------

def test_covered_create_update_and_ingest_refuse_with_one_audit_row(
        client, secret):
    tok = _token()
    h = {"X-Tenant-Id": CLOSED, "X-Step-Up-Token": tok,
         "X-Human-Confirmed": "true"}
    # A record from before the disconnect, kept.
    r = client.post("/r6/fhir/Observation", json={**_obs(), "id": "keep-1"},
                    headers=h)
    assert r.status_code == 201
    _close()
    assert client.post("/r6/fhir/Observation", json=_obs(),
                       headers=h).status_code == 409
    rid = R6Resource.query.filter_by(tenant_id=CLOSED).one().id
    assert client.put(f"/r6/fhir/Observation/{rid}",
                      json={**_obs(), "id": rid, "status": "amended"},
                      headers=h).status_code == 409
    assert client.post("/r6/fhir/Bundle/$ingest-context",
                       json={"resourceType": "Bundle", "type": "collection",
                             "entry": [{"resource": _obs()}]},
                       headers=h).status_code == 409
    assert _count() == 1
    rows = AuditEventRecord.query.filter(
        AuditEventRecord.tenant_id == CLOSED,
        AuditEventRecord.detail.like("write refused at %")).all()
    assert len(rows) == 3
    for row in rows:
        assert row.outcome == "failure"
        assert row.detail.endswith(": tenant disconnected")
        assert "8867-4" not in row.detail and "amended" not in row.detail


def test_no_oracle_for_an_anonymous_caller(client, secret, monkeypatch):
    """With read auth on (production), an anonymous write gets the same
    answer for a closed tenant and an open one, and no refusal audit row is
    written for the closed tenant."""
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    _close()
    for path, kw in (
            ("/r6/fhir/Observation", {"json": _obs()}),
            ("/r6/fhir/Bundle/$ingest-context",
             {"json": {"resourceType": "Bundle", "type": "collection",
                       "entry": [{"resource": _obs()}]}}),
            ("/r6/fhir/internal/ingest-bundle",
             {"json": {"bundle": {"resourceType": "Bundle",
                                  "type": "collection", "entry": []}}}),
            ("/shc/ingest", {"json": {"resourceType": "Bundle",
                                      "entry": [{"resource": _obs()}]}})):
        closed = client.post(path, headers={"X-Tenant-Id": CLOSED}, **kw)
        opened = client.post(path, headers={"X-Tenant-Id": OPEN}, **kw)
        assert closed.status_code == opened.status_code != 409, path
    assert AuditEventRecord.query.filter(
        AuditEventRecord.detail.like("write refused at %")).count() == 0


def test_read_auth_off_makes_ingest_context_an_oracle(client, secret,
                                                      monkeypatch):
    """Config-conditional and pre-existing in kind: with READ_AUTH_ENABLED
    unset, $ingest-context takes anonymous writes, so the 409 tells an
    anonymous caller which tenants are closed. Pinned as the documented
    non-production posture, not a property to keep."""
    monkeypatch.delenv("READ_AUTH_ENABLED", raising=False)
    _close()
    body = {"resourceType": "Bundle", "type": "collection",
            "entry": [{"resource": _obs()}]}
    closed = client.post("/r6/fhir/Bundle/$ingest-context", json=body,
                         headers={"X-Tenant-Id": CLOSED})
    opened = client.post("/r6/fhir/Bundle/$ingest-context", json=body,
                         headers={"X-Tenant-Id": OPEN})
    assert closed.status_code == 409 and opened.status_code != 409


# --- paths that still write into a closed tenant -----------------------------

@pytest.mark.xfail(strict=True, reason="sec913 R1: POST /r6/smbp/reading "
                   "stores an Observation in a closed tenant")
def test_smbp_reading_into_a_closed_tenant(client, secret):
    _close()
    client.post("/r6/smbp/reading",
                json={"patient_ref": "Patient/p1", "systolic": 128,
                      "diastolic": 82, "effective": "2026-10-08T09:00:00Z"},
                headers={"X-Tenant-Id": CLOSED,
                         "X-Step-Up-Token": _token()})
    assert _count() == 0


@pytest.mark.xfail(strict=True, reason="sec913 R2: GET /r6/smbp/report "
                   "persists a DocumentReference in a closed tenant, and "
                   "POST /r6/smbp/enroll (tenant header only) opens the "
                   "session it needs")
def test_smbp_enroll_and_report_into_a_closed_tenant(client, secret):
    _close()
    h = {"X-Tenant-Id": CLOSED, "X-Step-Up-Token": _token()}
    r = client.post("/r6/smbp/enroll", json={"patient_ref": "Patient/p1"},
                    headers=h)
    enrolled = r.status_code == 201
    if enrolled:
        client.get(f"/r6/smbp/report/{r.get_json()['id']}?format=pdf",
                   headers=h)
    assert enrolled, "enroll was refused; probe the report another way"
    assert _count(rtype="DocumentReference") == 0


@pytest.mark.xfail(strict=True, reason="sec913 R3: /internal/seed (internal "
                   "secret) seeds a closed tenant, built-in set and a "
                   "caller-supplied bundle")
@pytest.mark.parametrize("bundle", [None, {"resourceType": "Bundle",
                                           "entry": [{"resource": {
                                               **_obs(), "id": "seed-x"}}]}])
def test_seed_into_a_closed_tenant(client, secret, bundle):
    _close()
    body = {"tenant_id": CLOSED}
    if bundle:
        body["bundle"] = bundle
    client.post("/r6/fhir/internal/seed", json=body,
                headers={"X-Internal-Secret": SECRET})
    assert _count() == 0


@pytest.mark.xfail(strict=True, reason="sec913 R4: $curatr-apply-fix "
                   "rewrites a kept record and adds a Provenance in a "
                   "closed tenant")
def test_curatr_apply_fix_in_a_closed_tenant(client, secret):
    h = {"X-Tenant-Id": CLOSED, "X-Step-Up-Token": _token(),
         "X-Human-Confirmed": "true"}
    cond = {"resourceType": "Condition", "subject": {"reference":
                                                     "Patient/p1"},
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-9-cm",
                                 "code": "250.00"}]}}
    rid = client.post("/r6/fhir/Condition", json=cond,
                      headers=h).get_json()["id"]
    _close()
    client.post(f"/r6/fhir/Condition/{rid}/$curatr-apply-fix",
                json={"fixes": [
                    {"field_path": "Condition.code.coding[0].system",
                     "new_value": "http://hl7.org/fhir/sid/icd-10-cm"},
                    {"field_path": "Condition.code.coding[0].code",
                     "new_value": "E11.9"}],
                      "patient_intent": "update code"},
                headers=h)
    assert _count(rtype="Provenance") == 0


@pytest.mark.xfail(strict=True, reason="sec913 R5: /demo/agent-loop (mint "
                   "gate: internal secret) writes into a closed tenant")
def test_demo_agent_loop_into_a_closed_tenant(client, secret):
    _close()
    client.post("/r6/fhir/demo/agent-loop",
                headers={"X-Tenant-Id": CLOSED, "X-Internal-Secret": SECRET})
    assert _count() == 0


@pytest.mark.xfail(strict=True, reason="sec913 R6: the action rail "
                   "(propose, commit, review submit) stores a reviewed "
                   "QuestionnaireResponse in a closed tenant")
def test_action_rail_review_writes_into_a_closed_tenant(
        app, client, tenant_headers, auth_headers):
    from tests.test_intake_attestation_gate import (
        _allergy, _committed_action, _medication, _patient, _review, _store)
    tenant = tenant_headers["X-Tenant-Id"]
    for resource in (_patient(), _medication(), _allergy()):
        _store(resource, tenant)
    _close(tenant)
    action_id = _committed_action(client, tenant_headers, auth_headers)
    _review(client, auth_headers, action_id,
            **{"med-0": "yes", "allergy-0": "confirm"})
    assert _count(tenant, "QuestionnaireResponse") == 0


def test_extract_commits_nothing_today(client, secret):
    """$extract's commit allowlist (COMMIT_WITHOUT_CONFIRMATION) is empty,
    so it stores nothing anywhere; recorded so a widened allowlist is
    noticed here too."""
    from r6.sdc import routes as sdc_routes
    assert not getattr(sdc_routes, "COMMIT_WITHOUT_CONFIRMATION", set())


# --- revoke on a public tenant ------------------------------------------------

def test_revoke_with_the_secret_closes_a_public_tenant_for_good(client,
                                                               secret):
    """Accepted only because the secret is infrastructure-only and CareAgents
    never names a public tenant (every connection gets a fresh ca- id). It
    is irreversible: no route clears the tombstone, and purge now skips
    public tenants, so the demo tenant stays closed to writes."""
    r = client.post("/r6/fhir/internal/fasten-revoke",
                    json={"tenant_id": "desktop-demo"},
                    headers={"X-Internal-Secret": SECRET})
    assert r.status_code == 200
    tok = _token("desktop-demo")
    assert client.post("/r6/fhir/Observation", json=_obs(),
                       headers={"X-Tenant-Id": "desktop-demo",
                                "X-Step-Up-Token": tok,
                                "X-Human-Confirmed": "true"}
                       ).status_code == 409
