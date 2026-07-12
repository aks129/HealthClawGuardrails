"""Twilio SMS rail — the fail-loud ActionExecutor for kind 'sms'. No silent
simulation: a missing/invalid provider config is a loud
ExecutionResult(status='failed', error=PROVIDER_NOT_CONFIGURED), never a
fake success. See r6/actions/rails/__init__.py for how registration works.
"""

import base64
import os

from r6.actions import errors
from r6.actions.rails import (
    RECONCILE_COMPLETED_STATUSES,
    RECONCILE_FAILED_STATUSES,
    _ProviderTransportError,
    _safe_json,
    _safe_request,
    _webhook_url,
    reconcile_needs_review,
)
from r6.actions.registry import ExecutionResult, register_executor


def _basic_auth(sid, token):
    return 'Basic ' + base64.b64encode(('%s:%s' % (sid, token)).encode()).decode()


class SmsExecutor:
    kind = 'sms'
    required_env = ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER')

    def validate(self, payload):
        phone = payload.get('phone')
        body = payload.get('body')
        if not isinstance(phone, str) or not phone or not isinstance(body, str) or not body:
            return [errors.PAYLOAD_INVALID]
        return []

    def execute(self, action):
        payload = action.payload
        sid = os.environ.get('TWILIO_ACCOUNT_SID')
        token = os.environ.get('TWILIO_AUTH_TOKEN')
        from_num = os.environ.get('TWILIO_FROM_NUMBER')
        if not (sid and token and from_num):
            return ExecutionResult(status='failed', error=errors.PROVIDER_NOT_CONFIGURED)
        try:
            resp = _safe_request(
                'POST', 'https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json' % sid,
                headers={'Authorization': _basic_auth(sid, token)},
                data={
                    'To': payload.get('phone'),
                    'From': from_num,
                    'Body': payload.get('body', ''),
                    'StatusCallback': _webhook_url('twilio', action.id),
                },
            )
        except _ProviderTransportError as exc:
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR,
                                   outcome_unknown=exc.outcome_unknown)
        if resp.status_code >= 500:
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR,
                                   outcome_unknown=True)
        if resp.status_code not in (200, 201):
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR)
        try:
            data = _safe_json(resp)
        except _ProviderTransportError as exc:
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR,
                                   outcome_unknown=exc.outcome_unknown)
        return ExecutionResult(status='executing', provider_ref=data.get('sid'))

    def reconcile(self, action):
        sid = os.environ.get('TWILIO_ACCOUNT_SID')
        token = os.environ.get('TWILIO_AUTH_TOKEN')
        if not (sid and token):
            return ExecutionResult(status='failed', error=errors.PROVIDER_NOT_CONFIGURED)
        url = 'https://api.twilio.com/2010-04-01/Accounts/%s/Messages/%s.json' % (
            sid, action.external_ref)
        try:
            resp = _safe_request('GET', url, headers={'Authorization': _basic_auth(sid, token)})
            if resp.status_code >= 400:
                return reconcile_needs_review()
            data = _safe_json(resp)
        except _ProviderTransportError:
            return reconcile_needs_review()
        status = str(data.get('status', '')).lower()
        if status in RECONCILE_COMPLETED_STATUSES:
            return ExecutionResult(status='completed', outcome=data)
        if status in RECONCILE_FAILED_STATUSES:
            return ExecutionResult(status='failed', outcome=data, error=errors.PROVIDER_ERROR)
        return ExecutionResult(status='executing', outcome=data)


def register():
    register_executor(SmsExecutor())


register()
