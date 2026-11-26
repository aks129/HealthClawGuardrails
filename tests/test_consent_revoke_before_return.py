"""A consent revoked before its return stays revoked.

The consent surface records the grant, then hands the browser the signed
return. A revoke that lands first finds no consent at the engine and gets
404, which the caller reads as "already gone". The engine used to remember
nothing, so the return then stored the consent live while the person's own
list said it was revoked.

A revoke of an unknown id now leaves a revoked tombstone (it still answers
404). The return stores its consent only if the id has no record, in one
step (r6.oauth._oauth_store_add), so a tombstone or an earlier consent is
never overwritten, whichever of the two requests gets there first.

The interleaving tests cut in between a route's read and its write by
wrapping _oauth_store_add. They run on the in-memory store; the Redis form
of the conditional write is covered against a fake client only, and no test
here runs two real requests at once. Synthetic ids only.
"""

from __future__ import annotations

import logging

import pytest

from r6 import oauth
from tests.test_oauth_consent_handoff import (  # noqa: F401  (fixture)
    MCP_RESOURCE, _grant, _params, _park, _pkce, _register, _return,
    _service, handoff)

pytestmark = pytest.mark.usefixtures('handoff')

TENANT = 'consent-race-t1'
REFUSAL_LOG = 'consent_return refused: existing record'


def _exchange(client, reg, code, verifier):
    resp = client.post('/r6/fhir/oauth/token', data={
        'grant_type': 'authorization_code', 'code': code,
        'code_verifier': verifier, 'client_id': reg['client_id'],
        'resource': MCP_RESOURCE})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def _introspect(client, token):
    return client.post('/r6/fhir/oauth/introspect', data={
        'token': token, 'client_id': 'mcp-server',
        'client_secret': 'introspection-secret'}).get_json()


def _revoke(client, consent_id):
    return client.post(f'/r6/fhir/oauth/consent/{consent_id}/revoke',
                       headers=_service())


def _approval(client, reg, consent_id=None):
    """A parked request and the signed approval for it, not yet returned."""
    verifier, challenge = _pkce()
    request_id, _, _ = _park(client, reg, challenge)
    grant, payload = _grant(request_id, tenant_id=TENANT, consent_id=consent_id)
    return grant, payload['consent_id'], verifier


def _record(consent_id):
    return oauth._oauth_store_get('consent', consent_id)


def _consent_rows(client):
    from r6.models import AuditEventRecord
    with client.application.app_context():
        return [(r.event_type, r.outcome, r.resource_id, r.detail)
                for r in AuditEventRecord.query.filter_by(
                    tenant_id=TENANT, resource_type='Consent').all()]


def _cut_in_before_the_write(monkeypatch, cut_in):
    """Run `cut_in` once, just before the next conditional consent write:
    the other request landing between a route's read and its write."""
    real, calls = oauth._oauth_store_add, []

    def _add(kind, key, value, ttl=None):
        if kind == 'consent' and not calls:
            calls.append(key)
            cut_in(key)
        return real(kind, key, value, ttl=ttl)

    monkeypatch.setattr(oauth, '_oauth_store_add', _add)
    return calls


# --- the sequential race ----------------------------------------------------


def test_a_consent_revoked_before_its_return_stays_revoked(client):
    """MUTATION: drop the tombstone write in consent_revoke -> a code is
    issued. Make the return's write unconditional -> a code is issued and
    the tombstone is replaced by a live consent."""
    reg = _register(client)
    grant, consent_id, _ = _approval(client, reg)

    assert _revoke(client, consent_id).status_code == 404

    _, params = _params(_return(client, grant))
    assert params.get('error') == 'access_denied', params
    assert 'code' not in params
    assert set(_record(consent_id)) == {'revoked_at'}, 'the tombstone was replaced'
    assert oauth.consent_is_live(consent_id) is False


def test_an_unrevoked_consent_still_returns_a_code(client):
    """Non-vacuity: the same flow without the early revoke issues a code
    whose token introspects active, and writes no refusal."""
    reg = _register(client)
    grant, consent_id, verifier = _approval(client, reg)

    _, params = _params(_return(client, grant))
    tokens = _exchange(client, reg, params['code'], verifier)
    assert _introspect(client, tokens['access_token'])['active'] is True
    assert oauth.consent_is_live(consent_id) is True
    assert [outcome for _e, outcome, _i, _d in _consent_rows(client)] == ['success']


def test_revoking_after_the_return_still_revokes(client):
    reg = _register(client)
    grant, consent_id, verifier = _approval(client, reg)
    _, params = _params(_return(client, grant))
    tokens = _exchange(client, reg, params['code'], verifier)

    resp = _revoke(client, consent_id)
    assert resp.status_code == 200
    assert resp.get_json() == {'revoked': True, 'consent_id': consent_id}
    assert _introspect(client, tokens['access_token']) == {'active': False}


