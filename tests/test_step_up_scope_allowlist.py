"""A write gate accepts an allow-listed write scope, and nothing else (FHI-57).

validate_step_up_token used to refuse `scope == 'read'` on a write gate and
pass every other value. Nothing mints another scope today, so no token in
circulation could use it, but the first new scope string anybody minted
(a check-in export was about to) would have authorized every write in the
tenant. The gate is now an allow-list: r6.stepup.WRITE_SCOPES.

The per-route half of this is tests/test_write_guard_matrix.py
test_step_up_row_refuses_an_unknown_scope; this file pins the validator.

MUTATION: put the gate back to `payload.get('scope') == 'read'` ->
test_an_unknown_scope_cannot_write and its siblings redden.
MUTATION: drop the isinstance guard in _is_write_scope ->
test_a_non_string_scope_is_refused_not_raised reddens (TypeError).
"""

import base64
import hashlib
import hmac
import json
import os
import time

import pytest

from r6 import stepup
from r6.stepup import generate_step_up_token, validate_step_up_token

TENANT = 'scope-allowlist-tenant'


def _signed(payload):
    """A token with an arbitrary payload, signed with the test secret.

    generate_step_up_token omits a falsy scope and only takes strings, so the
    shapes it cannot produce are built by hand.
    """
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':')).encode()).decode()
    sig = hmac.new(os.environ['STEP_UP_SECRET'].encode(), body.encode(),
                   hashlib.sha256).hexdigest()
    return f'{body}.{sig}'


def _payload(**extra):
    payload = {'exp': int(time.time()) + 300, 'tid': TENANT, 'sub': 'test',
               'nonce': 'n'}
    payload.update(extra)
    return payload


def test_the_allow_list_is_unscoped_or_write():
    """Widening the list is a decision; this line is where it shows up."""
    assert stepup.WRITE_SCOPES == frozenset({None, 'write'})


def test_an_explicit_write_scope_can_write():
    token = generate_step_up_token(TENANT, scope='write')
    assert validate_step_up_token(token, TENANT) == (True, None)


def test_a_token_without_a_scope_can_still_write():
    """Every write minter today (CareAgents, seed, wearables, the approval
    credential) omits the claim. They must keep working."""
    token = generate_step_up_token(TENANT)
    assert validate_step_up_token(token, TENANT) == (True, None)


def test_a_read_scoped_token_keeps_its_own_sentence():
    """Clients already parse this sentence; the fix must not reword it."""
    token = generate_step_up_token(TENANT, scope='read')
    assert validate_step_up_token(token, TENANT) == (
        False, 'Read-scoped token cannot authorize this operation')


@pytest.mark.parametrize('scope', ['checkin', 'admin', 'write ', 'READ',
                                   'read write'])
def test_an_unknown_scope_cannot_write(scope):
    token = generate_step_up_token(TENANT, scope=scope)
    assert validate_step_up_token(token, TENANT) == (
        False, 'Token scope cannot authorize this operation')


def test_an_empty_scope_claim_cannot_write():
    """An empty string is a claim, not an absent one."""
    token = _signed(_payload(scope=''))
    valid, error = validate_step_up_token(token, TENANT)
    assert valid is False
    assert error == 'Token scope cannot authorize this operation'


@pytest.mark.parametrize('claim', [['write'], {'write': True}, 1, True])
def test_a_non_string_scope_is_refused_not_raised(claim):
    token = _signed(_payload(scope=claim))
    valid, error = validate_step_up_token(token, TENANT)
    assert valid is False
    assert error == 'Token scope cannot authorize this operation'


def test_the_read_gate_is_unchanged():
    """require_scope=None (Scope.TENANT_BOUND) is not what FHI-57 is about;
    a read-scoped token still reads."""
    token = generate_step_up_token(TENANT, scope='read')
    assert validate_step_up_token(token, TENANT, require_scope=None) == (
        True, None)


def test_the_new_sentence_reaches_the_caller():
    """It describes the caller's own token, so it is published like the
    read-scoped sentence beside it."""
    from r6 import access
    reason = 'Token scope cannot authorize this operation'
    assert access.public_step_up_reason(reason) == reason


# --- audience, both ways (#963 security review F1) --------------------------

@pytest.mark.parametrize('require_scope', ['write', None])
def test_a_gate_without_an_audience_refuses_a_token_with_one(require_scope):
    """The approval credential and the check-in token are purpose-bound;
    neither is a general credential for the tenant, read or write.

    MUTATION: put back `if require_audience is not None and ...` -> red."""
    token = generate_step_up_token(TENANT, audience='action-approval',
                                   operation='op')
    assert validate_step_up_token(token, TENANT,
                                  require_scope=require_scope) == (
        False, 'Token audience mismatch')


def test_a_gate_with_an_audience_still_accepts_exactly_it():
    token = generate_step_up_token(TENANT, audience='action-approval',
                                   operation='op')
    assert validate_step_up_token(
        token, TENANT, require_audience='action-approval',
        require_operation='op') == (True, None)


# --- read gates and malformed payloads (#962 security review) --------------

def test_the_read_allow_list_is_none_read_or_write():
    assert stepup.READ_SCOPES == frozenset({None, 'read', 'write'})


@pytest.mark.parametrize('claim', ['checkin', '', ['read'], {'x': 1}])
def test_a_read_gate_refuses_an_unknown_scope(claim):
    token = _signed(_payload(scope=claim))
    assert validate_step_up_token(token, TENANT, require_scope=None) == (
        False, 'Token scope cannot authorize this operation')


def test_an_unknown_require_scope_refuses_every_token():
    """MUTATION: drop the final `else` branch of the scope gate -> red."""
    token = generate_step_up_token(TENANT)
    assert validate_step_up_token(token, TENANT, require_scope='admin') == (
        False, 'Token scope cannot authorize this operation')


@pytest.mark.parametrize('payload', [[1, 2], 'text', 7, None])
def test_a_non_object_payload_is_malformed_not_a_crash(payload):
    """MUTATION: drop the isinstance(payload, dict) check -> AttributeError."""
    assert validate_step_up_token(_signed(payload), TENANT) == (
        False, 'Malformed token payload')
