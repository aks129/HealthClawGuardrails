"""Structured per-item review page (Task 6) — the core safety UI.

A human confirms each populated medication and allergy individually, and must
EXPLICITLY affirm "no known allergies" — it is never inferred from the absence
of allergy data. The server-side POST is the load-bearing gate: a crafted POST
that skips the allergy attestation MUST be rejected (422), leaving the action
untouched and issuing NO ActionConfirmation.

Field contract for POST /r6/actions/<id>/review (JSON or form):
  med-<i>       one of 'yes' | 'no' | 'remove'   (i = 0..N-1 populated meds)
  allergy-<i>   one of 'confirm' | 'remove'      (i = 0..M-1 populated allergies)
  condition-<i> one of 'confirm' | 'remove'      (optional; confirmable)
  nka           truthy => the explicit "no known allergies" attestation
The server RE-POPULATES from the tenant's FHIR to decide which med/allergy
rows must be acted on — it never trusts the client about how many rows exist.
"""
import json

from models import db
from r6.actions.confirmations import ActionConfirmation
from r6.actions.models import ProposedAction

PATIENT = {
    'resourceType': 'Patient', 'id': 'test-patient-1',
    'name': [{'family': 'Smith', 'given': ['John']}],
    'gender': 'male', 'birthDate': '1990-01-15',
}
MED_A = {
    'resourceType': 'MedicationRequest', 'id': 'med-a', 'status': 'active',
    'intent': 'order',
    'medicationCodeableConcept': {'text': 'Metformin 500 mg tablet'},
    'dosageInstruction': [{'text': 'Take 1 tablet twice daily'}],
    'subject': {'reference': 'Patient/test-patient-1'},
}
MED_B = {
    'resourceType': 'MedicationRequest', 'id': 'med-b', 'status': 'active',
    'intent': 'order',
    'medicationCodeableConcept': {'text': 'Lisinopril 10 mg tablet'},
    'dosageInstruction': [{'text': 'Take 1 tablet daily'}],
    'subject': {'reference': 'Patient/test-patient-1'},
}
ALLERGY_A = {
    'resourceType': 'AllergyIntolerance', 'id': 'allergy-a',
    'code': {'text': 'Penicillin'},
    'reaction': [{'manifestation': [{'text': 'Hives'}]}],
    'patient': {'reference': 'Patient/test-patient-1'},
}
CONDITION_A = {
    'resourceType': 'Condition', 'id': 'cond-a',
    'code': {'text': 'Type 2 diabetes mellitus'},
    'subject': {'reference': 'Patient/test-patient-1'},
}

FORM_FILL_BODY = {
    'kind': 'form-fill',
    'payload': {'to': 'Intake portal', 'questionnaire': 'healthclaw-intake',
                'body': 'new patient intake form'},
}


def _seed(app, tenant, resources):
    with app.app_context():
        for r in resources:
            db.session.add(R6(r, tenant))
        db.session.commit()


def R6(resource, tenant):
    from r6.models import R6Resource
    return R6Resource(resource_type=resource['resourceType'],
                      resource_json=json.dumps(resource),
                      resource_id=resource['id'], tenant_id=tenant)


