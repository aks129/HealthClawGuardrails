"""Slice 9: the four blueprints that accepted any string as a tenant id.

Before this slice `r6/smbp`, `r6/shc`, `r6/fasten` and `r6/wearables` each read
`X-Tenant-Id` by hand and used whatever string arrived as a partition key and
as audit detail. `X-Tenant-Id: ../../etc/passwd` was a valid tenant on
`/r6/smbp/enroll`, `/shc/ingest`, `/fasten/connections` and
`/fasten/jobs/<id>/retry`; on the remaining sites a later gate happened to
refuse first, which made the missing validation invisible rather than absent.

The property pinned here is the one `tenant_from_request` promises
(`docs/2026-08-03-access-kernel-spec.md` §1.1): the id that reaches the
handler matched ``[A-Za-z0-9_-]{1,64}``. Three shapes are probed because they
fail for three different reasons — a path traversal (illegal characters), an
over-long id (length), and an id with a space (the shape a header-splitting or
log-injection attempt takes).

The companion pin is `tests/test_write_guard_matrix.py`, where five rows gain
TENANT_FORMAT in this same change. This file states the refusal per route;
the matrix states which guards the route is claimed to have.

Constitution rule 20: each test names the edit that must turn it red.
"""

from __future__ import annotations

import uuid

import pytest

MALFORMED = [
    pytest.param("../../etc/passwd", id="path-traversal"),
    pytest.param("a" * 65, id="over-64-chars"),
    pytest.param("tenant with space", id="contains-a-space"),
]

SECRET = "slice9-internal-secret"


@pytest.fixture
def shc_secret(monkeypatch):
    monkeypatch.setenv("SHC_WEBHOOK_SECRET", SECRET)
    return SECRET


# ---------------------------------------------------------------------------
# smbp — /r6/smbp/enroll, /reading, /report/<id>
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tenant", MALFORMED)
def test_smbp_enroll_refuses_a_malformed_tenant_id(client, tenant):
    """MUTATION: return the raw header from r6/smbp/routes.py::_tenant -> red.

    Before slice 9 this answered 201 and persisted an SMBPSession partitioned
    by the malformed string.
    """
    resp = client.post("/r6/smbp/enroll",
                       headers={"X-Tenant-Id": tenant},
                       json={"patient_ref": "Patient/slice9"})
    assert resp.status_code == 400, resp.get_data(as_text=True)


@pytest.mark.parametrize("tenant", MALFORMED)
def test_smbp_reading_refuses_a_malformed_tenant_id(client, tenant):
    """MUTATION: return the raw header from r6/smbp/routes.py::_tenant -> red.

    The 401 this answered before came from the step-up gate below the tenant
    read, not from any format check: a caller holding a token would have been
    let through with the malformed id intact.
    """
    resp = client.post("/r6/smbp/reading",
                       headers={"X-Tenant-Id": tenant},
                       json={"patient_ref": "Patient/slice9", "systolic": 128,
                             "diastolic": 78, "effective": "2026-08-02T10:00:00Z"})
    assert resp.status_code == 400, resp.get_data(as_text=True)


@pytest.mark.parametrize("tenant", MALFORMED)
def test_smbp_report_refuses_a_malformed_tenant_id(client, tenant):
    """MUTATION: return the raw header from r6/smbp/routes.py::_tenant -> red."""
    resp = client.get("/r6/smbp/report/slice9-session",
                      headers={"X-Tenant-Id": tenant})
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_smbp_still_refuses_an_absent_tenant_header_in_its_own_dialect(client):
    """The MISSING-tenant answer is unchanged by the migration.

    MUTATION: let TenantRejected('absent') propagate out of _tenant() -> the
    body becomes the kernel's OperationOutcome text and this goes red.
    """
    resp = client.post("/r6/smbp/enroll", json={"patient_ref": "Patient/x"})
    assert resp.status_code == 400
    assert resp.get_json()["issue"][0]["diagnostics"] == "X-Tenant-Id required"


# ---------------------------------------------------------------------------
# shc — /shc/ingest
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tenant", MALFORMED)
def test_shc_ingest_refuses_a_malformed_tenant_id(client, tenant, shc_secret):
    """MUTATION: return the raw header from r6/shc/routes.py::ingest -> red.

    Before slice 9 this answered 200 and handed the malformed id to the
    background ingest thread as the tenant every resource was written under.
    """
    resp = client.post("/shc/ingest",
                       headers={"X-Tenant-Id": tenant,
                                "Authorization": f"Bearer {shc_secret}"},
                       json={"resourceType": "Bundle", "entry": []})
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_shc_ingest_still_refuses_an_absent_tenant_header_in_its_own_dialect(
        client, shc_secret):
    """MUTATION: let TenantRejected('absent') propagate -> red."""
    resp = client.post("/shc/ingest",
                       headers={"Authorization": f"Bearer {shc_secret}"},
                       json={"resourceType": "Bundle", "entry": []})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "X-Tenant-Id header required"}


