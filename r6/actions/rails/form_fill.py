"""form-fill rail — the ActionExecutor for kind 'form-fill' (Task 8).

execute() is now the real end-to-end orchestration: it takes the human-
reviewed QuestionnaireResponse handed over by the review page (Task 6),
renders it to a PDF (Task 4), persists that PDF as a FHIR DocumentReference
(Task 5), and returns a signed, expiring download link (Task 7).

Safety posture is unchanged and load-bearing: this rail fails loud, never
fabricates a completed form.
  - PUBLIC_BASE_URL unset               -> failed / PROVIDER_NOT_CONFIGURED
  - no reviewed QR on the action        -> needs_review (never 'completed')
  - reviewed QR vanished (deleted/stale)-> failed / STALE_SOURCE_DATA
  - reviewed QR not the reviewed answers-> needs_review (#1163)
  - any render/persist/link exception   -> failed / PROVIDER_ERROR
Only on a fully rendered+persisted+linked PDF does it return 'completed'.
"""

import os
import unicodedata
import uuid

from r6 import constant_time
from r6.actions import errors
from r6.actions.confirmations import reviewed_answers_digest
from r6.actions.registry import ExecutionResult, register_executor
from r6.models import R6Resource


class FormFillExecutor:
    kind = 'form-fill'
    # PUBLIC_BASE_URL is genuinely required: the delivery link handed to the
    # patient/staff (and the render step in Task 8) is built from it, the
    # same way the phone/sms webhook callback URLs are (see
    # r6.actions.rails._webhook_url).
    required_env = ('PUBLIC_BASE_URL',)

    def validate(self, payload):
        from r6.sdc.intake import INTAKE_REFERENCES
        questionnaire = payload.get('questionnaire')
        body = payload.get('body')
        if (not isinstance(questionnaire, str) or not questionnaire
                or not isinstance(body, str) or not body):
            return [errors.PAYLOAD_INVALID]
        # Only the built-in intake (#1170). Refused rather than ignored, so
        # the proposal on file always names the form that is rendered.
        if questionnaire.strip() not in INTAKE_REFERENCES:
            return [errors.PAYLOAD_INVALID]
        return []

    def execute(self, action):
        # (1) Env check comes before any payload-specific logic: a dark rail
        # fails loud regardless of what the caller sent.
        if not os.environ.get('PUBLIC_BASE_URL'):
            return ExecutionResult(status='failed',
                                   error=errors.PROVIDER_NOT_CONFIGURED)

        # (2) Require a human-reviewed QuestionnaireResponse. Its id is stamped
        # onto the action by the review page (Task 6). No reviewed QR means the
        # form was never reviewed — we DO NOT fabricate one and DO NOT emit a
        # 'completed' form; we fall back to needs_review (the honest state).
        reviewed_qr_id = action.payload.get('reviewed_qr_id')
        if not reviewed_qr_id:
            return ExecutionResult(
                status='needs_review',
                outcome={'reason': 'form not reviewed — open the review page '
                                   'and confirm each item first'},
            )

        # (3) Load the reviewed QR, tenant-scoped. If it vanished (deleted or
        # stale), the reviewed answers are gone: fail loud, never render a
        # blank/guessed form.
        #
        # `is_deleted=False` is what makes that comment true (#509). It named
        # deletion as the case it handled and the query did not filter, so a
        # soft-deleted QuestionnaireResponse was loaded and RENDERED into a
        # form submitted on a patient's behalf — the exact "blank/guessed
        # form" outcome the sentence above promises to prevent. A control
        # that reads as one thing and does another, with a comment supplying
        # the misreading.
        row = R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse', id=reviewed_qr_id,
            tenant_id=action.tenant_id, is_deleted=False).first()
        if row is None:
            return ExecutionResult(status='failed',
                                   error=errors.STALE_SOURCE_DATA)

        # (3a) The answers must be the ones the person reviewed (#1163). The
        # payload names the response by id, and a write token can PUT over
        # that id, so an id alone let the answers change between the review
        # and this read. The review route stores a keyed digest of the stored
        # response beside the id; a payload without one was never reviewed
        # here (an id planted at propose has none it could compute), and a
        # mismatch means the response was rewritten. Either way nothing is
        # rendered: the honest state is needs_review, as for no review.
        expected = action.payload.get('reviewed_qr_digest')
        if not isinstance(expected, str) or not constant_time.equal(
                expected, reviewed_answers_digest(action.id,
                                                  row.resource_json)):
            return ExecutionResult(
                status='needs_review',
                outcome={'reason': 'the answers on file are not the ones '
                                   'that were reviewed, so no form was '
                                   'made'},
            )
        reviewed_qr = row.to_fhir_json()

        # (3b) Only the built-in intake's items are rendered (#1170). The
        # review now builds the response from fixed ids, but one reviewed
        # before that change still carries whatever leaf a planted
        # Questionnaire added, under a digest that matches. The PDF labels
        # an unknown leaf by its linkId, which that Questionnaire chose, and
        # a known linkId out of its place (the attestation inside the
        # demographics group) prints the intake's own label beside an answer
        # nobody gave. So each item must sit where the intake puts it, with
        # the answer type the intake gives it; otherwise the response is
        # refused, never rendered without the item: the person's review
        # covered a form that is not this one.
        if _off_form(reviewed_qr):
            return ExecutionResult(
                status='needs_review',
                outcome={'reason': 'the reviewed answers include items that '
                                   'are not on the intake form, so no form '
                                   'was made; review it again'})
        # (3c) The same for a demographic answer outside the printable
        # bound (#1174): one reviewed before the bound existed could put a
        # sentence in the title or a row.
        if not demographics_printable(reviewed_qr):
            return ExecutionResult(
                status='needs_review',
                outcome={'reason': 'a reviewed answer cannot be printed on '
                                   'the form, so no form was made; review '
                                   'it again'})

        # (4)-(7) Render -> persist -> link. Any failure here maps to a loud
        # PROVIDER_ERROR — an exception must never escape as a fake success.
        docref_id = None
        try:
            from r6.sdc.pdf import render_questionnaire_response_pdf
            from r6.sdc.documents import persist_intake_document
            from r6.sdc.delivery import build_document_link

            # (4) Labels come from the built-in intake, never a stored
            # Questionnaire (#1169): a write token can store one under any
            # id after the review, and the label is what the printed answer
            # reads as. The reviewed response carries no item text of its
            # own (review.py builds it from fixed ids), so this is the only
            # source of every label on the form.
            from r6.sdc.intake import intake_questionnaire
            questionnaire = intake_questionnaire()

            # (5) Render the reviewed answers to a PDF. The title name comes
            # from the reviewed answers (#1171), which the review took and
            # the digest above binds. The Patient is not read here: a write
            # token can rename it between the review and this call.
            subject_ref = (reviewed_qr.get('subject') or {}).get('reference')
            subject_label = reviewed_subject_label(reviewed_qr)
            reviewed_on = reviewed_qr.get('authored')
            pdf_bytes = render_questionnaire_response_pdf(
                reviewed_qr, questionnaire=questionnaire,
                reviewed_on=reviewed_on, subject_label=subject_label)

            # (6) Persist the PDF as a FHIR DocumentReference. Its id is
            # recorded on the action first (#1173): the lock on a delivered
            # document keys on that server-owned ref, so it is in force
            # before the document exists, not from 'completed' on.
            docref_id = str(uuid.uuid4())
            _set_ref(action, None, docref_id)
            persist_intake_document(
                action.tenant_id, subject_ref, pdf_bytes,
                title='Completed intake form',
                questionnaire_response_id=reviewed_qr_id,
                resource_id=docref_id)

            # (7) Build a signed, expiring download link.
            link = build_document_link(action.tenant_id, docref_id)
        except Exception as exc:  # noqa: BLE001 — fail loud, never fake success
            # A failed execution delivers nothing, so it holds no lock.
            if docref_id is not None:
                _set_ref(action, docref_id, None)
            # Truncate to keep the taxonomy detail PHI-free-ish; do not log PHI.
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR,
                                   outcome={'detail': str(exc)[:200]})

        # (8) Success: the docref id is the provider_ref; the link + ids ride
        # in the outcome so the confirm route can surface them.
        return ExecutionResult(
            status='completed', provider_ref=docref_id,
            outcome={'document_reference_id': docref_id,
                     'delivery_link': link,
                     'questionnaire_response_id': reviewed_qr_id})

    def reconcile(self, action):
        # form-fill has no async provider webhook (unlike Bland.ai/Twilio) and
        # execute() is now synchronous and terminal — it either completes the
        # render/persist/link in one shot or fails loud. There is no external
        # status to poll, so keep this honest: needs_review, never an invented
        # verdict.
        return ExecutionResult(
            status='needs_review',
            outcome={'reason': 'form-fill execute() is synchronous and '
                               'terminal — nothing to reconcile'},
        )


