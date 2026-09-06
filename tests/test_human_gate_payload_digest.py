"""The human gate can show byte equality between what was approved and what
executes (#559, spec docs/specs/2026-08-16-human-gate.md section 8.3).

Before this, ActionConfirmation had no digest: the payload seal (#528)
bound the payload to the confirmation temporally, but nothing recorded
what the bytes were at the moment of approval, so a writer past the seal
(bulk Query.update, session.execute(update()), raw SQL, ...) would leave
no evidence. The ledger could say "this person approved, and nobody we
know of changed it afterwards"; only "this person approved exactly this"
survives an audit.

Now every confirmation is minted with a sha256 over a canonical
serialisation of the payload, the approval audit line carries the digest,
and the confirm route verifies every open confirmation's digest against
the payload it is about to execute: a mismatch fails the action, audits
the two digests (never the payload), and answers 409.

MUTATION: r6/actions/routes.py, delete the digest check in confirm_action
-> the tamper test goes red (the action executes). r6/actions/
confirmations.py, mint with payload_digest=None -> the mint test goes red.
"""

import json

import pytest

from models import db
from r6.actions.confirmations import (ActionConfirmation, issue_confirmation,
                                      payload_digest)
from r6.actions.models import ProposedAction
from r6.models import AuditEventRecord

from tests.test_actions_routes import (PROPOSE_BODY, _approval_headers,
                                       _propose)


def test_the_digest_is_over_a_canonical_form():
    a = json.dumps({'to': 'x', 'phone': '1', 'body': 'b'})
    b = json.dumps({'body': 'b', 'phone': '1', 'to': 'x'}, indent=2)
    assert payload_digest(a) == payload_digest(b)
    assert len(payload_digest(a)) == 64
    assert payload_digest(a) != payload_digest(json.dumps({'to': 'y'}))


def _commit(client, tenant_headers, auth_headers, action_id):
    resp = client.post('/r6/actions/%s/commit' % action_id, headers=auth_headers)
    assert resp.status_code == 202, resp.get_json()


def _audit_rows(app, tenant_id, action_id):
    with app.app_context():
        return AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type='ProposedAction',
            resource_id=action_id).all()


def test_a_confirmation_is_minted_with_the_payload_digest_and_the_audit_says_so(
        client, app, tenant_headers, auth_headers, tenant_id):
    action_id = _propose(client, tenant_headers)
    _commit(client, tenant_headers, auth_headers, action_id)
    with app.app_context():
        expected = payload_digest(
            ProposedAction.query.get(action_id).payload_json)
    resp = client.post('/r6/actions/%s/confirm' % action_id,
                       headers=_approval_headers(auth_headers, action_id))
    # No provider is configured in the suite, so execution itself answers
    # 502 after the consent record is minted; what this pins is the record.
    assert resp.status_code != 409
    assert resp.get_json().get('error_code') != 'approved_payload_mismatch'
    with app.app_context():
        rows = ActionConfirmation.query.filter_by(action_id=action_id).all()
        assert rows and all(r.payload_digest == expected for r in rows)
    approvals = [r for r in _audit_rows(app, tenant_id, action_id)
                 if r.detail and r.detail.startswith('approved via')]
    assert approvals and all(
        'payload_digest=%s' % expected in r.detail for r in approvals)


def test_a_payload_changed_past_the_seal_after_approval_is_refused(
        client, app, tenant_headers, auth_headers, tenant_id):
    """The review-page shape: a confirmation minted in one request, execution
    in a later one, and in between a writer the ORM seal cannot see."""
    action_id = _propose(client, tenant_headers)
    _commit(client, tenant_headers, auth_headers, action_id)
    with app.app_context():
        action = ProposedAction.query.get(action_id)
        issue_confirmation(action_id, approved_via='review-page',
                           ttl_minutes=15, payload_json=action.payload_json)
        db.session.commit()
        swapped = dict(PROPOSE_BODY['payload'], phone='617-555-0199')
        # Bulk update compiles straight to SQL: the @validates seal never fires.
        ProposedAction.query.filter_by(id=action_id).update(
            {'payload_json': json.dumps(swapped)}, synchronize_session=False)
        db.session.commit()
    resp = client.post('/r6/actions/%s/confirm' % action_id,
                       headers=_approval_headers(auth_headers, action_id))
    assert resp.status_code == 409
    assert resp.get_json()['error_code'] == 'approved_payload_mismatch'
    with app.app_context():
        assert ProposedAction.query.get(action_id).status == 'failed'
    failures = [r for r in _audit_rows(app, tenant_id, action_id)
                if r.outcome == 'failure']
    assert failures
    detail = failures[-1].detail
    assert 'approved payload digest mismatch' in detail
    assert 'approved=' in detail and 'current=' in detail
    for phi in ('617-555-0199', '617-555-0100', 'metformin', 'John Smith'):
        assert phi not in detail


