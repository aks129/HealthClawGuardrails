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

        # (4)-(7) Render -> persist -> link. Any failure here maps to a loud
        # PROVIDER_ERROR — an exception must never escape as a fake success.
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

            # (6) Persist the PDF as a FHIR DocumentReference.
            docref = persist_intake_document(
                action.tenant_id, subject_ref, pdf_bytes,
                title='Completed intake form',
                questionnaire_response_id=reviewed_qr_id)
            docref_id = docref['id']

            # (7) Build a signed, expiring download link.
            link = build_document_link(action.tenant_id, docref_id)
        except Exception as exc:  # noqa: BLE001 — fail loud, never fake success
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


def reviewed_subject_label(reviewed_qr):
    """The human name for the PDF title: the first and last name answers in
    the reviewed response, exactly as the review page showed them.

    Snapshotted at review and bound by the answers digest (#1171), so no
    write after the review changes whose form it says it is. It is never
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
    """True when this is the delivered DocumentReference or the reviewed
    QuestionnaireResponse of a completed form-fill (#1171).

    Both were ordinary tenant resources, so the write token the proposing
    agent holds could rewrite them after the person approved, and the
    signed link then served the agent's bytes. Keyed on server-owned state
    only: the completed action's provider ref and its sealed payload.
    """
    from r6.actions.models import ProposedAction
    if resource_type not in ('DocumentReference', 'QuestionnaireResponse'):
        return False
    completed = ProposedAction.query.filter_by(
        tenant_id=tenant_id, kind='form-fill', status='completed')
    if resource_type == 'DocumentReference':
        return completed.filter_by(
            external_ref=resource_id).first() is not None
    return any(a.payload.get('reviewed_qr_id') == resource_id
               for a in completed.all())


def register():
    register_executor(FormFillExecutor())


register()
