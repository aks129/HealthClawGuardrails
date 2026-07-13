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
  - any render/persist/link exception   -> failed / PROVIDER_ERROR
Only on a fully rendered+persisted+linked PDF does it return 'completed'.
"""

import os

from r6.actions import errors
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
        questionnaire = payload.get('questionnaire')
        body = payload.get('body')
        if (not isinstance(questionnaire, str) or not questionnaire
                or not isinstance(body, str) or not body):
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
        row = R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse', id=reviewed_qr_id,
            tenant_id=action.tenant_id).first()
        if row is None:
            return ExecutionResult(status='failed',
                                   error=errors.STALE_SOURCE_DATA)
        reviewed_qr = row.to_fhir_json()

        # (4)-(7) Render -> persist -> link. Any failure here maps to a loud
        # PROVIDER_ERROR — an exception must never escape as a fake success.
        try:
            from r6.sdc.pdf import render_questionnaire_response_pdf
            from r6.sdc.documents import persist_intake_document
            from r6.sdc.delivery import build_document_link

            # (4) Resolve the questionnaire for nicer labels — non-fatal.
            questionnaire = self._resolve_questionnaire(
                reviewed_qr.get('questionnaire'), action.tenant_id)

            # (5) Render the reviewed answers to a PDF.
            subject_ref = (reviewed_qr.get('subject') or {}).get('reference')
            subject_label = self._subject_label(subject_ref, action.tenant_id)
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

    @staticmethod
    def _resolve_questionnaire(questionnaire_ref, tenant_id):
        """Best-effort load of the stored Questionnaire for nicer labels.

        Falls back to the canonical intake Questionnaire. Never raises — label
        resolution is a nicety, not a gate on delivering the reviewed answers.
        """
        from r6.sdc.intake import intake_questionnaire
        try:
            if questionnaire_ref:
                q_id = str(questionnaire_ref).split('|')[0]
                if q_id.startswith('Questionnaire/'):
                    q_id = q_id.split('/', 1)[1]
                q_id = q_id.rstrip('/').split('/')[-1]
                row = R6Resource.query.filter_by(
                    resource_type='Questionnaire', id=q_id,
                    tenant_id=tenant_id).first()
                if row is not None:
                    return row.to_fhir_json()
        except Exception:  # noqa: BLE001 — non-fatal; fall through to canonical
            pass
        try:
            return intake_questionnaire()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _subject_label(subject_ref, tenant_id):
        """Best-effort human name for the PDF title from the subject Patient.

        Returns None when nothing is easily available — the renderer treats a
        missing label as "no name", never a hard error.
        """
        if not subject_ref or not str(subject_ref).startswith('Patient/'):
            return None
        try:
            patient_id = str(subject_ref).split('/', 1)[1]
            row = R6Resource.query.filter_by(
                resource_type='Patient', id=patient_id,
                tenant_id=tenant_id).first()
            if row is None:
                return None
            names = (row.to_fhir_json().get('name') or [])
            if not names:
                return None
            name = names[0]
            if name.get('text'):
                return name['text']
            given = ' '.join(name.get('given') or [])
            label = ('%s %s' % (given, name.get('family') or '')).strip()
            return label or None
        except Exception:  # noqa: BLE001 — label is a nicety, never a gate
            return None

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


def register():
    register_executor(FormFillExecutor())


register()
