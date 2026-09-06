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

# A transaction Bundle references its own entries by fullUrl, so a patient
# arrives as `urn:uuid:<id>` rather than `Patient/<id>` (#390).
URN_ALLERGY = {
    'resourceType': 'AllergyIntolerance', 'id': 'allergy-urn',
    'code': {'text': 'Penicillin'},
    'reaction': [{'manifestation': [{'text': 'Hives'}]}],
    'patient': {'reference': 'urn:uuid:test-patient-1'},
}
# Same shape, but the urn is one no Patient row in the tenant carries — the
# ingester minted a fresh id for a Bundle entry that had none, so the
# reference can no longer be tied to anybody.
ORPHANED_ALLERGY = {
    'resourceType': 'AllergyIntolerance', 'id': 'allergy-orphan',
    'code': {'text': 'Penicillin'},
    'reaction': [{'manifestation': [{'text': 'Hives'}]}],
    'patient': {'reference': 'urn:uuid:9d2c8f16-not-a-stored-id'},
}

# Copy the page must (or must not) render. Asserted as literals so a reworded
# absence claim cannot slip back in silently.
ABSENCE_LINE = 'No allergies found in your records'
UNREADABLE_LINE = 'We could not read your records'
ATTESTATION_CAVEAT = 'confirms nothing about what is on file'


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


def _staged_form_fill(client, tenant_headers, auth_headers, subject_ref=None):
    """propose + commit a form-fill action -> awaiting_confirmation."""
    body = FORM_FILL_BODY
    if subject_ref is not None:
        payload = dict(FORM_FILL_BODY['payload'])
        payload['subject'] = {'reference': subject_ref}
        body = dict(FORM_FILL_BODY, payload=payload)
    r = client.post('/r6/actions/propose', json=body,
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


# ---------------------------------------------------------------------------
# "Looked, found none" vs "could not resolve the patient" (#390)
#
# The absence line sits directly above the no-known-allergies attestation, so
# rendering it from a lookup that never resolved a patient walks a skimming
# patient into attesting to a sentence nothing checked. Both states must stay
# distinguishable in BOTH directions: swapping the false claim for silence
# would trade a wrong answer for no answer.
# ---------------------------------------------------------------------------

def test_get_review_no_patient_row_does_not_claim_no_allergies(
        client, app, tenant_headers, auth_headers):
    """A tenant whose source sent clinical resources before demographics."""
    _seed(app, tenant_headers['X-Tenant-Id'], [MED_A])   # no Patient row
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    resp = _get(client, auth_headers, action_id)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    html = resp.get_data(as_text=True)
    assert ABSENCE_LINE not in html
    assert UNREADABLE_LINE in html
    # The attestation is still offered — it is the patient's own statement —
    # but it is not framed as agreeing with anything we read.
    assert 'name="nka"' in html
    assert ATTESTATION_CAVEAT in html


def test_get_review_urn_uuid_subject_resolves_the_patient(
        client, app, tenant_headers, auth_headers):
    """`urn:uuid:` is valid FHIR, so resolve it rather than give up on it."""
    _seed(app, tenant_headers['X-Tenant-Id'], [PATIENT, URN_ALLERGY])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers,
                                  subject_ref='urn:uuid:test-patient-1')

    resp = _get(client, auth_headers, action_id)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    html = resp.get_data(as_text=True)
    # Resolved both ways: the action's urn subject AND the allergy's urn
    # patient reference. The allergy renders, so there is no absence claim.
    assert 'Penicillin' in html
    assert 'Smith' in html
    assert ABSENCE_LINE not in html
    assert UNREADABLE_LINE not in html
    assert ATTESTATION_CAVEAT not in html


def test_get_review_unresolvable_urn_does_not_claim_no_allergies(
        client, app, tenant_headers, auth_headers):
    """The half of the urn case that cannot be resolved: allergy records are
    held, but their reference names no patient we hold. That is an unread
    record, not an empty one."""
    _seed(app, tenant_headers['X-Tenant-Id'], [PATIENT, ORPHANED_ALLERGY])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    resp = _get(client, auth_headers, action_id)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    html = resp.get_data(as_text=True)
    assert ABSENCE_LINE not in html
    assert UNREADABLE_LINE in html
    assert ATTESTATION_CAVEAT in html


