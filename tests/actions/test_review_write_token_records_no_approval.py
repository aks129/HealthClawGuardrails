"""A write token alone cannot record an approval on the review page (#1104).

The review POST takes the tenant's ordinary write token, which the agent
that proposed the action also holds. For every kind but form-fill it used to
mint an ActionConfirmation and an "approved via review-page" audit row from
that token alone, and the person's own Approve then answered 409.

The approval is recorded by the confirm route, under the action-bound
credential only the approve surface can mint. These tests hold the review
POST to recording none.
"""
from models import db
from r6.actions.confirmations import ActionConfirmation
from r6.actions.models import ProposedAction
from r6.models import AuditEventRecord, R6Resource
from tests.actions.test_action_approve_page import (CURATR, _post, _staged,
                                                    seeded)  # noqa: F401
from tests.actions.test_review_flow import PATIENT, _seed, _staged_form_fill
from tests.approval_helpers import approval_headers, reviewed

# ruff: noqa: F811  (the `seeded` fixture is imported, then requested by name)


def _confirmations(app, action_id):
    with app.app_context():
        return ActionConfirmation.query.filter_by(action_id=action_id).all()


def _audit_details(app, tenant_id, action_id):
    with app.app_context():
        return [r.detail or '' for r in AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type='ProposedAction',
            resource_id=action_id).all()]


def test_a_write_token_review_records_no_approval(
        client, app, tenant_headers, auth_headers, tenant_id, seeded):
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    r = _post(client, auth_headers, action_id)
    # Answered, because the approve surface's relay sends this same request
    # before it mints the approval credential; but nothing is approved.
    assert r.status_code == 200, r.get_data(as_text=True)
    assert 'approved_via' not in r.get_json()
    assert _confirmations(app, action_id) == []
    details = _audit_details(app, tenant_id, action_id)
    assert not any(d.startswith('approved via') for d in details), details
    # The access itself is still on the trail.
    assert any(d.startswith('review received') for d in details), details
    with app.app_context():
        assert (db.session.get(ProposedAction, action_id).status
                == 'awaiting_confirmation')


def test_the_real_approval_succeeds_after_a_write_token_review(
        client, app, tenant_headers, auth_headers, tenant_id, seeded):
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    # The proposer's own POST, with the write token it already holds.
    assert _post(client, auth_headers, action_id).status_code == 200
    # The person's Approve, as the relay sends it: review, then confirm on
    # the minted credential. The review used to answer 409 here.
    again = _post(client, auth_headers, action_id)
    assert again.status_code == 200, again.get_data(as_text=True)
    r = client.post('/r6/actions/%s/confirm' % action_id,
                    json={'approved_via': 'review-page'},
                    headers=approval_headers(app, auth_headers, action_id))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['status'] == 'completed'
    # One consent record, and it is the one the approval credential wrote.
    rows = _confirmations(app, action_id)
    assert [c.approved_via for c in rows] == ['review-page']
    assert rows[0].consumed_at is not None
    approved = [d for d in _audit_details(app, tenant_id, action_id)
                if d.startswith('approved via')]
    assert len(approved) == 1, approved


def test_two_approvals_after_a_write_token_review_execute_once(
        client, app, tenant_headers, auth_headers, tenant_id, seeded):
    """A second review POST now answers 200 instead of 409, so a double-tap
    reaches confirm twice. The claim is single-winner: one execution, one
    consent record, one "approved via" row."""
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    assert _post(client, auth_headers, action_id).status_code == 200
    assert _post(client, auth_headers, action_id).status_code == 200
    h1 = approval_headers(app, auth_headers, action_id)
    h2 = approval_headers(app, auth_headers, action_id)
    r1 = client.post('/r6/actions/%s/confirm' % action_id, headers=h1,
                     json={'approved_via': 'review-page'})
    r2 = client.post('/r6/actions/%s/confirm' % action_id, headers=h2,
                     json={'approved_via': 'review-page'})
    assert sorted([r1.status_code, r2.status_code]) == [200, 409], (
        r1.get_data(as_text=True), r2.get_data(as_text=True))
    assert len(_confirmations(app, action_id)) == 1
    approved = [d for d in _audit_details(app, tenant_id, action_id)
                if d.startswith('approved via')]
    assert len(approved) == 1, approved


def _require(cond, msg):
    """Set-up check. RuntimeError, so a broken set-up is told apart from the
    refusal the test asserts (this was a strict xfail until #1116)."""
    if not cond:
        raise RuntimeError(msg)


def test_a_write_token_cannot_submit_attested_answers(
        client, app, tenant_headers, auth_headers, tenant_id):
    _seed(app, tenant_id, [PATIENT])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    # Reach proof: a form-fill action awaiting review, with no answers and
    # no confirmation yet, so the POST below lands on the open branch.
    with app.app_context():
        action = db.session.get(ProposedAction, action_id)
        _require(action.kind == 'form-fill', 'not a form-fill action')
        _require(action.status == 'awaiting_confirmation', 'not awaiting')
        _require('reviewed_qr_id' not in action.payload, 'already reviewed')
    _require(_confirmations(app, action_id) == [], 'already confirmed')
    r = _post_answers(client, auth_headers, action_id, {'nka': 'true'})
    assert r.status_code in (401, 403), r.get_data(as_text=True)
    assert _confirmations(app, action_id) == []
    with app.app_context():
        assert R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse',
            tenant_id=tenant_id).count() == 0
    # Positive control: the same answers on the person's credential land,
    # so the refusal above is the credential and not a broken request.
    person = _post_answers(client,
                           approval_headers(app, auth_headers, action_id),
                           action_id, {'nka': 'true'})
    assert person.status_code == 200, person.get_data(as_text=True)
    assert [c.approved_via for c in _confirmations(app, action_id)] == [
        'review-page']


def _post_answers(client, headers, action_id, body):
    return client.post('/r6/actions/%s/review' % action_id, headers=headers,
                       json=reviewed(client, headers, action_id, body,
                                     strict=False))


def test_a_credential_for_another_form_is_refused(
        client, app, tenant_headers, auth_headers, tenant_id):
    """The credential is bound to one action and its payload (#1116), so an
    approval minted for another form cannot carry answers into this one.

    MUTATION: drop `operation=` from _require_approval_credential -> red."""
    _seed(app, tenant_id, [PATIENT])
    target = _staged_form_fill(client, tenant_headers, auth_headers)
    other = _staged_form_fill(client, tenant_headers, auth_headers)
    r = _post_answers(client, approval_headers(app, auth_headers, other),
                      target, {'nka': 'true'})
    assert r.status_code == 401, r.get_data(as_text=True)
    assert _confirmations(app, target) == []
    with app.app_context():
        assert 'reviewed_qr_id' not in db.session.get(
            ProposedAction, target).payload
    # Positive control: the target's own credential is accepted.
    own = _post_answers(client, approval_headers(app, auth_headers, target),
                        target, {'nka': 'true'})
    assert own.status_code == 200, own.get_data(as_text=True)


def test_the_same_credential_works_again_after_a_refused_review(
        client, app, tenant_headers, auth_headers, tenant_id):
    """Not nonce-consumed: a review refused 422 (no attestation) is
    corrected and sent again on the credential the page already holds."""
    _seed(app, tenant_id, [PATIENT])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    headers = approval_headers(app, auth_headers, action_id)
    first = _post_answers(client, headers, action_id, {})
    assert first.status_code == 422, first.get_data(as_text=True)
    again = _post_answers(client, headers, action_id, {'nka': 'true'})
    assert again.status_code == 200, again.get_data(as_text=True)