def _staged_form_fill(client, tenant_headers, auth_headers):
    """propose + commit a form-fill action -> awaiting_confirmation."""
    r = client.post('/r6/actions/propose', json=FORM_FILL_BODY,
                    headers=tenant_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    action_id = r.get_json()['id']
    c = client.post('/r6/actions/%s/commit' % action_id, headers=auth_headers)
    assert c.status_code == 202, c.get_data(as_text=True)
    return action_id


def _get(client, headers, action_id):
    return client.get('/r6/actions/%s/review' % action_id, headers=headers)


def _post(client, headers, action_id, body):
    return client.post('/r6/actions/%s/review' % action_id,
                       headers=headers, json=body)


# ---------------------------------------------------------------------------
# GET renders the review page
# ---------------------------------------------------------------------------

def test_get_review_renders_populated_items_nka_not_prechecked(
        client, app, tenant_headers, auth_headers):
    _seed(app, tenant_headers['X-Tenant-Id'],
          [PATIENT, MED_A, MED_B, ALLERGY_A, CONDITION_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    resp = _get(client, auth_headers, action_id)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    html = resp.get_data(as_text=True)
    # Populated clinical content is shown with provenance.
    assert 'Metformin 500 mg tablet' in html
    assert 'Lisinopril 10 mg tablet' in html
    assert 'Penicillin' in html
    assert 'from your records' in html
    # The NKA checkbox exists and is NOT pre-checked.
    assert 'no known allergies' in html.lower()
    nka_idx = html.lower().find('no known allergies')
    # find the input element for NKA (name="nka") and assert it is unchecked
    assert 'name="nka"' in html
    input_idx = html.find('name="nka"')
    # slice the input tag and ensure 'checked' is not inside it
    tag = html[html.rfind('<input', 0, input_idx):
               html.find('>', input_idx) + 1]
    assert 'checked' not in tag.lower()
    assert nka_idx > 0


def test_get_review_no_allergies_never_prechecks_nka(
        client, app, tenant_headers, auth_headers):
    _seed(app, tenant_headers['X-Tenant-Id'], [PATIENT, MED_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    resp = _get(client, auth_headers, action_id)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'No allergies found in your records' in html
    input_idx = html.find('name="nka"')
    tag = html[html.rfind('<input', 0, input_idx):
               html.find('>', input_idx) + 1]
    assert 'checked' not in tag.lower()


def test_get_review_requires_step_up(client, app, tenant_headers, auth_headers):
    _seed(app, tenant_headers['X-Tenant-Id'], [PATIENT, MED_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    resp = _get(client, tenant_headers, action_id)   # no step-up token
    assert resp.status_code == 401


def test_get_review_non_form_fill_404(client, app, tenant_headers, auth_headers):
    r = client.post('/r6/actions/propose', json={
        'kind': 'sms',
        'payload': {'to': 'Dr. Smith', 'phone': '617-555-0100',
                    'body': 'reminder'}}, headers=tenant_headers)
    action_id = r.get_json()['id']
    client.post('/r6/actions/%s/commit' % action_id, headers=auth_headers)
    resp = _get(client, auth_headers, action_id)
    assert resp.status_code == 404


def test_get_review_wrong_tenant_404(client, app, tenant_headers, auth_headers,
                                     other_tenant_headers):
    _seed(app, tenant_headers['X-Tenant-Id'], [PATIENT, MED_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    resp = _get(client, other_tenant_headers, action_id)
    assert resp.status_code == 404


def test_get_review_requires_awaiting_confirmation(client, app, tenant_headers,
                                                   auth_headers):
    _seed(app, tenant_headers['X-Tenant-Id'], [PATIENT, MED_A])
    r = client.post('/r6/actions/propose', json=FORM_FILL_BODY,
                    headers=tenant_headers)
    action_id = r.get_json()['id']          # proposed, NOT committed
    resp = _get(client, auth_headers, action_id)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# LOAD-BEARING server-side allergy-attestation gate
# ---------------------------------------------------------------------------

def test_post_omitting_allergy_attestation_is_rejected(
        client, app, tenant_headers, auth_headers):
    """A crafted POST that acts on every row but neither confirms an allergy
    nor affirms NKA MUST be rejected — silence is never consent."""
    tenant = tenant_headers['X-Tenant-Id']
    _seed(app, tenant, [PATIENT, MED_A, MED_B, ALLERGY_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    # Every med acted on; the one allergy is REMOVED (not confirmed); no NKA.
    resp = _post(client, auth_headers, action_id,
                 {'med-0': 'yes', 'med-1': 'no', 'allergy-0': 'remove'})
    assert resp.status_code == 422, resp.get_data(as_text=True)
    assert 'allergy' in resp.get_json()['error'].lower()

    with app.app_context():
        # Action untouched, NO consent record issued.
        assert db.session.get(ProposedAction,
                              action_id).status == 'awaiting_confirmation'
        assert ActionConfirmation.query.filter_by(
            action_id=action_id).count() == 0


def test_post_no_allergies_still_requires_nka_attestation(
        client, app, tenant_headers, auth_headers):
    """Zero allergies in the records does NOT let the form proceed silently —
    the patient must affirmatively check NKA."""
    tenant = tenant_headers['X-Tenant-Id']
    _seed(app, tenant, [PATIENT, MED_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    resp = _post(client, auth_headers, action_id, {'med-0': 'yes'})
    assert resp.status_code == 422
    with app.app_context():
        assert ActionConfirmation.query.filter_by(
            action_id=action_id).count() == 0


def test_post_missing_med_action_is_rejected(client, app, tenant_headers,
                                             auth_headers):
    tenant = tenant_headers['X-Tenant-Id']
    _seed(app, tenant, [PATIENT, MED_A, MED_B])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    # med-1 omitted -> a medication row was not acted on.
    resp = _post(client, auth_headers, action_id,
                 {'med-0': 'yes', 'nka': 'true'})
    assert resp.status_code == 422
    assert 'medication' in resp.get_json()['error'].lower()
    with app.app_context():
        assert ActionConfirmation.query.filter_by(
            action_id=action_id).count() == 0


# ---------------------------------------------------------------------------
# POST happy paths
# ---------------------------------------------------------------------------

def test_post_with_nka_affirmed_succeeds(client, app, tenant_headers,
                                         auth_headers):
    tenant = tenant_headers['X-Tenant-Id']
    _seed(app, tenant, [PATIENT, MED_A, MED_B])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    resp = _post(client, auth_headers, action_id,
                 {'med-0': 'yes', 'med-1': 'no', 'nka': 'true'})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    with app.app_context():
        rows = ActionConfirmation.query.filter_by(action_id=action_id).all()
        assert len(rows) == 1
        assert rows[0].approved_via == 'review-page'
        # Reviewed QR persisted, tenant-scoped, status completed.
        action = db.session.get(ProposedAction, action_id)
        qr_id = action.payload.get('reviewed_qr_id')
        assert qr_id
        from r6.models import R6Resource
        row = R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse', id=qr_id,
            tenant_id=tenant).first()
        assert row is not None
        qr = row.to_fhir_json()
        assert qr['status'] == 'completed'
        # NKA attestation captured as an explicit boolean true.
        assert _nka_answer(qr) is True


def test_post_confirming_real_allergy_succeeds(client, app, tenant_headers,
                                               auth_headers):
    tenant = tenant_headers['X-Tenant-Id']
    _seed(app, tenant, [PATIENT, MED_A, ALLERGY_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    # Confirm the real allergy; NO NKA. This satisfies the attestation gate.
    resp = _post(client, auth_headers, action_id,
                 {'med-0': 'yes', 'allergy-0': 'confirm'})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    with app.app_context():
        assert ActionConfirmation.query.filter_by(
            action_id=action_id, approved_via='review-page').count() == 1
        action = db.session.get(ProposedAction, action_id)
        qr_id = action.payload.get('reviewed_qr_id')
        from r6.models import R6Resource
        qr = R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse', id=qr_id,
            tenant_id=tenant).first().to_fhir_json()
        assert qr['status'] == 'completed'
        assert _nka_answer(qr) is not True   # NKA never inferred


def test_post_requires_step_up(client, app, tenant_headers, auth_headers):
    _seed(app, tenant_headers['X-Tenant-Id'], [PATIENT, MED_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    resp = client.post('/r6/actions/%s/review' % action_id,
                       headers=tenant_headers, json={'med-0': 'yes',
                                                     'nka': 'true'})
    assert resp.status_code == 401


def test_post_wrong_tenant_404(client, app, tenant_headers, auth_headers,
                               other_tenant_headers):
    _seed(app, tenant_headers['X-Tenant-Id'], [PATIENT, MED_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    resp = _post(client, other_tenant_headers, action_id,
                 {'med-0': 'yes', 'nka': 'true'})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# confirmations.py accepts the new channel
# ---------------------------------------------------------------------------

def test_issue_confirmation_accepts_review_page(app):
    from r6.actions.confirmations import (APPROVED_VIA_VALUES,
                                          issue_confirmation)
    assert 'review-page' in APPROVED_VIA_VALUES
    with app.app_context():
        c = issue_confirmation('some-action', 'review-page', ttl_minutes=15)
        db.session.add(c)
        db.session.commit()
        assert c.approved_via == 'review-page'


def _nka_answer(qr):
    """Extract the boolean answer for allergies.no-known-allergies, or None."""
    for group in qr.get('item', []):
        if group.get('linkId') == 'allergies':
            for child in group.get('item', []):
                if child.get('linkId') == 'allergies.no-known-allergies':
                    for ans in child.get('answer', []):
                        if 'valueBoolean' in ans:
                            return ans['valueBoolean']
    return None
