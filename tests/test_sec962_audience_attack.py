"""Security re-review of #962 at 160e7f8: attacks on the audience rule.

`validate_step_up_token` now checks `aud` both ways: a gate that asks for an
audience needs exactly that one, and a gate that asks for none refuses a
token that carries one. Read gates take READ_SCOPES. These tests attack the
rule from outside and check that tokens with no `aud` keep working.

MUTATION: in r6/stepup.py put the audience check back to
`require_audience is not None and payload.get('aud') != require_audience`.
The audience-free gate tests below then go red.

Synthetic tenants and data only.
"""

import base64
import hashlib
import hmac
import json
import os
import time

import pytest

from r6.stepup import generate_step_up_token, validate_step_up_token

TENANT = 'sec962-aud-a'
OTHER = 'sec962-aud-b'
AUD_MISMATCH = 'Token audience mismatch'


def _signed(payload):
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':')).encode()).decode()
    sig = hmac.new(os.environ['STEP_UP_SECRET'].encode(), body.encode(),
                   hashlib.sha256).hexdigest()
    return f'{body}.{sig}'


def _payload(tenant=TENANT, **extra):
    p = {'exp': int(time.time()) + 300, 'tid': tenant, 'sub': 'sec962',
         'nonce': os.urandom(8).hex()}
    p.update(extra)
    return p


# ---------------------------------------------------------------------------
# 1. aud shapes at audience-free gates (write and read)
# ---------------------------------------------------------------------------

AUD_SHAPES = ['action-approval', 'checkin-export', 'curatr', '',
              ' ', 'Action-Approval', ['action-approval'], [], {}, 0, False,
              'action-approval​']


@pytest.mark.parametrize('aud', AUD_SHAPES, ids=repr)
@pytest.mark.parametrize('require_scope', ['write', None])
def test_any_aud_claim_is_refused_at_an_audience_free_gate(aud,
                                                           require_scope):
    token = _signed(_payload(aud=aud))
    assert validate_step_up_token(token, TENANT,
                                  require_scope=require_scope) == (
        False, AUD_MISMATCH)


@pytest.mark.parametrize('require_scope', ['write', None])
def test_null_aud_is_the_same_as_no_aud(require_scope):
    """By design: `"aud": null` is an absent claim. No minter writes null
    (the generator drops a falsy audience), so only the secret holder can."""
    assert validate_step_up_token(_signed(_payload(aud=None)), TENANT,
                                  require_scope=require_scope) == (True, None)


# ---------------------------------------------------------------------------
# 2. aud shapes at the gate that asks for action-approval
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('aud', [
    None, '', ['action-approval'], 'action-approval ', 'ACTION-APPROVAL',
    'action‐approval', {'aud': 'action-approval'},
], ids=repr)
def test_only_the_exact_audience_passes_an_audience_gate(aud):
    claims = {'op': 'x'}
    if aud is not None:
        claims['aud'] = aud
    token = _signed(_payload(**claims))
    assert validate_step_up_token(token, TENANT,
                                  require_audience='action-approval',
                                  require_operation='x') == (
        False, AUD_MISMATCH)


def test_an_op_without_aud_never_passes_an_audience_gate():
    token = _signed(_payload(op='confirm:anything'))
    assert validate_step_up_token(token, TENANT,
                                  require_audience='action-approval',
                                  require_operation='confirm:anything') == (
        False, AUD_MISMATCH)


def test_an_op_without_aud_is_an_ordinary_token_elsewhere():
    """Characterization: `op` is checked only where a gate asks for one, so
    a token with an op and no aud acts as an unscoped tenant token. No
    minter produces this shape (the approval credential always carries
    both), so it is not reachable without the secret."""
    token = _signed(_payload(op='confirm:anything'))
    assert validate_step_up_token(token, TENANT) == (True, None)


# ---------------------------------------------------------------------------
# 3. The approval credential at confirm/decline: wrong action, wrong tenant
# ---------------------------------------------------------------------------

def _awaiting_action(app, tenant=TENANT):
    from models import db
    from r6.actions.models import ProposedAction
    with app.app_context():
        action = ProposedAction(
            tenant_id=tenant, kind='sms',
            payload={'phone': '+15555550100', 'body': 'synthetic'})
        action.status = 'awaiting_confirmation'
        db.session.add(action)
        db.session.commit()
        return action.id, action.payload_json