#: Characters a printed demographic answer may hold beside letters, marks
#: and digits: enough for names, addresses and phone numbers, and no colon,
#: so an answer cannot read as a "label: value" row.
_PRINTABLE_PUNCTUATION = frozenset(" .,'-/#&+()")
PRINTABLE_MAX = 64


def printable_answer(value):
    """True for a string answer fit to print on the form: at most
    PRINTABLE_MAX characters of letters, marks, digits and a little
    punctuation (#1174). One predicate for the review page, the reviewed
    response and the executor."""
    if not isinstance(value, str) or not 0 < len(value) <= PRINTABLE_MAX:
        return False
    # Letters by category, not isalnum(): modifier letters (Lm) include the
    # look-alike colon U+02D0. A mark is accepted only on a letter, and only
    # a generic COMBINING diacritic or one of that letter's own script, so a
    # Devanagari visarga (U+0903) cannot sit on a Latin name as a colon
    # (#1175 R2-2).
    script = None
    for ch in value:
        category = unicodedata.category(ch)
        name = unicodedata.name(ch, '')
        if category.startswith('L') and category != 'Lm':
            script = name.split(' ', 1)[0]
        elif category.startswith('M') and script and (
                name.startswith('COMBINING ')
                or name.split(' ', 1)[0] == script):
            continue
        elif category == 'Nd' or ch in _PRINTABLE_PUNCTUATION:
            script = None
        else:
            return False
    return True