def test_shc_ingest_checks_its_shared_secret_before_the_tenant_format(
        client, monkeypatch):
    """A malformed tenant must not turn an unauthorized call into a 400.

    MUTATION: move the tenant read above `if not _verify_secret()` -> red.
    Answering 400 rather than 401 would tell an unauthenticated caller that
    the header was read at all.
    """
    monkeypatch.delenv("SHC_WEBHOOK_SECRET", raising=False)
    resp = client.post("/shc/ingest",
                       headers={"X-Tenant-Id": "../../etc/passwd"},
                       json={"resourceType": "Bundle", "entry": []})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# fasten — /fasten/connections, /fasten/jobs/<id>/retry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tenant", MALFORMED)
def test_fasten_register_connection_refuses_a_malformed_tenant_id(client,
                                                                  tenant):
    """MUTATION: return the raw header from fasten.register_connection -> red.

    Before slice 9 this answered 201 and bound an EHR connection to the
    malformed string — the tenant header alone is what binds that connection.
    """
    resp = client.post("/fasten/connections",
                       headers={"X-Tenant-Id": tenant},
                       json={"org_connection_id": f"slice9-{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 400, resp.get_data(as_text=True)


@pytest.mark.parametrize("tenant", MALFORMED)
def test_fasten_retry_job_refuses_a_malformed_tenant_id(client, tenant):
    """MUTATION: return the raw header from fasten.retry_job -> red.

    Before slice 9 this answered 404 — a tenant-scoped lookup miss, meaning
    the malformed id had already been used as the partition key.
    """
    resp = client.post("/fasten/jobs/slice9-task/retry",
                       headers={"X-Tenant-Id": tenant})
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_fasten_still_refuses_an_absent_tenant_header_in_its_own_dialect(client):
    """MUTATION: let TenantRejected('absent') propagate -> red."""
    resp = client.post("/fasten/connections",
                       json={"org_connection_id": "slice9-absent"})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "X-Tenant-Id header required"}


@pytest.mark.parametrize("tenant", MALFORMED)
def test_fasten_read_routes_keep_their_401_for_a_malformed_tenant(client,
                                                                  tenant):
    """The read helper's dialect is preserved, not normalized.

    MUTATION: re-raise TenantRejected('malformed') from
    fasten._tenant_for_read instead of answering 401 -> red.

    `_tenant_for_read` already refused a malformed id, because
    authorize_tenant_read validates the pattern before anything else. Adopting
    the kernel here is a pure move; turning its 401 into a 400 would be a
    status-dialect change, which is a separate deliberate PR.
    """
    resp = client.get("/fasten/connections", headers={"X-Tenant-Id": tenant})
    assert resp.status_code == 401, resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# wearables — /wearables/sync-now
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tenant", MALFORMED)
def test_wearables_sync_now_refuses_a_malformed_tenant_id(client, tenant):
    """MUTATION: return the raw header from wearables.sync_now -> red.

    The 403 this answered before came from the missing step-up token, not
    from a format check — the matrix row already claimed TENANT_FORMAT on the
    strength of that 403.
    """
    resp = client.post("/wearables/sync-now", headers={"X-Tenant-Id": tenant})
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_wearables_still_refuses_an_absent_tenant_header_in_its_own_dialect(
        client):
    """MUTATION: let TenantRejected('absent') propagate -> red."""
    resp = client.post("/wearables/sync-now")
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "X-Tenant-Id required"}


# ---------------------------------------------------------------------------
# actions — every route behind _tenant_or_none (kernel slice 10)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tenant", MALFORMED)
def test_actions_propose_refuses_a_malformed_tenant_id(client, tenant):
    """MUTATION: return the raw header from _tenant_or_none -> red.

    Before slice 10 this blueprint collapsed ABSENT and MALFORMED into the
    same `None`, so both answered the same 400 with the same message. The
    status is unchanged; what changes is that a malformed id is now refused
    by the kernel and says so, instead of being reported as a missing
    header.
    """
    resp = client.post("/r6/actions/propose", json={},
                       headers={"X-Tenant-Id": tenant})
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_actions_still_refuses_an_absent_tenant_header_in_its_own_dialect(client):
    """The absent case must not move — six handlers answer it.

    MUTATION: let TenantRejected('absent') propagate out of
    _tenant_or_none -> red (the body becomes an OperationOutcome).
    """
    resp = client.post("/r6/actions/propose", json={})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "X-Tenant-Id header is required"}