def _approval_token(tenant, action_id, payload_json):
    from r6.actions.confirmations import (ACTION_APPROVAL_AUDIENCE,
                                          approval_operation)
    return generate_step_up_token(
        tenant, agent_id='human-approval', audience=ACTION_APPROVAL_AUDIENCE,
        operation=approval_operation(action_id, payload_json))


def _status(app, action_id):
    from r6.actions.models import ProposedAction
    with app.app_context():
        return ProposedAction.query.filter_by(id=action_id).first().status


@pytest.mark.parametrize('route,body', [
    ('confirm', {'approved_via': 'dashboard'}),
    ('decline', {'declined_via': 'dashboard'}),
])
def test_an_approval_for_one_action_cannot_answer_another(app, client, route,
                                                           body):
    x_id, x_payload = _awaiting_action(app)
    y_id, _ = _awaiting_action(app)
    token = _approval_token(TENANT, x_id, x_payload)
    resp = client.post(f'/r6/actions/{y_id}/{route}', json=body, headers={
        'X-Tenant-Id': TENANT, 'X-Step-Up-Token': token})
    assert resp.status_code == 401
    assert _status(app, y_id) == 'awaiting_confirmation'
    # The refused attempt did not burn X's single-use credential.
    ok = client.post(f'/r6/actions/{x_id}/decline',
                     json={'declined_via': 'dashboard'},
                     headers={'X-Tenant-Id': TENANT,
                              'X-Step-Up-Token': token})
    assert ok.status_code == 200
    assert _status(app, x_id) == 'declined'


def test_an_approval_for_another_tenant_cannot_answer(app, client):
    y_id, y_payload = _awaiting_action(app, tenant=TENANT)
    token = _approval_token(OTHER, y_id, y_payload)
    resp = client.post(f'/r6/actions/{y_id}/confirm',
                       json={'approved_via': 'dashboard'},
                       headers={'X-Tenant-Id': TENANT,
                                'X-Step-Up-Token': token})
    assert resp.status_code == 401
    assert _status(app, y_id) == 'awaiting_confirmation'


def test_the_approval_credential_still_confirms_its_own_action(
        app, client, fake_providers):
    """Positive control for F1: the legitimate CareAgents shape still runs."""
    x_id, x_payload = _awaiting_action(app)
    token = _approval_token(TENANT, x_id, x_payload)
    resp = client.post(f'/r6/actions/{x_id}/confirm',
                       json={'approved_via': 'review-page'},
                       headers={'X-Tenant-Id': TENANT,
                                'X-Step-Up-Token': token})
    assert resp.status_code != 401, resp.get_data(as_text=True)
    assert _status(app, x_id) != 'awaiting_confirmation'
    replay = client.post(f'/r6/actions/{x_id}/confirm',
                         json={'approved_via': 'review-page'},
                         headers={'X-Tenant-Id': TENANT,
                                  'X-Step-Up-Token': token})
    assert replay.status_code in (401, 409)


# ---------------------------------------------------------------------------
# 4. Audience-bound tokens on general gates (F1) and checkin-export on reads
# ---------------------------------------------------------------------------

def _seed_patient(client):
    resp = client.post('/r6/fhir/Patient', json={
        'resourceType': 'Patient', 'gender': 'unknown'}, headers={
            'X-Tenant-Id': TENANT, 'X-Human-Confirmed': 'true',
            'X-Step-Up-Token': generate_step_up_token(TENANT)})
    assert resp.status_code == 201
    return resp.get_json()['id']


AUD_TOKENS = {
    'action-approval': lambda: generate_step_up_token(
        TENANT, agent_id='human-approval', audience='action-approval',
        operation='anything'),
    'checkin-export': lambda: generate_step_up_token(
        TENANT, audience='checkin-export'),
    'checkin-export+checkin': lambda: generate_step_up_token(
        TENANT, audience='checkin-export', scope='checkin'),
    'checkin-export+read': lambda: generate_step_up_token(
        TENANT, audience='checkin-export', scope='read'),
    'checkin-scope': lambda: generate_step_up_token(TENANT, scope='checkin'),
}


