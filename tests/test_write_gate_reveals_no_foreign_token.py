"""A write refusal never says the token belongs to a different tenant (#478).

Before slice 6, the FHIR write gates in r6/routes.py answered a rejected token
with the validator's raw reason interpolated into the body:

    return _operation_outcome('error', 'security',
                              f'Step-up token rejected: {err}'), 401

One of the eleven values `err` takes is 'Token tenant mismatch'. Presenting a
correctly-signed token for tenant A while claiming tenant B got that answer
back, which separates "a real credential issued elsewhere" from "junk" — the
distinction a caller probing with a token they should not have is trying to
draw. r6/read_auth.py:262 has always withheld it; these sites did not.

The kernel classifies that reason as withheld (r6/access._WITHHELD_REASONS),
so migrating the gates closes the leak. This file is the pin: it fails if a
write path ever again tells a caller *why* in a way that describes someone
else's credential.

It asserts on the REFUSAL TEXT rather than on which function the handler
calls. A future rewrite that stops using require_grant is fine; a future
rewrite that starts naming the tenant mismatch is not.

MUTATION: add 'Token tenant mismatch' to r6/access._PUBLIC_REASONS -> every
test here reddens. Verified.
"""

import json

import pytest

from r6.stepup import generate_step_up_token

#: Substrings that would mean the response described the OTHER tenant's token
#: rather than the caller's request. 'mismatch' alone is not banned — an
#: audience or operation mismatch is the caller's own token and is published
#: deliberately (owner ruling, 2026-08-10).
FORBIDDEN = ('tenant mismatch', 'Tenant mismatch', 'Token tenant')


def _foreign_token_headers(tenant_id):
    """A valid token for someone else, presented as this tenant."""
    return {'X-Tenant-Id': tenant_id,
            'X-Step-Up-Token': generate_step_up_token('some-other-tenant'),
            'Content-Type': 'application/json'}


def _assert_refused_without_naming_the_other_tenant(resp):
    assert resp.status_code == 401, (
        f'a foreign token must be refused, got {resp.status_code}')
    body = resp.get_data(as_text=True)
    for phrase in FORBIDDEN:
        assert phrase not in body, (
            f'the refusal contains {phrase!r}, which tells the caller their '
            'token is valid for a different tenant (#478)')


class TestForeignTokenIsRefusedWithoutExplanation:

    def test_create(self, client, tenant_id):
        resp = client.post(
            '/r6/fhir/Patient',
            data=json.dumps({'resourceType': 'Patient'}),
            headers=_foreign_token_headers(tenant_id))
        _assert_refused_without_naming_the_other_tenant(resp)

    def test_update(self, client, tenant_id, sample_patient, auth_headers):
        created = client.post('/r6/fhir/Patient',
                              data=json.dumps(sample_patient),
                              content_type='application/json',
                              headers=auth_headers)
        assert created.status_code == 201
        pid = created.get_json()['id']
        resp = client.put(f'/r6/fhir/Patient/{pid}',
                          data=json.dumps({'resourceType': 'Patient',
                                           'id': pid}),
                          headers=_foreign_token_headers(tenant_id))
        _assert_refused_without_naming_the_other_tenant(resp)

    def test_share_bundle(self, client, tenant_id):
        resp = client.post('/r6/fhir/$share-bundle',
                           data=json.dumps({'patient_id': 'anything'}),
                           headers=_foreign_token_headers(tenant_id))
        _assert_refused_without_naming_the_other_tenant(resp)


class TestTheCallersOwnReasonSurvives:
    """The ruling's other half. Withholding one reason must not quietly
    collapse the rest back into 'Invalid step-up token'."""

    @pytest.mark.parametrize('path,method', [
        ('/r6/fhir/Patient', 'post'),
        ('/r6/fhir/$share-bundle', 'post'),
    ])
    def test_an_expired_token_says_it_expired(self, client, tenant_id,
                                              path, method):
        expired = generate_step_up_token(tenant_id, ttl_seconds=-10)
        resp = getattr(client, method)(
            path,
            data=json.dumps({'resourceType': 'Patient'}),
            headers={'X-Tenant-Id': tenant_id,
                     'X-Step-Up-Token': expired,
                     'Content-Type': 'application/json'})
        assert resp.status_code == 401
        assert 'expired' in resp.get_data(as_text=True).lower(), (
            'an expired token is the caller\'s own; the ruling says tell them')
