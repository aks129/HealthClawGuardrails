"""Structured per-item review page (Task 6) — the CORE SAFETY UI.

Two routes on the actions blueprint:

  GET  /r6/actions/<id>/review  — render a per-item confirmation page for a
       form-fill action: Demographics read-only; each populated medication a
       row with a required Still-taking? decision; each populated allergy a
       row with a confirm/remove decision PLUS the explicit, UNCHECKED
       "No known allergies (patient confirmed)" checkbox; each condition
       confirmable. Provenance ("from your records") on populated items.

  POST /r6/actions/<id>/review  — the SERVER-SIDE SAFETY GATE. It RE-POPULATES
       the questionnaire from the tenant's FHIR (never trusting the client
       about how many rows exist), then requires: every med row acted on, every
       allergy row acted on, AND (the NKA box affirmed OR >=1 allergy
       confirmed). A crafted POST that skips the allergy attestation is
       rejected 422 — silence about allergies is never consent, and NKA is
       never inferred from the absence of allergy data. On success it builds
       the reviewed QuestionnaireResponse (status completed, author = reviewing
       Device, source = Patient), persists it tenant-scoped, issues an
       ActionConfirmation (approved_via='review-page'), and stores the reviewed
       QR id on the action for Task 8's execute().

Auth mirrors /confirm: X-Tenant-Id + a tenant-bound X-Step-Up-Token. The token
is validated multi-use (not nonce-consumed) so the page can be re-opened and
submitted with the same credential; the load-bearing gate here is the
per-item + allergy-attestation check, not single-use.
"""
import json
import logging

from flask import render_template, request

from models import db
from r6.actions.confirmations import issue_confirmation
from r6.actions.models import ProposedAction
from r6.actions.routes import _error, _tenant_or_none, actions_blueprint
from r6.audit import record_audit_event
from r6.models import R6Resource
from r6.sdc.intake import intake_questionnaire
from r6.sdc.populate import populate_questionnaire
from r6.stepup import validate_step_up_token

logger = logging.getLogger(__name__)

_MED_ACTIONS = ('yes', 'no', 'remove')
_ITEM_ACTIONS = ('confirm', 'remove')

# Where each list resource references its patient (R4 is inconsistent —
# AllergyIntolerance uses `patient`, everything else `subject`).
_CONTENT_TYPES = (
    ('Observation', 'subject'),
    ('MedicationRequest', 'subject'),
    ('AllergyIntolerance', 'patient'),
    ('Condition', 'subject'),
)


def _require_step_up(tenant_id):
    """Return None if a valid tenant-bound step-up token is present, else an
    (response, status) error tuple. Multi-use validation (no nonce consume) so
    GET-then-POST with one token works."""
    token = request.headers.get('X-Step-Up-Token')
    if not token:
        return _error(401, 'Review requires X-Step-Up-Token header')
    valid, err = validate_step_up_token(token, tenant_id)
    if not valid:
        return _error(401, 'Step-up token rejected: %s' % err)
    return None


def _load_form_fill_action(action_id, tenant_id):
    """Load a form-fill action that is tenant-scoped and awaiting_confirmation.
    Returns the action or None (caller maps None -> 404). A wrong tenant, wrong
    kind, or wrong status all collapse to 'not found' — no information leak."""
    action = ProposedAction.query.filter_by(
        id=action_id, tenant_id=tenant_id).first()
    if action is None:
        return None
    if action.kind != 'form-fill':
        return None
    if action.status != 'awaiting_confirmation':
        return None
    return action


def _resolve_questionnaire(action, tenant_id):
    """Resolve the action's questionnaire. A stored Questionnaire wins; the
    canonical intake form is the built-in fallback for 'healthclaw-intake'."""
    qref = (action.payload.get('questionnaire') or '').strip()
    ident = qref.split('/')[-1].split('|')[0]
    row = R6Resource.query.filter_by(
        resource_type='Questionnaire', id=ident, tenant_id=tenant_id).first()
    if row is not None:
        return row.to_fhir_json()
    if ident == 'healthclaw-intake' or not ident:
        return intake_questionnaire()
    return intake_questionnaire()


