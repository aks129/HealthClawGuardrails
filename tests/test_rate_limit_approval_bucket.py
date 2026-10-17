"""An approval credential keeps confirm/decline in the tenant's bucket.

#962 made every audience-free gate refuse an audience-bound token, and the
rate limiter's "did this request prove its tenant?" question is one of them.
So every confirm and decline (which carry the action-approval credential)
fell to the per-IP bucket: from CareAgents' single egress IP, one 120/min
bucket for every tenant, plus an INFO refusal line per legitimate approval
(#962 security review, Low).

The fix is narrow: on those two endpoints only, the key lookup asks for the
action-approval audience. Everywhere else an approval credential still
proves nothing.

MUTATION: drop the endpoint branch in r6/rate_limit.py
_tenant_claim_is_authenticated -> the two tenant-key tests go red.
"""

import logging

import pytest

from r6.actions.confirmations import ACTION_APPROVAL_AUDIENCE
from r6.rate_limit import rate_limit_key
from r6.stepup import generate_step_up_token

TENANT = 'approval-bucket-tenant'
IP = '203.0.113.44'


def _approval_token(tenant=TENANT):
    return generate_step_up_token(tenant, agent_id='human-approval',
                                  audience=ACTION_APPROVAL_AUDIENCE,
                                  operation='approve:some-action:digest')


def _key(app, path, token, method='POST'):
    with app.test_request_context(path, method=method, headers={
            'X-Tenant-Id': TENANT, 'X-Step-Up-Token': token},
            environ_base={'REMOTE_ADDR': IP}):
        return rate_limit_key()


@pytest.mark.parametrize('verb', ['confirm', 'decline'])
def test_an_approval_credential_keys_confirm_and_decline_by_tenant(app,
                                                                   verb):
    assert _key(app, f'/r6/actions/act-1/{verb}', _approval_token()) == TENANT


@pytest.mark.parametrize('verb', ['confirm', 'decline'])
def test_the_key_lookup_logs_no_refusal_for_an_approval(app, caplog, verb):
    with caplog.at_level(logging.INFO, logger='r6.access'):
        _key(app, f'/r6/actions/act-1/{verb}', _approval_token())
    assert 'step-up refused' not in caplog.text


@pytest.mark.parametrize('verb', ['confirm', 'decline'])
def test_another_tenants_approval_credential_does_not_key_by_tenant(app,
                                                                    verb):
    assert _key(app, f'/r6/actions/act-1/{verb}',
                _approval_token('someone-else')) == f'ip:{IP}'


@pytest.mark.parametrize('path,method', [
    ('/r6/actions/act-1/commit', 'POST'),
    ('/r6/actions/act-1/review', 'POST'),
    ('/r6/actions/act-1', 'GET'),
    ('/r6/fhir/Patient', 'POST'),
    ('/r6/fhir/Patient/p1', 'GET'),
])
def test_an_approval_credential_proves_nothing_anywhere_else(app, path,
                                                             method):
    """The exception is two endpoints wide, not an audience allow-list."""
    assert _key(app, path, _approval_token(), method=method) == f'ip:{IP}'


@pytest.mark.parametrize('verb', ['confirm', 'decline'])
def test_a_general_token_still_keys_confirm_by_tenant(app, verb):
    """An ordinary tenant token keeps working at the key lookup, though the
    gate itself refuses it on these routes."""
    assert _key(app, f'/r6/actions/act-1/{verb}',
                generate_step_up_token(TENANT)) == TENANT