def test_get_review_resolved_empty_record_still_says_none_found(
        client, app, tenant_headers, auth_headers):
    """The other direction. A patient we DID resolve, whose record genuinely
    holds no allergies, still gets the honest absence line — otherwise this
    fix trades a false claim for no information at all."""
    _seed(app, tenant_headers['X-Tenant-Id'], [PATIENT, MED_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    resp = _get(client, auth_headers, action_id)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    html = resp.get_data(as_text=True)
    assert ABSENCE_LINE in html
    assert UNREADABLE_LINE not in html
    assert ATTESTATION_CAVEAT not in html


def test_gather_content_states_are_distinguishable(app, tenant_id):
    """The two states at the source, not just in the rendered page."""
    from r6.actions.review import (CONTENT_OK, CONTENT_UNRESOLVED,
                                   _gather_content)
    with app.app_context():
        for resource in (PATIENT, ALLERGY_A):
            db.session.add(R6(resource, tenant_id))
        db.session.commit()

        resolved = _gather_content(tenant_id, dict(PATIENT))
        assert resolved.status == CONTENT_OK
        assert resolved.reason == ''
        assert any(r['resourceType'] == 'AllergyIntolerance'
                   for r in resolved.resources)

        unresolved = _gather_content(tenant_id, None)
        assert unresolved.status == CONTENT_UNRESOLVED
        assert unresolved.reason           # names WHY, never a bare empty list
        assert unresolved.resources == []


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


def test_post_next_step_does_not_claim_an_outcome_it_cannot_know(
        client, app, tenant_headers, auth_headers):
    """#645: this handler stages a confirmation row (ActionConfirmation) and
    returns BEFORE the separate confirm/execute call that actually renders
    the form — so it cannot know, at response time, whether that later call
    will produce a completed PDF or a needs_review/failed outcome. The old
    text asserted one specific outcome ("the form-fill executor currently
    returns an honest needs_review placeholder") — true when written, false
    once form-fill was actually implemented, and nobody read the string
    again to notice. Pin the PROPERTY, not a phrase: no wording produced
    here may name a terminal, verifiable outcome ('completed', 'placeholder',
    'generated', a task number) as if it already happened.

    MUTATION: restore the deleted 'Task 8'/'placeholder' string -> red.
    """
    tenant = tenant_headers['X-Tenant-Id']
    _seed(app, tenant, [PATIENT, MED_A, MED_B])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    resp = _post(client, auth_headers, action_id,
                 {'med-0': 'yes', 'med-1': 'no', 'nka': 'true'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    next_step = resp.get_json()['next_step']

    # This handler DID succeed at what it does — say that plainly.
    assert 'Review recorded' in next_step

    # It must not assert what only the later execute() call can determine.
    forbidden = ('Task 8', 'placeholder', 'needs_review', 'completed',
                'generated.', 'was generated')
    hits = [w for w in forbidden if w in next_step]
    assert not hits, (
        f'next_step claims an outcome this handler cannot know: {hits} '
        f'in {next_step!r}')


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


def test_post_payload_is_final_before_the_confirmation_is_issued(
        client, app, tenant_headers, auth_headers, monkeypatch):
    """The confirmation is the human's signature over the payload; the
    payload must not move after it is minted (human-gate spec §9 R2, #528).

    Snapshot payload_json at the instant issue_confirmation() runs and
    compare it with what is stored after the request. Before the fix the
    review route appended reviewed_qr_id AFTER minting, so the two differed
    and the ledger could not say what was approved."""
    import r6.actions.review as review
    from r6.actions import confirmations

    tenant = tenant_headers['X-Tenant-Id']
    _seed(app, tenant, [PATIENT, MED_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    seen = {}

    def _snapshotting_issue(aid, approved_via, ttl_minutes):
        seen['payload_json'] = db.session.get(ProposedAction, aid).payload_json
        return confirmations.issue_confirmation(aid, approved_via, ttl_minutes)

    monkeypatch.setattr(review, 'issue_confirmation', _snapshotting_issue)

    resp = _post(client, auth_headers, action_id,
                 {'med-0': 'yes', 'nka': 'true'})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    with app.app_context():
        action = db.session.get(ProposedAction, action_id)
        assert action.payload['reviewed_qr_id']           # hand-off kept
        assert seen['payload_json'] == action.payload_json


def test_second_submit_answers_409_instead_of_raising_the_seal(
        client, app, tenant_headers, auth_headers, monkeypatch):
    """A resubmitted review is refused, not crashed (#528 follow-on).

    This route never transitions the action, so _load_form_fill_action keeps
    accepting it after a successful submit — and the payload is sealed from
    the moment the first submit mints a confirmation. Before the pre-check,
    the second POST raised PayloadSealed out of the handler: a Werkzeug HTML
    500, which action_review.html's `r.json()` rejects with no .catch, so the
    patient's screen does not move and a double-tap is the ordinary way in.

    Pins the refusal AND that the first review is left completely alone: one
    confirmation, the original reviewed_qr_id, and no second (orphan)
    QuestionnaireResponse row.

    Also pins what the pre-check specifically buys over the except branch
    that backs it: the doomed request is refused BEFORE _draft_qr re-reads
    the patient's record out of FHIR. Without that assertion the pre-check is
    unpinned — the except branch answers 409 either way and the rollback
    hides the QuestionnaireResponse insert.
    """
    import r6.actions.review as review
    from r6.models import R6Resource

    tenant = tenant_headers['X-Tenant-Id']
    _seed(app, tenant, [PATIENT, MED_A, ALLERGY_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    first = _post(client, auth_headers, action_id,
                  {'med-0': 'yes', 'allergy-0': 'confirm'})
    assert first.status_code == 200, first.get_data(as_text=True)
    with app.app_context():
        qr_id = db.session.get(ProposedAction, action_id).payload[
            'reviewed_qr_id']
        qr_count = R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse', tenant_id=tenant).count()

    # Same action, DIFFERENT answers — the swap the seal exists to refuse.
    # _draft_qr must not run: a request that cannot succeed does not re-read
    # the record.
    def _must_not_repopulate(*a, **kw):
        raise AssertionError('re-read FHIR for an already-submitted review')

    monkeypatch.setattr(review, '_draft_qr', _must_not_repopulate)
    second = _post(client, auth_headers, action_id,
                   {'med-0': 'no', 'allergy-0': 'remove', 'nka': 'true'})
    monkeypatch.undo()
    assert second.status_code == 409, second.get_data(as_text=True)

    # The BODY SHAPE the approve page reads, not just the status.
    # templates/action_review.html branches on `res.r.status === 409` AFTER
    # parsing the body; an unparseable one takes its `.catch` arm instead,
    # which can only say "we could not read the answer". A JSON object here
    # is what lets the page say the specific, true thing. Asserted before the
    # `['error']` read below, which already fails on a non-JSON body but as a
    # bare TypeError that names nothing.
    #
    # This travels to the other surface unchanged: careagents/app.py
    # review_submit relays body and status verbatim, because
    # _answered_about_data(409) is true, so the CareAgents patient's copy of
    # this page branches off the same answer.
    assert second.mimetype == 'application/json', (
        'a non-JSON 409 lands in the page unreadable-body branch, which '
        'cannot tell the patient their approval already went through')
    parsed = second.get_json()
    assert isinstance(parsed, dict), parsed
    # A 409 is a DEFINITE answer. `confirmed: null` is the #416 third-answer
    # shape, and the page keys a neighbouring branch on it.
    assert parsed.get('confirmed', False) is not None, (
        'a 409 must not carry the unknown-outcome shape')
    assert 'already been submitted' in parsed['error']

    with app.app_context():
        action = db.session.get(ProposedAction, action_id)
        assert action.status == 'awaiting_confirmation'
        assert action.payload['reviewed_qr_id'] == qr_id
        assert ActionConfirmation.query.filter_by(
            action_id=action_id).count() == 1
        assert R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse',
            tenant_id=tenant).count() == qr_count


def test_second_submit_racing_the_precheck_answers_409_not_500(
        client, app, tenant_headers, auth_headers, monkeypatch):
    """The pre-check is not a lock. Two submits milliseconds apart both pass
    it and the loser reaches the seal with the winner's confirmation already
    committed. Neutralising the pre-check reproduces exactly that branch: it
    must answer 409 too, not raise.

    The validator does its own function-local import of has_confirmation, so
    patching the name in this module reaches the route's pre-check only —
    the seal itself still fires. That is the point of the test.
    """
    import r6.actions.review as review

    tenant = tenant_headers['X-Tenant-Id']
    _seed(app, tenant, [PATIENT, MED_A, ALLERGY_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    first = _post(client, auth_headers, action_id,
                  {'med-0': 'yes', 'allergy-0': 'confirm'})
    assert first.status_code == 200, first.get_data(as_text=True)

    monkeypatch.setattr(review, 'has_confirmation', lambda _aid: False)
    second = _post(client, auth_headers, action_id,
                   {'med-0': 'no', 'allergy-0': 'remove', 'nka': 'true'})
    assert second.status_code == 409, second.get_data(as_text=True)

    with app.app_context():
        assert ActionConfirmation.query.filter_by(
            action_id=action_id).count() == 1


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


def test_review_refuses_a_read_scoped_token(client, app, tenant_headers,
                                            auth_headers):
    """The review surface is where a human approves a clinical write.

    `_require_step_up`'s docstring called the credential "tenant-bound",
    but `validate_step_up_token`'s `require_scope` defaults to 'write', so
    the code had always demanded more than the prose described. Kernel
    slice 5 makes it Scope.WRITE and this pins the behaviour the default
    was providing silently — swapping in Scope.TENANT_BOUND left the entire
    suite green before this test existed.

    MUTATION: scope=Scope.TENANT_BOUND in _require_step_up -> red.
    """
    from r6.stepup import generate_step_up_token

    _seed(app, tenant_headers['X-Tenant-Id'], [PATIENT, MED_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    resp = _get(client, {
        'X-Tenant-Id': tenant_headers['X-Tenant-Id'],
        'X-Step-Up-Token': generate_step_up_token(
            tenant_headers['X-Tenant-Id'], scope='read'),
    }, action_id)
    assert resp.status_code == 401, resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# #581 item 1: the content list handed to the populate engine never holds the
# Patient. The Patient arrives as `subject`; a copy inside the list was dead
# weight sitting beside a redaction boundary, the same door $populate closed
# in #578 after proving nothing read it.
# ---------------------------------------------------------------------------

def test_the_content_list_never_holds_the_patient(app, tenant_id, monkeypatch):
    """MUTATION: r6/actions/review.py, seed the list with the patient again
    (`content = [patient]`) -> red."""
    from r6.actions import review
    from r6.models import R6Resource
    from models import db

    with app.app_context():
        db.session.add(R6Resource(
            resource_type='Patient', resource_id='p-581', tenant_id=tenant_id,
            resource_json='{"resourceType":"Patient","id":"p-581","name":[{"family":"Quux581"}]}'))
        db.session.add(R6Resource(
            resource_type='AllergyIntolerance', resource_id='a-581', tenant_id=tenant_id,
            resource_json='{"resourceType":"AllergyIntolerance","id":"a-581",'
                          '"patient":{"reference":"Patient/p-581"},'
                          '"code":{"text":"peanut-581"}}'))
        db.session.commit()
        patient = review._load_patient(tenant_id, 'Patient/p-581')
        assert patient and patient['id'] == 'p-581'
        content = review._gather_content(tenant_id, patient, 'Patient/p-581')
        assert content.resolved
        types = [r['resourceType'] for r in content.resources]
        assert 'Patient' not in types
        assert types == ['AllergyIntolerance']       # the record still reaches the engine

        # And what the engine is handed on the page's own path: the subject
        # separately, the list without it.
        seen = {}
        real = review.populate_questionnaire

        def _capture(questionnaire, subject, content_resources):
            seen['subject'] = subject
            seen['content'] = list(content_resources)
            return real(questionnaire, subject, content_resources)

        monkeypatch.setattr(review, 'populate_questionnaire', _capture)
        from r6.actions.models import ProposedAction
        action = ProposedAction(tenant_id=tenant_id, kind='form-fill',
                                payload={'subject': {'reference': 'Patient/p-581'}})
        db.session.add(action)
        db.session.commit()
        review._draft_qr(action, tenant_id)
        assert seen['subject']['id'] == 'p-581'
        assert all(r['resourceType'] != 'Patient' for r in seen['content'])
