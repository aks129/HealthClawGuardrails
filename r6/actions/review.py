"""Structured per-item review page (Task 6) — the CORE SAFETY UI.

Two routes on the actions blueprint:

  GET  /r6/actions/<id>/review  — render a per-item confirmation page for a
       form-fill action: Demographics read-only; each populated medication a
       row with a required Still-taking? decision; each populated allergy a
       row with a confirm/remove decision PLUS the explicit, UNCHECKED
       "No known allergies (patient confirmed)" checkbox; each condition
       confirmable. Provenance ("from your records") on populated items.
       A patient we could NOT resolve renders as an unread record, never as
       an empty one — the absence line sits directly above the attestation,
       so claiming it from a lookup that never ran walks the patient into
       affirming it (#390). See IntakeContent.

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
from dataclasses import dataclass, field

from flask import render_template, request

from models import db
from r6.actions.confirmations import has_confirmation, issue_confirmation
from r6.actions.models import PayloadSealed, ProposedAction
from r6.actions.routes import _error, _tenant_or_none, actions_blueprint
from r6.audit import record_audit_event
from r6.models import R6Resource
from r6.sdc.intake import intake_questionnaire
from r6.sdc.populate import populate_questionnaire
from r6.access import Scope, require_grant

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

# The intake content has three states, not two: rows we found, a record we
# read that holds none, and a patient we could not resolve at all. The third
# exists because the first two are both ANSWERS, and the empty list that used
# to stand in for a failure renders as "No allergies found in your records"
# directly above the no-known-allergies attestation (#390) — walking a
# skimming patient into agreeing with a sentence a lookup emptied. "No known
# allergies" is never inferred; neither is the context the patient reads
# before affirming it.
#
# Modelled on r6/labs/interpret.py's _indeterminate(): a result we could not
# produce carries a REASON and is never dressed up as a clean one.
CONTENT_OK = 'ok'
CONTENT_UNRESOLVED = 'unresolved'

CONTENT_REASON_NO_PATIENT = 'no patient record has reached this account yet'
CONTENT_REASON_UNKNOWN_SUBJECT = (
    'the form names a patient record this account does not hold')
CONTENT_REASON_UNANCHORED = (
    'part of the record refers to a patient we could not identify')


@dataclass
class IntakeContent:
    """The FHIR content behind the review page, plus whether we resolved the
    patient it is supposed to belong to.

    Deliberately not a bare list. An empty list carries two meanings and the
    difference between them is the whole point, so callers reach through
    `.resources` and cannot mistake "could not resolve" for "nothing on
    file". `status` defaults to unresolved — ok is earned, never assumed.
    """
    resources: list = field(default_factory=list)
    status: str = CONTENT_UNRESOLVED
    reason: str = CONTENT_REASON_NO_PATIENT

    @property
    def resolved(self):
        return self.status == CONTENT_OK


_URN_UUID_PREFIX = 'urn:uuid:'


def _referenced_patient_id(reference):
    """The Patient id a subject/patient reference points at, or None.

    Two shapes resolve. The relative `Patient/<id>` form, and the
    `urn:uuid:<id>` form a transaction Bundle uses for entries that reference
    each other by fullUrl — which is how bundles arrive, and which the literal
    string compare this replaced could never match (#390). Anything else (an
    absolute URL, another resource type) resolves to None: the caller then
    treats the record as unread rather than guessing whose it is.
    """
    if not isinstance(reference, str):
        return None
    ref = reference.strip()
    if ref.startswith(_URN_UUID_PREFIX):
        return ref[len(_URN_UUID_PREFIX):] or None
    if ref.startswith('Patient/'):
        return ref.split('/', 1)[1] or None
    return None


def _require_step_up(tenant):
    """Require a write-capable step-up token for `tenant`, or raise.

    Access kernel, slice 5. Multi-use validation (no nonce consume) so
    GET-then-POST with one token works.

    Scope.WRITE, not TENANT_BOUND, despite the wording this docstring used
    to carry. `validate_step_up_token`'s `require_scope` defaults to
    'write', so the two-argument call this replaces was already refusing a
    read-scoped token. The old text said "tenant-bound" and the code
    demanded write — the kind of gap between prose and behaviour that the
    kernel exists to close, so it is fixed here rather than preserved.

    Raises StepUpDenied rather than returning an error tuple. Callers no
    longer branch on the result, so there is no `if err is not None` to
    forget.
    """
    require_grant(
        scope=Scope.WRITE,
        tenant=tenant,
        absent_status=401,
        rejected_status=401,
    )


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
        # `urn:uuid:<id>` first (a Bundle's own fullUrl reference), then the
        # last path segment, which covers `Patient/<id>` and an absolute URL.
        ident = (_referenced_patient_id(subject_ref)
                 or subject_ref.split('/')[-1])
        row = R6Resource.query.filter_by(
            resource_type='Patient', id=ident, tenant_id=tenant_id).first()
        return row.to_fhir_json() if row else None
    row = R6Resource.query.filter_by(
        resource_type='Patient', tenant_id=tenant_id).first()
    return row.to_fhir_json() if row else None


def _gather_content(tenant_id, patient, subject_ref=None):
    """The patient's FHIR content, and whether we resolved the patient at all.

    An unresolved patient is NOT an empty record, and the difference is the
    whole return type (see IntakeContent). Only a resolved, genuinely empty
    record may be rendered as an absence (#390).
    """
    if not (patient and patient.get('id')):
        return IntakeContent(
            reason=(CONTENT_REASON_UNKNOWN_SUBJECT if subject_ref
                    else CONTENT_REASON_NO_PATIENT))

    patient_id = patient['id']
    ref = 'Patient/%s' % patient_id
    content = [patient]
    unanchored = False
    for resource_type, subject_field in _CONTENT_TYPES:
        for row in R6Resource.query.filter_by(
                resource_type=resource_type, tenant_id=tenant_id).all():
            resource = row.to_fhir_json()
            reference = (resource.get(subject_field) or {}).get('reference')
            if _referenced_patient_id(reference) == patient_id:
                # Canonicalize a resolved urn onto the relative form so the
                # populate engine, which matches `Patient/<id>` alone, sees
                # it. to_fhir_json() hands back a fresh dict each call, so
                # this rewrites the copy the page renders — never the store.
                resource[subject_field] = dict(resource[subject_field],
                                               reference=ref)
                content.append(resource)
            elif isinstance(reference, str) \
                    and reference.startswith(_URN_UUID_PREFIX):
                # A Bundle entry whose fullUrl reference names no id we hold:
                # the ingester minted a fresh id for a patient that arrived
                # without one, so this record can no longer be tied to
                # anybody. We are holding clinical data we cannot read, which
                # is the one thing the page must not report as "none found".
                unanchored = True

    if unanchored:
        return IntakeContent(resources=content,
                             reason=CONTENT_REASON_UNANCHORED)
    return IntakeContent(resources=content, status=CONTENT_OK, reason='')


def _draft_qr(action, tenant_id):
    """Populate the action's questionnaire from the tenant's FHIR -> draft QR.
    Deterministic: population order fixes the med/allergy/condition row indices
    used by both the rendered page and the POST gate.

    Returns the content marker alongside the QR — an empty QR alone cannot say
    whether the record is empty or unread, and the page has to say which."""
    questionnaire = _resolve_questionnaire(action, tenant_id)
    subject_ref = (action.payload.get('subject') or {}).get('reference') \
        if isinstance(action.payload.get('subject'), dict) else None
    patient = _load_patient(tenant_id, subject_ref)
    content = _gather_content(tenant_id, patient, subject_ref)
    qr, _issues = populate_questionnaire(questionnaire, patient,
                                         content.resources)
    return questionnaire, patient, qr, content


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
    tenant = _tenant_or_none()
    if tenant is None:
        return _error(400, 'X-Tenant-Id header is required')
    tenant_id = tenant.id
    _require_step_up(tenant)

    action = _load_form_fill_action(action_id, tenant_id)
    if action is None:
        return _error(404, 'Unknown action')

    _questionnaire, _patient, draft_qr, content = _draft_qr(action, tenant_id)
    demographics = _demographics(draft_qr)
    meds, allergies, conditions = _view_rows(draft_qr)

    record_audit_event(
        'read', resource_type='ProposedAction', resource_id=action.id,
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        # The reasons are fixed strings from this module, never record text,
        # so the detail stays PHI-free. Without this, the state that renders
        # the unreadable notice is invisible in production.
        detail=('review page rendered' if content.resolved else
                'review page rendered; record unreadable: %s' % content.reason),
    )
    html = render_template(
        'action_review.html', action_id=action_id, demographics=demographics,
        meds=meds, allergies=allergies, conditions=conditions,
        record_readable=content.resolved, record_reason=content.reason,
        # step_up_token is deliberately NOT passed. It is a write credential,
        # and handing it to a template is how it reached the patient's browser
        # across an origin boundary (#395). The submit path supplies its own
        # credentials server-side; nothing in the page needs this.
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
    tenant = _tenant_or_none()
    if tenant is None:
        return _error(400, 'X-Tenant-Id header is required')
    tenant_id = tenant.id
    _require_step_up(tenant)

    action = _load_form_fill_action(action_id, tenant_id)
    if action is None:
        return _error(404, 'Unknown action')

    # A submitted review is final. This route does NOT transition the action
    # (the confirm route's claim does), so the loader above keeps accepting an
    # action that has already been reviewed — and the payload is sealed the
    # moment the first submit mints a confirmation (#528). Without this the
    # second submit reaches the sealed assignment below and raises out of the
    # handler as a 500; the review page calls r.json() on every response, so a
    # Werkzeug HTML 500 rejects the promise and the patient sees nothing move
    # at all. A phone double-tap is the ordinary way to reach that. Answer
    # instead, before any FHIR work or QuestionnaireResponse row is created.
    if has_confirmation(action_id):
        return _error(409, 'This review has already been submitted. Your '
                           'answers were recorded and approved; nothing '
                           'further is needed.')

    # RE-POPULATE from FHIR: the server, not the client, decides which rows
    # exist and must be acted on. This is what makes the gate un-craftable.
    _questionnaire, patient, draft_qr, _content = _draft_qr(action, tenant_id)
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

    # (3a) The two answers cannot both be true. "No known allergies" beside a
    # confirmed allergy row is not a stricter reading of the same fact, it is
    # a contradiction, and the reviewed response would carry both (#667). The
    # extraction engine ignores the attestation structurally and writes the
    # confirmed rows, so the record would hold an allergy while the response
    # attests there are none. The message names both halves and neither the
    # substance nor the reaction: the person is looking at the row.
    if nka_affirmed and confirmed_allergy:
        return _error(422, 'You confirmed an allergy and also checked "No '
                           'known allergies (patient confirmed)". Both cannot '
                           'be true: uncheck the box, or remove the allergy '
                           'rows you confirmed.')

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

    # (6) Hand-off marker for Task 8 goes into the payload FIRST, then the
    # consent record. The confirmation is the human's signature over the
    # payload as it stands, and payload_json is sealed once it exists (#528)
    # — the other order minted the signature and then changed what it signed.
    # The pre-check above is not a lock: two submits milliseconds apart both
    # pass it, and the loser reaches the seal here with the winner's
    # confirmation already committed. Same answer as the pre-check — this is
    # the branch that made it a 500, and the FHIR re-population between the
    # two is exactly wide enough for a double-tap to land in it.
    try:
        payload = action.payload
        payload['reviewed_qr_id'] = qr_row.id
        action.payload_json = json.dumps(payload)
        issue_confirmation(action_id, approved_via='review-page',
                           ttl_minutes=15)
        db.session.commit()
    except PayloadSealed:
        db.session.rollback()
        return _error(409, 'This review has already been submitted. Your '
                           'answers were recorded and approved; nothing '
                           'further is needed.')

    record_audit_event(
        'update', resource_type='ProposedAction', resource_id=action.id,
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        detail='reviewed via review-page; qr=%s' % qr_row.id,
    )

    # #645: this response used to assert the executor's OUTCOME — "the
    # form-fill executor currently returns an honest needs_review
    # placeholder" — as if this handler could see it. It can't: this handler
    # only stages a confirmation row (issue_confirmation, above); execution
    # happens on a separate call (r6/actions/routes.py's confirm route),
    # which this function returns before. A caller reading only this
    # response has no way to know the true outcome yet, and the old string
    # was itself only ever a guess — one that outlived being true, so a
    # tester who generated a real PDF was told nothing had been generated.
    # Say only what this handler actually knows: the review was recorded.
    from flask import jsonify
    return jsonify({
        'id': action.id,
        'status': action.status,
        'reviewed_qr_id': qr_row.id,
        'approved_via': 'review-page',
        'next_step': ('Review recorded and approval issued. The form is '
                      'generated once the confirmation above is claimed and '
                      'executed — check the action\'s own status for the '
                      'outcome.'),
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
