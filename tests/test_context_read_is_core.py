"""`GET /r6/fhir/context/<id>` is core, so its guardrails are tested in core.

The route reads context envelopes from the store and returns them, and with
`?_include=resources` it returns the records they point at, redacted. It stays
in the core when the `$ingest-context` provider is not installed
(r6/extensions.py). The tests that covered its tenant check and its redaction
built their envelope through `$ingest-context`, so they are marked
`extension` and the core-only job deselects them. These seed the envelope
directly instead, so the same checks run with no extension loaded.

MUTATIONS, each in r6/routes.py `get_context`:
- drop `tenant_id=tenant_id` from the ContextEnvelope lookup
  -> test_another_tenant_cannot_read_the_envelope is red.
- drop `tenant_id=tenant_id` from the R6Resource lookup in the include loop
  -> test_included_resources_are_redacted_and_stay_in_the_tenant is red.
- replace `apply_redaction(r.to_fhir_json())` with `r.to_fhir_json()`
  -> test_included_resources_are_redacted_and_stay_in_the_tenant is red.
- delete the `expires < now` branch -> test_an_expired_envelope_is_refused is red.
- delete the `record_audit_event('read', 'ContextEnvelope', ...)` call
  -> test_a_read_is_audited_and_found_by_its_context_id is red.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from models import db
from r6.models import AuditEventRecord, ContextEnvelope, ContextItem, R6Resource

CTX = "ctx-core-1"
EXPIRED = "ctx-core-expired"
FAMILY = "Synthetica"
MRN = "MRN-CORE-55501"
STREET = "742 Synthetic Terrace"
FOREIGN = "ctx-core-foreign"


def _store(tenant, resource):
    db.session.add(R6Resource(
        resource_type=resource["resourceType"],
        resource_json=json.dumps(resource),
        resource_id=resource["id"], tenant_id=tenant))


def _envelope(context_id, tenant, refs, expires_at):
    db.session.add(ContextEnvelope(
        context_id=context_id, tenant_id=tenant,
        patient_ref="Patient/ctx-core-p", expires_at=expires_at))
    db.session.flush()
    for ref in refs:
        db.session.add(ContextItem(context_id=context_id, resource_ref=ref,
                                   resource_version="1", slice_name="core"))


@pytest.fixture
def seeded(app, tenant_id):
    now = datetime.now(timezone.utc)
    with app.app_context():
        _store(tenant_id, {
            "resourceType": "Patient", "id": "ctx-core-p",
            "name": [{"family": FAMILY, "given": ["Corey"]}],
            "identifier": [{"system": "http://example.org/mrn", "value": MRN}],
            "address": [{"line": [STREET], "city": "Springfield"}],
            "birthDate": "1970-04-02"})
        _store(tenant_id, {
            "resourceType": "Observation", "id": "ctx-core-o",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": "8867-4"}]},
            "subject": {"reference": "Patient/ctx-core-p"},
            "valueQuantity": {"value": 72, "unit": "/min"}})
        # A row in another tenant that this tenant's envelope names. The
        # include loop must not follow the reference across the boundary.
        _store("other-tenant", {
            "resourceType": "Patient", "id": FOREIGN,
            "name": [{"family": "Foreignname"}]})
        _envelope(CTX, tenant_id,
                  ["Patient/ctx-core-p", "Observation/ctx-core-o",
                   f"Patient/{FOREIGN}"],
                  now + timedelta(hours=1))
        _envelope(EXPIRED, tenant_id, ["Patient/ctx-core-p"],
                  now - timedelta(minutes=1))
        db.session.commit()
    return CTX


def test_another_tenant_cannot_read_the_envelope(
        client, seeded, tenant_headers, other_tenant_headers):
    assert client.get(f"/r6/fhir/context/{seeded}",
                      headers=tenant_headers).status_code == 200
    resp = client.get(f"/r6/fhir/context/{seeded}",
                      headers=other_tenant_headers)
    assert resp.status_code == 404
    assert "ctx-core-p" not in resp.get_data(as_text=True)


def test_included_resources_are_redacted_and_stay_in_the_tenant(
        client, seeded, tenant_headers):
    resp = client.get(f"/r6/fhir/context/{seeded}?_include=resources",
                      headers=tenant_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    body = resp.get_data(as_text=True)

    ids = sorted(r["id"] for r in data["resources"])
    assert ids == ["ctx-core-o", "ctx-core-p"], ids
    assert "Foreignname" not in body

    patient = next(r for r in data["resources"] if r["id"] == "ctx-core-p")
    assert FAMILY not in body
    assert MRN not in body
    assert STREET not in body
    for ident in patient.get("identifier", []):
        assert "value" not in ident
    for addr in patient.get("address", []):
        assert "line" not in addr


def test_an_expired_envelope_is_refused(client, seeded, tenant_headers):
    resp = client.get(f"/r6/fhir/context/{EXPIRED}?_include=resources",
                      headers=tenant_headers)
    assert resp.status_code == 410
    assert FAMILY not in resp.get_data(as_text=True)


def test_a_read_is_audited_and_found_by_its_context_id(
        app, client, seeded, tenant_id, tenant_headers):
    assert client.get(f"/r6/fhir/context/{seeded}",
                      headers=tenant_headers).status_code == 200
    with app.app_context():
        rows = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, context_id=seeded,
            resource_type="ContextEnvelope").all()
        assert [r.event_type for r in rows] == ["read"]
        # No agent header was sent: the core's own default, with or without
        # the health-context provider.
        assert rows[0].agent_id == "healthclaw-guardrails"
        assert FAMILY not in (rows[0].detail or "")

    resp = client.get(f"/r6/fhir/AuditEvent?context-id={seeded}",
                      headers=tenant_headers)
    assert resp.status_code == 200
    assert resp.get_json()["total"] >= 1