def test_a_replayed_consent_id_is_refused_and_the_first_consent_is_kept(client):
    """A second approval that reuses a consent id is refused, and the
    consent the first one stored is left exactly as it was.

    MUTATION: make the return's write unconditional -> the replay gets a
    code and the record is rewritten."""
    reg = _register(client)
    first, consent_id, _ = _approval(client, reg)
    _params(_return(client, first))
    stored = dict(_record(consent_id))

    replay, _, _ = _approval(client, reg, consent_id=consent_id)
    _, params = _params(_return(client, replay))
    assert params.get('error') == 'access_denied', params
    assert 'code' not in params
    assert _record(consent_id) == stored
    assert oauth.consent_is_live(consent_id) is True


# --- the two requests interleaved -------------------------------------------


def test_a_revoke_that_lands_inside_the_return_is_not_overwritten(
        client, monkeypatch):
    """The revoke's tombstone lands after the return has started and before
    its consent is written. The write is conditional, so the tombstone
    stands and no code is issued.

    MUTATION: write the consent with a look followed by _oauth_store_set, as
    the first version of this fix did -> the cut-in never runs and a code is
    issued. Make _oauth_store_add overwrite -> the tombstone is replaced."""
    reg = _register(client)
    grant, consent_id, _ = _approval(client, reg)
    calls = _cut_in_before_the_write(
        monkeypatch,
        lambda key: oauth._oauth_store_set('consent', key, {'revoked_at': 1.0}))

    _, params = _params(_return(client, grant))
    assert calls == [consent_id], 'the return never reached its conditional write'
    assert params.get('error') == 'access_denied', params
    assert 'code' not in params
    assert _record(consent_id) == {'revoked_at': 1.0}


def test_a_return_that_lands_inside_the_revoke_is_revoked_not_replaced(
        client, monkeypatch):
    """The revoke read "no record"; the return then stored its consent; the
    revoke's tombstone write comes next. It must not replace the consent
    with a bare tombstone, and the consent must end revoked.

    MUTATION: write the tombstone with _oauth_store_set -> the record loses
    its tenant and the revoke answers 404. Drop the re-read after a refused
    tombstone -> the consent stays live."""
    live = {'tenant_id': TENANT, 'client_id': 'cid-x', 'scopes': ['fhir.read'],
            'granted_at': 1.0, 'revoked_at': None}
    consent_id = 'consent_interleaved_1'
    calls = _cut_in_before_the_write(
        monkeypatch,
        lambda key: oauth._oauth_store_set('consent', key, dict(live)))

    resp = _revoke(client, consent_id)
    assert calls == [consent_id], 'the revoke never reached its conditional write'
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json() == {'revoked': True, 'consent_id': consent_id}
    record = _record(consent_id)
    assert record['tenant_id'] == TENANT and record['client_id'] == 'cid-x'
    assert record['revoked_at']
    assert oauth.consent_is_live(consent_id) is False


# --- the revoke's status sequence -------------------------------------------


def test_revoking_an_unknown_id_twice_answers_404_then_200(client, caplog):
    """The first revoke of an id with no record is 404 and leaves the
    tombstone; the repeat finds the tombstone and answers 200. So a repeat
    reports `revoked: true` for an id that never had a consent.

    MUTATION: drop the tombstone write -> the repeat is 404 too."""
    consent_id = 'consent_never_stored_1'
    with caplog.at_level(logging.INFO, logger='r6.oauth'):
        first = _revoke(client, consent_id)
    assert first.status_code == 404
    assert first.get_json() == {'error': 'not_found'}
    lines = [r.getMessage() for r in caplog.records if 'tombstone' in r.getMessage()]
    assert lines == ['consent_revoke: tombstone stored for an unknown consent id']

    again = _revoke(client, consent_id)
    assert again.status_code == 200
    assert again.get_json() == {'revoked': True, 'consent_id': consent_id}


def test_a_revoke_without_the_service_credential_stores_nothing(client):
    """Only the service credential can leave a tombstone.

    MUTATION: drop the credential check from consent_revoke -> a record is
    stored."""
    consent_id = 'consent_no_credential_1'
    resp = client.post(f'/r6/fhir/oauth/consent/{consent_id}/revoke')
    assert resp.status_code == 403
    assert _record(consent_id) is None


# --- the refusal leaves a record --------------------------------------------


