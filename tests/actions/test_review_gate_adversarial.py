"""Adversarial probes for the review-page allergy-attestation gate (Task 6).

Written by the coordinator (not the implementer) to independently attack the
load-bearing safety property: the software must NEVER let a form be finalized
that silently drops a real allergy or asserts "no known allergies" without an
explicit human attestation. Mirrors the hands-on verification done for the SDC
populate NKA invariant.
"""
from models import db
from r6.actions.confirmations import ActionConfirmation
from r6.actions.models import ProposedAction

from tests.actions.test_review_flow import (
    ALLERGY_A, PATIENT, R6, _post, _staged_form_fill,
)


def _no_confirmation(app, action_id):
    with app.app_context():
        return ActionConfirmation.query.filter_by(action_id=action_id).count() == 0


def _action(app, action_id):
    with app.app_context():
        a = ProposedAction.query.filter_by(id=action_id).first()
        return a.status, (a.payload or {}).get('reviewed_qr_id')


def test_fake_extra_allergy_confirm_cannot_bypass_nka_gate(
        client, app, tenant_headers, auth_headers, tenant_id):
    """Patient HAS a real Penicillin allergy. Attacker removes the real row and
    submits a fabricated allergy-1=confirm (no such server row) plus no NKA.
    The gate reads decisions only over server-derived rows, so the fake confirm
    is ignored -> 422, no ActionConfirmation, action untouched."""
    with app.app_context():
        db.session.add(R6(PATIENT, tenant_id))
        db.session.add(R6(ALLERGY_A, tenant_id))
        db.session.commit()

    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    body = {'allergy-0': 'remove', 'allergy-1': 'confirm'}  # no 'nka'
    r = _post(client, {**tenant_headers, **auth_headers}, action_id, body)

    assert r.status_code == 422, r.get_data(as_text=True)
    assert _no_confirmation(app, action_id), 'a rejected review must issue NO confirmation'
    status, reviewed_qr_id = _action(app, action_id)
    assert status == 'awaiting_confirmation', 'rejected review must not advance the action'
    assert reviewed_qr_id is None, 'no reviewed QR should be persisted on rejection'


def test_removing_real_allergy_without_nka_is_rejected(
        client, app, tenant_headers, auth_headers, tenant_id):
    """The canonical dangerous case: a real allergy exists, the human removes it
    and does NOT attest NKA. Silence is not consent -> 422, no confirmation."""
    with app.app_context():
        db.session.add(R6(PATIENT, tenant_id))
        db.session.add(R6(ALLERGY_A, tenant_id))
        db.session.commit()

    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    r = _post(client, {**tenant_headers, **auth_headers}, action_id,
              {'allergy-0': 'remove'})

    assert r.status_code == 422, r.get_data(as_text=True)
    assert _no_confirmation(app, action_id)
