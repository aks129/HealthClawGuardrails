"""A caller-supplied seed bundle needs the ingest secret, not the mint gate.

Found while building the SMBP demo data, by reading `/internal/seed` to work
out how to deliver it. The endpoint accepts a caller-supplied `bundle` and was
gated by `_internal_mint_authorized`, which returns True for any PUBLIC tenant
with no credential at all.

Measured against production before the fix:

    POST /r6/fhir/internal/seed  {"tenant_id": "desktop-demo", "bundle": {...}}
    -> HTTP 201, created_count 1, and a write-capable step_up_token

`desktop-demo` is the tenant the public MCP demo serves to every Claude user.
So a stranger could author FHIR content that lands in an LLM's context, with
no resource-type allowlist in front of it.

WHAT THIS IS NOT

It is not "writes to public tenants are now closed". Anyone can still mint a
desktop-demo step-up token and write through the normal FHIR path. That is
deliberate — the demo bot depends on it, and the mint gate's exemption is
sound reasoning ABOUT TOKENS: minting for a tenant that already bypasses
read-auth grants nothing extra.

The defect is that the reasoning was reused for CONTENT. The repo already
knew the difference: `_internal_ingest_authorized` was written for exactly
this and its docstring opens "Deliberately NOT `_internal_mint_authorized`".
Two gates, one hardened, one not, on two paths that do the same thing — the
defect shape in docs/2026-08-02-retro.md, arriving again.
"""

import json

import pytest


SECRET = 'test-internal-secret-not-real'


def _bundle(resource_id='guard-probe-obs'):
    return {
        'resourceType': 'Bundle',
        'type': 'collection',
        'entry': [{'resource': {
            'resourceType': 'Observation',
            'id': resource_id,
            'status': 'final',
            'code': {'coding': [{'system': 'http://loinc.org',
                                 'code': '8480-6'}]},
            'subject': {'reference': 'Patient/demo-patient-rivera'},
        }}],
    }


def _seed(client, body, secret=None):
    headers = {'Content-Type': 'application/json'}
    if secret is not None:
        headers['X-Internal-Secret'] = secret
    return client.post('/r6/fhir/internal/seed', data=json.dumps(body),
                       headers=headers)


@pytest.fixture
def secret_configured(monkeypatch):
    """With the secret set, the gate is a credential check in every env."""
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)


class TestABundleNeedsTheIngestSecret:

    def test_a_bundle_without_the_secret_is_refused(self, client,
                                                    secret_configured):
        """MUTATION: drop the _internal_ingest_authorized check -> red.

        This is the production behaviour that was measured at 201.
        """
        res = _seed(client, {'tenant_id': 'desktop-demo',
                             'bundle': _bundle()})
        assert res.status_code == 403

    def test_the_refused_bundle_wrote_nothing(self, client, secret_configured):
        """A 403 that still persisted the resource would be the worse bug:
        the guard reports refusal and the content is in the tenant anyway."""
        _seed(client, {'tenant_id': 'desktop-demo',
                       'bundle': _bundle('guard-probe-not-written')})
        read = client.get('/r6/fhir/Observation/guard-probe-not-written',
                          headers={'X-Tenant-Id': 'desktop-demo'})
        assert read.status_code == 404

    def test_a_bundle_with_the_secret_is_accepted(self, client,
                                                  secret_configured):
        """The operator path has to keep working — it is how the SMBP demo
        history is delivered."""
        res = _seed(client, {'tenant_id': 'desktop-demo',
                             'bundle': _bundle('guard-probe-allowed')},
                    secret=SECRET)
        assert res.status_code == 201
        assert res.get_json()['created_count'] == 1

    def test_a_wrong_secret_is_refused(self, client, secret_configured):
        res = _seed(client, {'tenant_id': 'desktop-demo',
                             'bundle': _bundle()}, secret='wrong')
        assert res.status_code == 403


class TestTheBuiltInSeedIsUnchanged:
    """railway.toml runs `seed-demo` before every deploy and the browser demo
    dashboard seeds on demand. Neither carries the internal secret, and both
    seed the FIXED built-in set this repo wrote — the content nobody else
    chose. Breaking them to fix the bundle path would trade one outage for
    another."""

    def test_seeding_without_a_bundle_still_works_without_the_secret(
            self, client, secret_configured):
        res = _seed(client, {'tenant_id': 'desktop-demo'})
        assert res.status_code == 201

    def test_a_non_public_tenant_is_still_refused_by_the_mint_gate(
            self, client, secret_configured):
        """The pre-existing protection, pinned so this change cannot loosen
        it: a private tenant needs the secret even for the built-in set."""
        res = _seed(client, {'tenant_id': 'someone-elses-tenant'})
        assert res.status_code == 403