def _load_patient(tenant_id, subject_ref=None):
    if subject_ref:
        ident = subject_ref.split('/')[-1]
        row = R6Resource.query.filter_by(
            resource_type='Patient', id=ident, tenant_id=tenant_id).first()
        return row.to_fhir_json() if row else None
    row = R6Resource.query.filter_by(
        resource_type='Patient', tenant_id=tenant_id).first()
    return row.to_fhir_json() if row else None


def _gather_content(tenant_id, patient):
    content = []
    if patient:
        content.append(patient)
    if not (patient and patient.get('id')):
        return content
    ref = 'Patient/%s' % patient['id']
    for resource_type, subject_field in _CONTENT_TYPES:
        for row in R6Resource.query.filter_by(
                resource_type=resource_type, tenant_id=tenant_id).all():
            resource = row.to_fhir_json()
            if (resource.get(subject_field) or {}).get('reference') == ref:
                content.append(resource)
    return content


def _draft_qr(action, tenant_id):
    """Populate the action's questionnaire from the tenant's FHIR -> draft QR.
    Deterministic: population order fixes the med/allergy/condition row indices
    used by both the rendered page and the POST gate."""
    questionnaire = _resolve_questionnaire(action, tenant_id)
    subject_ref = (action.payload.get('subject') or {}).get('reference') \
        if isinstance(action.payload.get('subject'), dict) else None
    patient = _load_patient(tenant_id, subject_ref)
    content = _gather_content(tenant_id, patient)
    qr, _issues = populate_questionnaire(questionnaire, patient, content)
    return questionnaire, patient, qr


def _section_repeats(draft_qr, section_link_id, item_link_id):
    """Ordered list of populated repeat items for a repeating section group."""
    for group in draft_qr.get('item', []):
        if group.get('linkId') == section_link_id:
            return [child for child in group.get('item', [])
                    if child.get('linkId') == item_link_id]
    return []


def _leaf_value(repeat_item, leaf_link_id):
    for child in repeat_item.get('item', []):
        if child.get('linkId') == leaf_link_id:
            for ans in child.get('answer', []):
                if 'valueString' in ans:
                    return ans['valueString']
    return None


def _demographics(draft_qr):
    """Ordered (label, value) pairs from the populated demographics group."""
    labels = {
        'demographics.given-name': 'First name',
        'demographics.family-name': 'Last name',
        'demographics.birth-date': 'Date of birth',
        'demographics.gender': 'Gender',
        'demographics.phone': 'Phone',
        'demographics.address-line': 'Street address',
        'demographics.address-city': 'City',
        'demographics.address-state': 'State',
        'demographics.address-postal-code': 'Postal code',
    }
    out = []
    for group in draft_qr.get('item', []):
        if group.get('linkId') != 'demographics':
            continue
        for child in group.get('item', []):
            value = None
            for ans in child.get('answer', []):
                for key in ('valueString', 'valueDate', 'valueBoolean'):
                    if key in ans:
                        value = ans[key]
                if isinstance(ans.get('valueCoding'), dict):
                    value = ans['valueCoding'].get('display') \
                        or ans['valueCoding'].get('code')
            if value is not None:
                out.append((labels.get(child.get('linkId'),
                                       child.get('linkId')), value))
    return out


def _view_rows(draft_qr):
    """Build the template's per-item view model from the draft QR."""
    meds = []
    for row in _section_repeats(draft_qr, 'medications', 'medications.item'):
        meds.append({
            'name': _leaf_value(row, 'medications.item.name') or 'Medication',
            'dose': _leaf_value(row, 'medications.item.dose'),
        })
    allergies = []
    for row in _section_repeats(draft_qr, 'allergies', 'allergies.item'):
        allergies.append({
            'allergen': _leaf_value(row, 'allergies.item.allergen')
            or 'Allergy',
            'reaction': _leaf_value(row, 'allergies.item.reaction'),
        })
    conditions = []
    for row in _section_repeats(draft_qr, 'conditions', 'conditions.item'):
        conditions.append({
            'name': _leaf_value(row, 'conditions.item.name') or 'Condition',
        })
    return meds, allergies, conditions


