"""form-fill rail — the ActionExecutor skeleton for kind 'form-fill'
(Task 3). Registered so form-fill joins the action rail like phone-call and
sms: it validates its payload shape and fails loud
(ExecutionResult(status='failed', error=PROVIDER_NOT_CONFIGURED)) when
PUBLIC_BASE_URL isn't configured, same fail-loud posture as the other
rails.

execute() is deliberately NOT the full orchestration yet. The real
form-fill flow is populate -> human review -> render -> DocumentReference ->
deliver a link built from PUBLIC_BASE_URL; that lands in Task 8. Until then,
execute() returns an honest ExecutionResult(status='needs_review') — never
a fabricated 'completed' — so a form-fill Approve is visibly incomplete
rather than silently wrong.
"""

import os

from r6.actions import errors
from r6.actions.registry import ExecutionResult, register_executor


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
        # Env check comes before any payload-specific logic: a dark rail
        # fails loud regardless of what the caller sent.
        if not os.environ.get('PUBLIC_BASE_URL'):
            return ExecutionResult(status='failed',
                                   error=errors.PROVIDER_NOT_CONFIGURED)
        # TODO(Task 8): populate -> human review -> render -> FHIR
        # DocumentReference -> deliver a shareable link built from
        # PUBLIC_BASE_URL. Until that lands, this is an honest placeholder:
        # fail safe (needs_review), never a fake 'completed' PDF/link.
        return ExecutionResult(
            status='needs_review',
            outcome={'reason': 'form-fill orchestration lands in Task 8'},
        )

    def reconcile(self, action):
        # form-fill has no async provider webhook (unlike Bland.ai/Twilio),
        # so there is no external status to poll. Keep it honest: report
        # needs_review rather than inventing a verdict.
        return ExecutionResult(
            status='needs_review',
            outcome={'reason': 'form-fill orchestration lands in Task 8'},
        )


def register():
    register_executor(FormFillExecutor())


register()