@pytest.mark.parametrize('name', list(AUD_TOKENS))
def test_an_audience_or_narrow_token_reads_nothing(app, client, monkeypatch,
                                                   name):
    pid = _seed_patient(client)
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    h = {'X-Tenant-Id': TENANT, 'X-Step-Up-Token': AUD_TOKENS[name]()}
    for path in (f'/r6/fhir/Patient/{pid}', '/r6/fhir/Patient',
                 '/r6/fhir/AuditEvent', '/r6/fhir/Observation'):
        assert client.get(path, headers=h).status_code == 401, path
    # Bearer alias too.
    bh = {'X-Tenant-Id': TENANT,
          'Authorization': 'Bearer ' + AUD_TOKENS[name]()}
    assert client.get(f'/r6/fhir/Patient/{pid}', headers=bh).status_code == 401
    # Operator-only since SEC941-PRE1: no tenant token, audience-bound or
    # not, opens it (with a secret configured; unset is open outside
    # production, like every internal-secret surface).
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', 'sec962-operator-secret')
    assert client.get('/command-center/api/system',
                      headers=h).status_code == 403


@pytest.mark.parametrize('name', list(AUD_TOKENS))
def test_an_audience_or_narrow_token_writes_nothing(client, name):
    resp = client.post('/r6/fhir/Patient', json={
        'resourceType': 'Patient'}, headers={
            'X-Tenant-Id': TENANT, 'X-Human-Confirmed': 'true',
            'X-Step-Up-Token': AUD_TOKENS[name]()})
    assert resp.status_code == 401
    conv = client.post('/command-center/api/conversations', json={
        'tenant_id': TENANT, 'role': 'user', 'text': 'synthetic'},
        headers={'X-Step-Up-Token': AUD_TOKENS[name]()})
    assert conv.status_code == 401


@pytest.mark.parametrize('name', list(AUD_TOKENS))
def test_the_action_status_predicate_withholds_the_full_record(app, client,
                                                               name):
    x_id, _ = _awaiting_action(app)
    full = client.get(f'/r6/actions/{x_id}', headers={
        'X-Tenant-Id': TENANT,
        'X-Step-Up-Token': generate_step_up_token(TENANT)}).get_json()
    got = client.get(f'/r6/actions/{x_id}', headers={
        'X-Tenant-Id': TENANT, 'X-Step-Up-Token': AUD_TOKENS[name]()})
    body = got.get_data(as_text=True)
    assert '+15555550100' in json.dumps(full)  # control: full record exists
    assert '+15555550100' not in body
    assert 'synthetic' not in body


# ---------------------------------------------------------------------------
# 5. Tokens with no aud keep working (CareAgents, MCP, Fasten, wearables)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('label,kwargs,can_write', [
    ('careagents / mint endpoint unscoped', {}, True),
    ('mcp / mint endpoint read', {'scope': 'read'}, False),
    ('fasten patient-connect', {'scope': 'read',
                                'agent_id': 'patient-connect'}, False),
    ('wearables poller', {'agent_id': 'wearable-sync',
                          'ttl_seconds': 300}, True),
    ('explicit write', {'scope': 'write'}, True),
])
def test_tokens_without_aud_keep_their_capability(app, client, monkeypatch,
                                                  label, kwargs, can_write):
    pid = _seed_patient(client)
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    tok = generate_step_up_token(TENANT, **kwargs)
    read = client.get(f'/r6/fhir/Patient/{pid}', headers={
        'X-Tenant-Id': TENANT, 'X-Step-Up-Token': tok})
    assert read.status_code == 200, label
    write = client.post('/r6/fhir/Patient', json={
        'resourceType': 'Patient'}, headers={
            'X-Tenant-Id': TENANT, 'X-Human-Confirmed': 'true',
            'X-Step-Up-Token': tok})
    assert (write.status_code == 201) is can_write, (label,
                                                     write.status_code)


def test_the_approval_token_mint_needs_the_internal_secret(app, client,
                                                            monkeypatch):
    """F1's not-live claim: only a holder of the internal secret can get the
    approval credential, on public tenants too, and an unset secret
    refuses everyone."""
    x_id, _ = _awaiting_action(app)
    for tenant in (TENANT, 'desktop-demo'):
        for secret_env, header in ((None, None), ('s3cret', None),
                                   ('s3cret', 'wrong'), ('', '')):
            if secret_env is None:
                monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET',
                                   raising=False)
            else:
                monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', secret_env)
            h = {'X-Tenant-Id': tenant}
            if header is not None:
                h['X-Internal-Secret'] = header
            r = client.post(f'/r6/actions/{x_id}/approval-token', headers=h)
            assert r.status_code == 403, (tenant, secret_env, header,
                                          r.status_code)
            assert 'token' not in (r.get_json() or {})
