"""A person who reads the proposal and says no is recorded as one (#520).

Before this, `awaiting_confirmation` could only become `executing` or
`expired`, so a refusal and a timeout produced the same record: the trail
could prove nobody approved, never that somebody objected. `declined` is a
terminal state reachable only from `awaiting_confirmation` and only through
POST /<id>/decline, on the same action-bound, payload-bound, single-use
credential as Approve. Nothing infers it: a lapsed window stays `expired`.
"""
from datetime import datetime, timedelta, timezone

from models import db
from r6.actions.models import ProposedAction, _TRANSITIONS
from r6.models import AuditEventRecord
from tests.actions.test_confirm_is_commit import (
    PROPOSE_BODY, _approval_headers, _commit, _confirm, _propose)


def _decline(client, auth_headers, action_id, body=None, headers=None):
    return client.post('/r6/actions/%s/decline' % action_id,
                       headers=headers or _approval_headers(auth_headers,
                                                            action_id),
                       json=body or {})


def _past():
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)


def test_declined_is_terminal_and_reachable_only_from_awaiting():
    assert _TRANSITIONS['declined'] == set()
    assert {s for s, nxt in _TRANSITIONS.items() if 'declined' in nxt} == {
        'awaiting_confirmation'}


def test_a_decline_is_recorded_as_a_decline_not_a_timeout(
        client, tenant_headers, auth_headers, app, action_registry,
        fake_providers):
    action_id = _propose(client, tenant_headers)
    assert _commit(client, auth_headers, action_id).status_code == 202
    resp = _decline(client, auth_headers, action_id,
                    body={'declined_via': 'review-page'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json() == {'id': action_id, 'status': 'declined'}
    assert fake_providers == [], 'a decline must never dial'
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'declined'
        details = [e.detail for e in AuditEventRecord.query.filter_by(
            resource_id=action_id).all()]
        declined = [d for d in details
                    if d.startswith('declined via review-page; payload_digest=')]
        assert len(declined) == 1, details
        # PHI-free: the payload the person refused is not in the trail.
        assert PROPOSE_BODY['payload']['phone'] not in declined[0]
        assert 'John Smith' not in declined[0]


def test_a_declined_action_cannot_then_be_approved(
        client, tenant_headers, auth_headers, app, action_registry,
        fake_providers):
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    assert _decline(client, auth_headers, action_id).status_code == 200
    resp = _confirm(client, auth_headers, action_id)
    assert resp.status_code == 409
    assert 'declined' in resp.get_json()['error']
    assert fake_providers == []
    with app.app_context():
        assert db.session.get(ProposedAction, action_id).status == 'declined'


def test_the_decline_credential_is_spent_on_use(
        client, tenant_headers, auth_headers, app, action_registry):
    """One credential, one answer: the token that declined cannot be
    replayed to approve, and cannot decline twice."""
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    headers = _approval_headers(auth_headers, action_id)
    assert _decline(client, auth_headers, action_id,
                    headers=headers).status_code == 200
    replay = client.post('/r6/actions/%s/confirm' % action_id,
                         headers=headers, json={})
    assert replay.status_code == 401


def test_decline_needs_the_approval_credential(
        client, tenant_headers, auth_headers, app, action_registry):
    """A generic write token is what commits; it cannot decline, exactly as
    it cannot approve: only the surface that showed the card can answer."""
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    resp = _decline(client, auth_headers, action_id, headers=auth_headers)
    assert resp.status_code == 401
    with app.app_context():
        assert db.session.get(ProposedAction, action_id).status == (
            'awaiting_confirmation')


def test_a_lapsed_window_is_expired_not_declined(
        client, tenant_headers, auth_headers, app, action_registry):
    """Never inferred, in either direction: tapping Decline after the window
    lapsed records the timeout, because nobody could still have approved."""
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        row.expires_at = _past()
        db.session.commit()
    resp = _decline(client, auth_headers, action_id)
    assert resp.status_code == 410
    with app.app_context():
        assert db.session.get(ProposedAction, action_id).status == 'expired'


def test_decline_before_commit_conflicts(client, tenant_headers,
                                         auth_headers, app):
    """A proposal nobody has been asked to confirm has nothing to decline."""
    action_id = _propose(client, tenant_headers)
    resp = _decline(client, auth_headers, action_id)
    assert resp.status_code == 409
    with app.app_context():
        assert db.session.get(ProposedAction, action_id).status == 'proposed'


def test_decline_rejects_an_unknown_surface(client, tenant_headers,
                                            auth_headers, app):
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    resp = _decline(client, auth_headers, action_id,
                    body={'declined_via': 'carrier-pigeon'})
    assert resp.status_code == 400
    with app.app_context():
        assert db.session.get(ProposedAction, action_id).status == (
            'awaiting_confirmation')


def test_a_window_that_lapses_between_the_check_and_the_claim_is_expired(
        client, tenant_headers, auth_headers, app, monkeypatch):
    """TOCTOU closure, mirroring confirm: the claim's WHERE re-checks
    expires_at, and a refused claim on a still-awaiting row is reported as
    the timeout it is, never as a 409 that names the same state twice."""
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        row.expires_at = _past()
        db.session.commit()
    monkeypatch.setattr(ProposedAction, 'is_expired', lambda self: False)
    resp = _decline(client, auth_headers, action_id)
    assert resp.status_code == 410
    with app.app_context():
        assert db.session.get(ProposedAction, action_id).status == 'expired'