def test_a_confirmation_without_a_digest_cannot_authorize_an_execution(
        client, app, tenant_headers, auth_headers, tenant_id):
    """A row minted before the column existed proves nothing about the
    bytes; it is refused rather than trusted."""
    action_id = _propose(client, tenant_headers)
    _commit(client, tenant_headers, auth_headers, action_id)
    with app.app_context():
        row = ActionConfirmation(action_id=action_id, approved_via='dashboard',
                                 expires_at=__import__('r6.actions.models',
                                                       fromlist=['_utcnow'])._utcnow()
                                 + __import__('datetime').timedelta(minutes=15))
        row.payload_digest = None
        db.session.add(row)
        db.session.commit()
    resp = client.post('/r6/actions/%s/confirm' % action_id,
                       headers=_approval_headers(auth_headers, action_id))
    assert resp.status_code == 409
    assert resp.get_json()['error_code'] == 'approved_payload_mismatch'
    failures = [r for r in _audit_rows(app, tenant_id, action_id)
                if r.outcome == 'failure']
    assert failures and 'carries no digest' in failures[-1].detail


def test_issue_confirmation_requires_the_payload(app):
    with app.app_context():
        with pytest.raises(TypeError):
            issue_confirmation('x', approved_via='dashboard', ttl_minutes=1)


def _plain_sha256(payload_json):
    """What a writer with table access but no server secret can compute."""
    import hashlib
    canonical = json.dumps(json.loads(payload_json), sort_keys=True,
                           separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def test_forging_the_payload_and_the_stored_digest_together_is_still_refused(
        client, app, tenant_headers, auth_headers, tenant_id):
    """The QA pass on #658: the digest sat in a row the same writer could
    rewrite, so forging both walked through the check and the approval
    audit line vouched for the forged hash. The digest is now keyed by a
    secret the database does not hold, so the best a table-level writer can
    do is a plain hash of the forged payload, which does not match.

    MUTATION: key the digest with a constant, or drop the key -> red.
    """
    action_id = _propose(client, tenant_headers)
    _commit(client, tenant_headers, auth_headers, action_id)
    with app.app_context():
        action = ProposedAction.query.get(action_id)
        issue_confirmation(action_id, approved_via='review-page',
                           ttl_minutes=15, payload_json=action.payload_json)
        db.session.commit()
        forged = json.dumps(dict(PROPOSE_BODY['payload'], phone='617-555-0199'))
        ProposedAction.query.filter_by(id=action_id).update(
            {'payload_json': forged}, synchronize_session=False)
        ActionConfirmation.query.filter_by(action_id=action_id).update(
            {'payload_digest': _plain_sha256(forged)}, synchronize_session=False)
        db.session.commit()
    resp = client.post('/r6/actions/%s/confirm' % action_id,
                       headers=_approval_headers(auth_headers, action_id))
    assert resp.status_code == 409
    assert resp.get_json()['error_code'] == 'approved_payload_mismatch'
    with app.app_context():
        assert ProposedAction.query.get(action_id).status == 'failed'
    rows = _audit_rows(app, tenant_id, action_id)
    assert any(r.outcome == 'failure' and 'approved payload digest mismatch' in r.detail
               for r in rows)
    # The ledger never vouches for the forgery: no success line carries it.
    assert not any(r.outcome == 'success' and 'approved via' in (r.detail or '')
                   for r in rows)


def test_the_digest_needs_the_secret(app, monkeypatch):
    with app.app_context():
        a = payload_digest('{"k": "v"}')
        monkeypatch.setenv('STEP_UP_SECRET', 'a-different-secret')
        assert payload_digest('{"k": "v"}') != a
        monkeypatch.setenv('STEP_UP_SECRET', '')
        with pytest.raises(ValueError):
            payload_digest('{"k": "v"}')