@actions_blueprint.route('/<action_id>/review', methods=['GET'])
def review_form(action_id):
    tenant_id = _tenant_or_none()
    if not tenant_id:
        return _error(400, 'X-Tenant-Id header is required')
    auth_err = _require_step_up(tenant_id)
    if auth_err is not None:
        return auth_err

    action = _load_form_fill_action(action_id, tenant_id)
    if action is None:
        return _error(404, 'Unknown action')

    _questionnaire, _patient, draft_qr = _draft_qr(action, tenant_id)
    demographics = _demographics(draft_qr)
    meds, allergies, conditions = _view_rows(draft_qr)

    record_audit_event(
        'read', resource_type='ProposedAction', resource_id=action.id,
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        detail='review page rendered',
    )
    html = render_template(
        'action_review.html', action_id=action_id, demographics=demographics,
        meds=meds, allergies=allergies, conditions=conditions,
        step_up_token=request.headers.get('X-Step-Up-Token', ''),
        tenant_id=tenant_id)
    return html, 200


def _submitted(action_id):
    """Read the submitted decisions from JSON or form-encoded body."""
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return {k: ('' if v is None else str(v)) for k, v in body.items()}
    return {k: v for k, v in request.form.items()}


def _truthy(value):
    return str(value).strip().lower() in ('1', 'true', 'on', 'yes', 'checked')


@actions_blueprint.route('/<action_id>/review', methods=['POST'])
def review_submit(action_id):
    tenant_id = _tenant_or_none()
    if not tenant_id:
        return _error(400, 'X-Tenant-Id header is required')
    auth_err = _require_step_up(tenant_id)
    if auth_err is not None:
        return auth_err

    action = _load_form_fill_action(action_id, tenant_id)
    if action is None:
        return _error(404, 'Unknown action')

    # RE-POPULATE from FHIR: the server, not the client, decides which rows
    # exist and must be acted on. This is what makes the gate un-craftable.
    _questionnaire, patient, draft_qr = _draft_qr(action, tenant_id)
    med_rows = _section_repeats(draft_qr, 'medications', 'medications.item')
    allergy_rows = _section_repeats(draft_qr, 'allergies', 'allergies.item')
    condition_rows = _section_repeats(draft_qr, 'conditions',
                                      'conditions.item')
    submitted = _submitted(action_id)

    # (1) Every medication row must be acted on with a valid decision.
    med_decisions = []
    for i in range(len(med_rows)):
        decision = submitted.get('med-%d' % i, '').strip().lower()
        if decision not in _MED_ACTIONS:
            return _error(422, 'Every medication must be reviewed '
                               '(Still taking? Yes/No/Remove). Medication '
                               'row %d was not acted on.' % (i + 1))
        med_decisions.append(decision)

    # (2) Every allergy row must be acted on with a valid decision.
    allergy_decisions = []
    for i in range(len(allergy_rows)):
        decision = submitted.get('allergy-%d' % i, '').strip().lower()
        if decision not in _ITEM_ACTIONS:
            return _error(422, 'Every allergy must be reviewed '
                               '(Confirm/Remove). Allergy row %d was not '
                               'acted on.' % (i + 1))
        allergy_decisions.append(decision)

    # (3) THE ATTESTATION GATE (load-bearing): the patient must either confirm
    # at least one allergy OR explicitly affirm "no known allergies". Removing
    # every allergy row without checking NKA does NOT satisfy this — an absence
    # of allergies is never inferred, it must be affirmatively attested.
    nka_affirmed = _truthy(submitted.get('nka', ''))
    confirmed_allergy = any(d == 'confirm' for d in allergy_decisions)
    if not (nka_affirmed or confirmed_allergy):
        return _error(422, 'You must confirm at least one allergy OR check '
                           '"No known allergies (patient confirmed)". No '
                           'known allergies is never assumed.')

    # (4) Conditions are confirmable but not gating.
    condition_decisions = [
        submitted.get('condition-%d' % i, 'confirm').strip().lower()
        for i in range(len(condition_rows))]

    # (5) Build the reviewed QuestionnaireResponse from the human's decisions.
    reviewed_qr = _build_reviewed_qr(
        draft_qr, patient, med_rows, med_decisions, allergy_rows,
        allergy_decisions, nka_affirmed, condition_rows, condition_decisions)

    qr_row = R6Resource(resource_type='QuestionnaireResponse',
                        resource_json=json.dumps(reviewed_qr),
                        tenant_id=tenant_id)
    db.session.add(qr_row)
    db.session.flush()          # assign qr_row.id

    # (6) Consent record for the review approval + hand-off marker for Task 8.
    issue_confirmation(action_id, approved_via='review-page', ttl_minutes=15)
    payload = action.payload
    payload['reviewed_qr_id'] = qr_row.id
    action.payload_json = json.dumps(payload)
    db.session.commit()

    record_audit_event(
        'update', resource_type='ProposedAction', resource_id=action.id,
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        detail='reviewed via review-page; qr=%s' % qr_row.id,
    )

    from flask import jsonify
    return jsonify({
        'id': action.id,
        'status': action.status,
        'reviewed_qr_id': qr_row.id,
        'approved_via': 'review-page',
        'next_step': ('Review recorded and approval issued. Form generation '
                      '(PDF/DocumentReference) is Task 8; the form-fill '
                      'executor currently returns an honest needs_review '
                      'placeholder.'),
    }), 200


