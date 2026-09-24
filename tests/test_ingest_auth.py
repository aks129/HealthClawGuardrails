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

MUTATION: delete the `record_audit_event` call on the success path in
`ingest_context` -> the success test fails (executed 2026-09-05). The deny
path's row is written by the kernel since #648 PR 2, not by the route: drop
the `require_grant` call -> every refusal test fails; delete
`_audit_refusal(exc)` in r6/access.py -> the deny tests fail.
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


def test_a_refused_ingest_writes_one_row_and_no_agent_id(
        app, client, sample_bundle, monkeypatch):
    """Exactly one row per refusal — the kernel's — and the caller's
    X-Agent-Id is not in it: that header is caller-controlled, not identity
    (owner ruling on #648).

    MUTATION: put the old site-level record_audit_event back, before
    require_grant -> 2 rows; pass agent_id=request.headers.get('X-Agent-Id')
    to the kernel's audit -> the agent id is stored.
    """
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    tenant = 'one-row-refusal-tenant'
    with app.app_context():
        before = {r.id for r in AuditEventRecord.query.filter_by(
            tenant_id=tenant)}

    refused = client.post(
        '/r6/fhir/Bundle/$ingest-context',
        json=sample_bundle,
        headers={'X-Tenant-Id': tenant,
                 'X-Step-Up-Token': generate_step_up_token(tenant) + 'x',
                 'X-Agent-Id': 'caller-chosen-agent-id'},
    )
    assert refused.status_code == 401

    with app.app_context():
        new = [r for r in AuditEventRecord.query.filter_by(tenant_id=tenant)
               if r.id not in before]
        assert len(new) == 1, [(r.event_type, r.detail) for r in new]
        row = new[0]
        assert row.outcome == 'failure'
        assert row.event_type == 'create'
        assert row.detail == ('step-up refused (rejected) at '
                              'r6.ingest_context: Invalid token signature')
        # agent_id is the audit layer's own default, never the header.
        assert 'caller-chosen-agent-id' not in ' '.join(
            str(v) for v in (row.agent_id, row.resource_type,
                             row.resource_id, row.context_id, row.detail))


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


# ---------------------------------------------------------------------------
# The wire contract, byte for byte (#648 PR 2)
# ---------------------------------------------------------------------------

#: The refusal a client sees, exactly. Owner ruling on #648: the move to the
#: kernel keeps this body byte-identical for every refusal kind — absent,
#: under-scoped, garbage and another tenant's token all answer the same
#: sentence, so the kernel's classified reasons do not reach this route.
_REFUSAL_BYTES = (
    b'{"issue":[{"code":"security","diagnostics":"Bundle ingestion requires '
    b'a tenant-bound write token","severity":"error"}],'
    b'"resourceType":"OperationOutcome"}\n'
)


def _refusal_tokens(tenant):
    return {
        'absent': '',
        'read-scoped': generate_step_up_token(tenant, scope='read'),
        'garbage': generate_step_up_token(tenant) + 'x',
        'other-tenant': generate_step_up_token('some-other-tenant'),
    }


def test_every_refusal_answers_the_same_bytes(client, sample_bundle,
                                              monkeypatch):
    """MUTATION: change the sentence at the route -> red; drop the
    site-specific message so the kernel's reason renders -> red."""
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    tenant = 'byte-pinned-tenant'
    for kind, token in _refusal_tokens(tenant).items():
        refused = client.post(
            '/r6/fhir/Bundle/$ingest-context',
            json=sample_bundle,
            headers={'X-Tenant-Id': tenant, 'X-Step-Up-Token': token},
        )
        assert refused.status_code == 401, kind
        assert refused.mimetype == 'application/json', kind
        assert refused.get_data() == _REFUSAL_BYTES, (
            kind, refused.get_data())