def demographics_printable(questionnaire_response):
    """True when every string answer in the demographics group passes
    printable_answer."""
    for group in questionnaire_response.get('item') or []:
        if not isinstance(group, dict) or group.get('linkId') != 'demographics':
            continue
        for item in group.get('item') or []:
            for answer in (item.get('answer') or []
                           if isinstance(item, dict) else []):
                for value in (answer.values()
                              if isinstance(answer, dict) else ()):
                    if isinstance(value, str) and not printable_answer(value):
                        return False
    return True


#: The answer keys each intake item type may carry ("choice" is a string
#: from the Patient, or a coding from an observation).
_ANSWER_KEYS = {'string': {'valueString'}, 'date': {'valueDate'},
                'boolean': {'valueBoolean'},
                'choice': {'valueString', 'valueCoding'}}


def _off_form(questionnaire_response):
    """True when any item is not where the built-in intake puts it: an
    unknown linkId, a known one under another parent, a second copy of an
    item that does not repeat, or an answer of another type (#1172)."""
    from r6.sdc.intake import intake_questionnaire

    def off(items, spec_items):
        spec = {q['linkId']: q for q in spec_items or []}
        seen = set()
        for item in items or []:
            if not isinstance(item, dict):
                return True
            q = spec.get(item.get('linkId'))
            if q is None or (item['linkId'] in seen and not q.get('repeats')):
                return True
            seen.add(item['linkId'])
            if q['type'] == 'group':
                if item.get('answer') or off(item.get('item'), q.get('item')):
                    return True
                continue
            keys = _ANSWER_KEYS.get(q['type'], set())
            if item.get('item') or not all(
                    isinstance(a, dict) and a and set(a) <= keys
                    for a in item.get('answer') or []):
                return True
        return False
    return off(questionnaire_response.get('item'),
               intake_questionnaire().get('item'))


def reviewed_subject_label(reviewed_qr):
    """The human name for the PDF title: the first and last name answers in
    the reviewed response.

    Taken when the review is submitted and bound by the answers digest
    (#1171), so no write after the submit changes whose form it says it is.
    The submit re-populates from the live Patient, so a rename between the
    page being shown and the submit is not caught here (#1174). It is never
    copied into the payload, which the status and confirm responses return.
    None when neither answer is present — the renderer treats a missing
    label as "no name". The populate step reads the structured name only,
    never `name.text` (#367).
    """
    answers = {}
    for group in reviewed_qr.get('item') or []:
        if group.get('linkId') != 'demographics':
            continue
        for item in group.get('item') or []:
            for answer in item.get('answer') or []:
                value = answer.get('valueString')
                if isinstance(value, str):
                    answers[item.get('linkId')] = value
    label = ' '.join(answers[k] for k in ('demographics.given-name',
                                          'demographics.family-name')
                     if answers.get(k))
    return label or None


def delivered_record_locked(tenant_id, resource_type, resource_id):
    """True for the reviewed QuestionnaireResponse and the delivered
    DocumentReference of a form-fill that is executing or completed (#1171).

    Both were ordinary tenant resources, so the write token the proposing
    agent holds could rewrite them after the person approved, and the
    signed link then served the agent's bytes. The lock holds from the
    moment execution starts, not from 'completed': the PDF is committed
    inside execute(), before the action is marked completed, and a write in
    that window was accepted. Keyed on server-owned state only: the
    action's status, its sealed reviewed_qr_id, and the document id
    execute() records in external_ref before persisting. Never on the
    document's own content, which a write token can set on any document
    (#1173 R2-3: a `relatesTo` key let it pin forged content for good).
    """
    from r6.actions.models import ProposedAction
    if resource_type not in ('DocumentReference', 'QuestionnaireResponse'):
        return False
    actions = ProposedAction.query.filter(
        ProposedAction.tenant_id == tenant_id,
        ProposedAction.kind == 'form-fill',
        ProposedAction.status.in_(('executing', 'completed'))).all()
    if resource_type == 'QuestionnaireResponse':
        return any(a.payload.get('reviewed_qr_id') == resource_id
                   for a in actions)
    return any(a.external_ref == resource_id for a in actions)


def _set_ref(action, expected, ref):
    """Record (or clear) the delivered document's id on the executing
    action, committed, so another request's lock check reads it."""
    from models import db
    from r6.actions.models import ProposedAction
    ProposedAction.query.filter_by(
        id=action.id, status='executing', external_ref=expected).update(
        {'external_ref': ref}, synchronize_session=False)
    db.session.commit()


def register():
    register_executor(FormFillExecutor())


register()
