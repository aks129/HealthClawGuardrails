"""Bundle ingestion is a clinical write: it requires write authorization,
and it leaves an audit trail.

The authorization half was already pinned by the three credential states
below. The audit half was not pinned by anything: both `record_audit_event`
calls on this route could be deleted with the full suite green (#634 F1),
against the standing rule that every FHIR resource access emits an
AuditEvent. A rejected write is the case that matters most — a credential
probe against a private tenant left no trace at all.

The two tests at the bottom count rows before and after rather than
asserting a row exists, so they cannot pass on audit rows some other test
left behind.

MUTATION: delete the `record_audit_event` call on the deny path
(r6/routes.py:1251) -> the deny test fails; delete the one on the success
path (:1269) -> the success test fails. Both executed 2026-09-05.
"""

from r6.models import AuditEventRecord
from r6.stepup import generate_step_up_token


def test_ingest_context_requires_write_token_when_auth_enabled(
    client, sample_bundle, monkeypatch,
):
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    tenant = 'private-ingest-tenant'
    base_headers = {'X-Tenant-Id': tenant}

    missing = client.post(
        '/r6/fhir/Bundle/$ingest-context',
        json=sample_bundle,
        headers=base_headers,
    )
    assert missing.status_code == 401

    read_only = client.post(
        '/r6/fhir/Bundle/$ingest-context',
        json=sample_bundle,
        headers={
            **base_headers,
            'X-Step-Up-Token': generate_step_up_token(tenant, scope='read'),
        },
    )
    assert read_only.status_code == 401

    authorized = client.post(
        '/r6/fhir/Bundle/$ingest-context',
        json=sample_bundle,
        headers={
            **base_headers,
            'X-Step-Up-Token': generate_step_up_token(tenant),
        },
    )
    assert authorized.status_code == 201


def _audit_rows(app, tenant):
    with app.app_context():
        return AuditEventRecord.query.filter_by(tenant_id=tenant).count()


def test_a_refused_ingest_is_audited(app, client, sample_bundle, monkeypatch):
    """A rejected clinical write must leave a trace.

    Without this, a credential-probing loop against a private tenant is
    silent: the write is refused and nothing records that it was attempted.
    """
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    tenant = 'audited-refusal-tenant'
    before = _audit_rows(app, tenant)

    refused = client.post(
        '/r6/fhir/Bundle/$ingest-context',
        json=sample_bundle,
        headers={'X-Tenant-Id': tenant},
    )
    assert refused.status_code == 401

    assert _audit_rows(app, tenant) > before, (
        'the refused ingest wrote no AuditEvent, so a credential probe '
        'against this tenant leaves no trace'
    )


def test_an_accepted_ingest_is_audited(app, client, sample_bundle, monkeypatch):
    """And so must an accepted one — resources landed in the record."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    tenant = 'audited-ingest-tenant'
    before = _audit_rows(app, tenant)

    accepted = client.post(
        '/r6/fhir/Bundle/$ingest-context',
        json=sample_bundle,
        headers={
            'X-Tenant-Id': tenant,
            'X-Step-Up-Token': generate_step_up_token(tenant),
        },
    )
    assert accepted.status_code == 201

    assert _audit_rows(app, tenant) > before, (
        'resources were written to the tenant record with no AuditEvent'
    )