def test_the_refused_return_is_logged_and_audited_without_ids_in_the_log(
        client, caplog):
    """The client sees the same access_denied as a person's "no", so the
    engine records which it was: one log line with no ids, and one failure
    AuditEvent under the tenant the signed grant names.

    MUTATION: drop the logger call -> no line. Drop the add_audit_event
    call on the refusal -> no failure row."""
    reg = _register(client)
    grant, consent_id, _ = _approval(client, reg)
    _revoke(client, consent_id)

    with caplog.at_level(logging.WARNING, logger='r6.oauth'):
        _params(_return(client, grant))

    lines = [r.getMessage() for r in caplog.records if 'consent_return' in r.getMessage()]
    assert lines == [REFUSAL_LOG]
    assert consent_id not in lines[0] and TENANT not in lines[0]
    assert _consent_rows(client) == [(
        'create', 'failure', consent_id,
        f"client_id={reg['client_id']} refused: "
        'consent id already has a record via=careagents')]


# --- the store cannot answer ------------------------------------------------


class _FakeRedis:
    def __init__(self, answer=True, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def set(self, key, value, ex=None, nx=False):
        self.calls.append({'key': key, 'ex': ex, 'nx': nx})
        if self.error is not None:
            raise self.error
        return self.answer


@pytest.mark.parametrize('redis_answer, stored', [(True, True), (None, False)])
def test_the_conditional_write_is_one_redis_set_nx(
        monkeypatch, redis_answer, stored):
    """SET NX answers None when the key exists; that is "not stored".

    MUTATION: drop nx=True -> the call no longer asks Redis for a
    conditional write. Return True whatever Redis answers -> the None row
    goes red."""
    fake = _FakeRedis(answer=redis_answer)
    monkeypatch.setattr(oauth, '_get_redis_client', lambda: fake)

    assert oauth._oauth_store_add('consent', 'c-1', {'revoked_at': 1.0}, ttl=60) is stored
    assert fake.calls == [{'key': oauth._oauth_key('consent', 'c-1'),
                           'ex': 60, 'nx': True}]
    assert 'c-1' not in oauth._OAUTH_STORES['consent'], 'fell through to memory'


def test_the_conditional_write_in_memory_keeps_the_first_value():
    """MUTATION: write with plain assignment -> the second add replaces the
    first and answers True."""
    assert oauth._oauth_store_add('consent', 'c-mem-1', {'revoked_at': 1.0}) is True
    assert oauth._oauth_store_add('consent', 'c-mem-1', {'revoked_at': None}) is False
    assert _record('c-mem-1') == {'revoked_at': 1.0}


def test_a_redis_failure_in_production_raises_and_stores_nothing(monkeypatch):
    """A store that cannot answer is never read as "no record".

    MUTATION: fall through to the in-memory store on a Redis error in
    production -> the add answers True."""
    fake = _FakeRedis(error=ConnectionError('down'))
    monkeypatch.setattr(oauth, '_get_redis_client', lambda: fake)
    monkeypatch.setattr(oauth, '_is_production', lambda: True)

    with pytest.raises(RuntimeError, match='OAuth state store unavailable'):
        oauth._oauth_store_add('consent', 'c-down-1', {'revoked_at': None}, ttl=60)
    assert len(fake.calls) == 1
    assert 'c-down-1' not in oauth._OAUTH_STORES['consent']


def test_the_return_answers_500_and_issues_no_code_when_the_store_fails(
        client, monkeypatch):
    """The return has no read of its own to misread: its one store step is
    the conditional write, and when that raises the caller gets a 500, no
    redirect, no code and no consent.

    MUTATION: catch the store failure in consent_return and carry on -> a
    code is issued."""
    reg = _register(client)
    grant, consent_id, _ = _approval(client, reg)
    asked = []

    def _down(kind, key, value, ttl=None):
        asked.append((kind, key))
        raise RuntimeError('OAuth state store unavailable')

    monkeypatch.setattr(oauth, '_oauth_store_add', _down)
    monkeypatch.setitem(client.application.config, 'PROPAGATE_EXCEPTIONS', False)
    codes = len(oauth._OAUTH_STORES['auth-code'])

    resp = _return(client, grant)
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert asked == [('consent', consent_id)]
    assert 'Location' not in resp.headers
    assert len(oauth._OAUTH_STORES['auth-code']) == codes
    assert _record(consent_id) is None
    assert _consent_rows(client) == []


def test_a_revoke_does_not_answer_not_found_for_a_record_it_cannot_read(
        client, monkeypatch):
    """The read says "no record" and the conditional write says "there is
    one": the record exists and cannot be read. 404 would report a live
    consent as gone, so the answer is 503.

    MUTATION: answer 404 in that branch -> red."""
    asked = []

    def _exists(kind, key, value, ttl=None):
        asked.append((kind, key))
        return False

    monkeypatch.setattr(oauth, '_oauth_store_add', _exists)
    resp = _revoke(client, 'consent_unreadable_1')
    assert asked == [('consent', 'consent_unreadable_1')]
    assert resp.status_code == 503
    assert resp.get_json() == {'error': 'temporarily_unavailable'}
