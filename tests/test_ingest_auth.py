"""Bundle ingestion is a clinical write and requires write authorization."""

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