def _build_reviewed_qr(draft_qr, patient, med_rows, med_decisions,
                       allergy_rows, allergy_decisions, nka_affirmed,
                       condition_rows, condition_decisions):
    """Assemble the completed QuestionnaireResponse: demographics carried
    through, only kept meds/allergies/conditions included, and the NKA boolean
    set ONLY from the explicit attestation."""
    from r6.actions.models import _utcnow

    items = []
    for group in draft_qr.get('item', []):
        if group.get('linkId') == 'demographics':
            items.append(group)

    kept_meds = [row for row, decision in zip(med_rows, med_decisions)
                 if decision != 'remove']
    meds_group = {'linkId': 'medications', 'item': list(kept_meds)}
    items.append(meds_group)

    kept_allergies = [row for row, decision in zip(allergy_rows,
                                                   allergy_decisions)
                      if decision == 'confirm']
    allergy_children = [{
        'linkId': 'allergies.no-known-allergies',
        'answer': [{'valueBoolean': bool(nka_affirmed)}],
    }]
    allergy_children.extend(kept_allergies)
    items.append({'linkId': 'allergies', 'item': allergy_children})

    kept_conditions = [row for row, decision in zip(condition_rows,
                                                    condition_decisions)
                       if decision != 'remove']
    if kept_conditions:
        items.append({'linkId': 'conditions', 'item': list(kept_conditions)})

    qr = {
        'resourceType': 'QuestionnaireResponse',
        'status': 'completed',
        'questionnaire': draft_qr.get('questionnaire'),
        'authored': _utcnow().isoformat() + 'Z',
        # Reviewing Device authored the structured response; the patient is the
        # information source.
        'author': {'reference': 'Device/healthclaw-review',
                   'display': 'HealthClaw review page'},
        'source': {'reference': 'Patient'},
        'item': items,
    }
    subject = (patient or {})
    if subject.get('resourceType') and subject.get('id'):
        qr['subject'] = {'reference': 'Patient/%s' % subject['id']}
        qr['source'] = {'reference': 'Patient/%s' % subject['id']}
    return qr
