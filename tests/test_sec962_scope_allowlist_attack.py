"""Security review of #962 (FHI-57): attacks on the write-scope allow-list.

#962 turned the write gate in r6/stepup.validate_step_up_token from a
denylist of one ('read') into an allow-list, WRITE_SCOPES = {None, 'write'}.
These tests attack that gate from the outside. They pass on the PR head and
the action-bound and non-matrix rows fail on the pre-#962 gate.

MUTATION: in r6/stepup.py put the write gate back to
`payload.get('scope') == 'read'` -> the spelling, unicode, confirm/decline
and non-matrix-gate tests below go red.

Synthetic tenants and data only.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time

import pytest

from r6.stepup import generate_step_up_token, validate_step_up_token

TENANT = 'sec962-tenant'
MINT_SECRET = 'sec962-internal-mint-secret'
NEW_SENTENCE = 'Token scope cannot authorize this operation'
READ_SENTENCE = 'Read-scoped token cannot authorize this operation'


def _signed(payload):
    """Sign an arbitrary payload with the test secret: the shapes no minter
    can produce, which only a holder of STEP_UP_SECRET could forge."""
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
# 1. The validator: spellings, lookalikes, types
# ---------------------------------------------------------------------------

LOOKALIKES = [
    'Write', 'WRITE', 'wRITE', ' write', 'write ', 'write\n', '\twrite',
    'write\x00', 'write​', '﻿write',
    'wrіte',            # Cyrillic i
    'ｗrite',            # fullwidth w
    'writе',            # Cyrillic e
    'wʀite',            # small capital R
    'write,read', 'read write', 'write read', 'fhir.write', 'user/*.write',
    'None', 'null', 'true', '*', 'admin', 'checkin', 'export', '',
    'Read', 'READ', 'read ',
]


@pytest.mark.parametrize('claim', LOOKALIKES, ids=repr)
def test_no_spelling_of_write_but_write_passes_a_write_gate(claim):
    valid, error = validate_step_up_token(_signed(_payload(scope=claim)),
                                          TENANT)
    assert valid is False
    assert error == NEW_SENTENCE
    assert claim.strip() == '' or claim not in (error or '')


@pytest.mark.parametrize('claim', [
    ['write'], ['read'], [None], [], {}, {'write': True}, 0, 1, 1.0, True,
    False, {'scope': 'write'}, [['write']],
], ids=repr)
def test_a_non_string_claim_is_refused_without_raising(claim):
    valid, error = validate_step_up_token(_signed(_payload(scope=claim)),
                                          TENANT)
    assert (valid, error) == (False, NEW_SENTENCE)


def test_json_null_scope_is_the_same_as_no_scope():
    """By design, not a finding: `"scope": null` and an absent claim are the
    same legacy full-capability token. No minter can write `null` (the
    generator drops a falsy scope), so only a holder of the secret can."""
    assert validate_step_up_token(_signed(_payload(scope=None)), TENANT) == (
        True, None)
    assert validate_step_up_token(_signed(_payload()), TENANT) == (True, None)


def test_the_exact_write_claim_passes():
    assert validate_step_up_token(_signed(_payload(scope='write')),
                                  TENANT) == (True, None)


def test_the_read_sentence_is_only_for_the_exact_read_claim():
    assert validate_step_up_token(_signed(_payload(scope='read')),
                                  TENANT) == (False, READ_SENTENCE)


def test_duplicate_scope_keys_take_the_last_value_and_still_need_the_secret():
    """json.loads keeps the last duplicate. A forged body is still unsigned:
    re-signing needs STEP_UP_SECRET, and a resigned read->null token is just
    a token the secret holder could have minted anyway."""
    body = ('{"exp":%d,"tid":"%s","sub":"x","nonce":"n","scope":"read",'
            '"scope":"checkin"}' % (int(time.time()) + 300, TENANT))
    b64 = base64.urlsafe_b64encode(body.encode()).decode()
    assert validate_step_up_token(b64 + '.' + '0' * 64, TENANT) == (
        False, 'Invalid token signature')
    sig = hmac.new(os.environ['STEP_UP_SECRET'].encode(), b64.encode(),
                   hashlib.sha256).hexdigest()
    assert validate_step_up_token(f'{b64}.{sig}', TENANT) == (
        False, NEW_SENTENCE)


def test_a_read_token_re_scoped_without_the_secret_fails_the_signature():
    """Token forgery assumption: the scope claim is only as good as the HMAC.
    Swapping 'read' for 'write' in the payload half breaks the signature."""
    token = generate_step_up_token(TENANT, scope='read')
    body, sig = token.rsplit('.', 1)
    payload = json.loads(base64.urlsafe_b64decode(body))
    payload['scope'] = 'write'
    forged = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':')).encode()).decode()
    assert validate_step_up_token(f'{forged}.{sig}', TENANT) == (
        False, 'Invalid token signature')


def test_a_wearables_oauth_state_is_not_a_step_up_token():
    """Cross-scheme: r6/wearables/routes.py signs its OAuth state with the
    same STEP_UP_SECRET (when WEARABLES_OAUTH_STATE_SECRET is unset) in the
    same base64url(json).hexsig wire shape. Its payload carries tenant_id,
    not tid, so it fails the tenant binding. If that state ever gains a
    `tid` claim, it becomes an unscoped write token, which this pins."""
    from r6.wearables.routes import _sign_state
    for n in range(4):  # vary length so some bodies need no padding
        state = _sign_state({'tenant_id': TENANT, 'provider': 'oura',
                             'ow_user_id': 'u' * n,
                             'exp': int(time.time()) + 300})
        valid, _ = validate_step_up_token(state, TENANT)
        assert valid is False


# ---------------------------------------------------------------------------
# 2. The only HTTP minter refuses every scope but read/absent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('scope', [
    'write', 'checkin', '', 'READ', 'read ', 'wrіte', ['read'], {},
    0, False, 'read write',
], ids=repr)
def test_the_mint_endpoint_refuses_any_other_scope(client, monkeypatch,
                                                   scope):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', MINT_SECRET)
    resp = client.post('/r6/fhir/internal/step-up-token',
                       json={'tenant_id': TENANT, 'scope': scope},
                       headers={'X-Internal-Secret': MINT_SECRET})
    assert resp.status_code == 400
    assert 'token' not in (resp.get_json() or {})


def test_the_mint_endpoint_mints_read_or_unscoped_only(client, monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', MINT_SECRET)
    for scope, write_ok in ((None, True), ('read', False)):
        resp = client.post('/r6/fhir/internal/step-up-token',
                           json={'tenant_id': TENANT, 'scope': scope},
                           headers={'X-Internal-Secret': MINT_SECRET})
        assert resp.status_code == 200
        token = resp.get_json()['token']
        assert validate_step_up_token(token, TENANT)[0] is write_ok


# ---------------------------------------------------------------------------
# 3. confirm/decline: the matrix row is refused on audience for these two.
#    Here the credential is correctly action-bound, so only scope refuses it.
# ---------------------------------------------------------------------------

def _awaiting_action(app):
    from models import db
    from r6.actions.models import ProposedAction
    with app.app_context():
        action = ProposedAction(
            tenant_id=TENANT, kind='sms',
            payload={'phone': '+15555550100', 'body': 'synthetic'})
        action.status = 'awaiting_confirmation'
        db.session.add(action)
        db.session.commit()
        return action.id, action.payload_json


def _approval_token(action_id, payload_json, scope=None):
    from r6.actions.confirmations import (ACTION_APPROVAL_AUDIENCE,
                                          approval_operation)
    return generate_step_up_token(
        TENANT, agent_id='human-approval', audience=ACTION_APPROVAL_AUDIENCE,
        operation=approval_operation(action_id, payload_json), scope=scope)


def _status(app, action_id):
    from r6.actions.models import ProposedAction
    with app.app_context():
        return ProposedAction.query.filter_by(id=action_id).first().status


@pytest.mark.parametrize('route,body', [
    ('confirm', {'approved_via': 'dashboard'}),
    ('decline', {'declined_via': 'dashboard'}),
])
def test_an_action_bound_credential_with_an_unknown_scope_is_refused(
        app, client, route, body):
    action_id, payload_json = _awaiting_action(app)
    token = _approval_token(action_id, payload_json, scope='checkin')
    resp = client.post(f'/r6/actions/{action_id}/{route}',
                       headers={'X-Tenant-Id': TENANT,
                                'X-Step-Up-Token': token}, json=body)
    assert resp.status_code == 401, resp.get_data(as_text=True)
    assert 'checkin' not in resp.get_data(as_text=True)
    assert _status(app, action_id) == 'awaiting_confirmation'


def test_the_same_credential_unscoped_still_declines(app, client):
    """Positive control: the refusal above is the scope, not the setup."""
    action_id, payload_json = _awaiting_action(app)
    token = _approval_token(action_id, payload_json)
    resp = client.post(f'/r6/actions/{action_id}/decline',
                       headers={'X-Tenant-Id': TENANT,
                                'X-Step-Up-Token': token},
                       json={'declined_via': 'dashboard'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _status(app, action_id) == 'declined'


# ---------------------------------------------------------------------------
# 4. Scope.WRITE gates the write-guard matrix does not list as STEP_UP rows
# ---------------------------------------------------------------------------

NON_MATRIX_GATES = [
    ('POST', '/command-center/api/conversations',
     {'tenant_id': TENANT, 'role': 'user', 'text': 'synthetic'}),
    ('POST', '/command-center/api/tasks',
     {'tenant_id': TENANT, 'agent_id': 'sally', 'title': 'synthetic'}),
    ('POST', '/command-center/api/generate-link', {'tenant_id': TENANT}),
    ('POST', '/command-center/api/runs',
     {'tenant_id': TENANT, 'message_id': 'sec962-msg'}),
    ('POST', '/r6/fhir/Bundle/$ingest-context',
     {'resourceType': 'Bundle', 'type': 'collection', 'entry': []}),
]


@pytest.mark.parametrize('method,path,body', NON_MATRIX_GATES,
                         ids=[p for _, p, _ in NON_MATRIX_GATES])
def test_a_non_matrix_write_gate_refuses_an_unknown_scope(
        client, monkeypatch, method, path, body):
    monkeypatch.setenv('READ_AUTH_ENABLED', 'true')
    bad = client.open(path, method=method, json=body, headers={
        'X-Tenant-Id': TENANT, 'Content-Type': 'application/json',
        'X-Step-Up-Token': generate_step_up_token(TENANT, scope='checkin')})
    assert bad.status_code in (401, 403), (
        f'{path} answered {bad.status_code} to a checkin-scoped token')
    assert 'checkin' not in bad.get_data(as_text=True)

    good = client.open(path, method=method, json=body, headers={
        'X-Tenant-Id': TENANT, 'Content-Type': 'application/json',
        'X-Step-Up-Token': generate_step_up_token(TENANT)})
    assert good.status_code not in (401, 403), (
        f'positive control: {path} refused an unscoped write token '
        f'({good.status_code}); the refusal above proves nothing')


# ---------------------------------------------------------------------------
# 5. Log and audit: the refusal never repeats the claim
# ---------------------------------------------------------------------------

def test_a_refused_scope_is_not_logged_or_audited_verbatim(app, client,
                                                           caplog):
    canary = 'scope-canary-7f3a'
    token = _signed(_payload(scope=canary))
    with caplog.at_level(logging.DEBUG):
        resp = client.post('/r6/fhir/Patient', json={
            'resourceType': 'Patient'}, headers={
                'X-Tenant-Id': TENANT, 'X-Step-Up-Token': token,
                'X-Human-Confirmed': 'true'})
    assert resp.status_code == 401
    assert canary not in resp.get_data(as_text=True)
    assert canary not in caplog.text
    assert NEW_SENTENCE in caplog.text
    from r6.models import AuditEventRecord
    with app.app_context():
        rows = AuditEventRecord.query.all()
        assert all(canary not in json.dumps(
            {c.name: str(getattr(r, c.name)) for c in r.__table__.columns})
            for r in rows)


# ---------------------------------------------------------------------------
# 6. Read gates (Scope.TENANT_BOUND): characterization, not a #962 defect
# ---------------------------------------------------------------------------

def test_read_gates_refuse_an_unknown_scope_string():
    """Was a characterization of the latent read-side gap (any scope string
    read everything at every TENANT_BOUND site, including the full action
    record at GET /r6/actions/<id>). #962 now closes it with READ_SCOPES,
    so this pins the refusal instead.

    MUTATION: drop the `elif require_scope is None` branch in
    validate_step_up_token -> red."""
    for claim in ('checkin', 'admin', 'wrіte', ['x']):
        assert validate_step_up_token(_signed(_payload(scope=claim)), TENANT,
                                      require_scope=None) == (
            False, 'Token scope cannot authorize this operation')
    for claim in ('read', 'write'):
        assert validate_step_up_token(_signed(_payload(scope=claim)), TENANT,
                                      require_scope=None) == (True, None)
