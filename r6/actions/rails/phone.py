"""Bland.ai phone-call rail — the fail-loud ActionExecutor for kind
'phone-call'. No silent simulation: a missing/invalid provider config is a
loud ExecutionResult(status='failed', error=PROVIDER_NOT_CONFIGURED), never
a fake success. See r6/actions/rails/__init__.py for how registration works.
"""

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


def _api_key():
    # BLAND_AI_API_KEY is the documented name (see required_env below);
    # BLAND_API_KEY is accepted as an alias so a key stored under either
    # spelling dials for real, matching the old executors.py behavior.
    return os.environ.get('BLAND_AI_API_KEY') or os.environ.get('BLAND_API_KEY')


class PhoneCallExecutor:
    kind = 'phone-call'
    required_env = ('BLAND_AI_API_KEY',)  # BLAND_API_KEY alias handled in _api_key()

    def validate(self, payload):
        phone = payload.get('phone')
        body = payload.get('body')
        if not isinstance(phone, str) or not phone or not isinstance(body, str) or not body:
            return [errors.PAYLOAD_INVALID]
        return []

    def execute(self, action):
        payload = action.payload
        api_key = _api_key()
        if not api_key:
            return ExecutionResult(status='failed', error=errors.PROVIDER_NOT_CONFIGURED)
        try:
            resp = _safe_request(
                'POST', 'https://api.bland.ai/v1/calls',
                headers={'Authorization': api_key, 'Content-Type': 'application/json'},
                json={
                    'phone_number': payload.get('phone'),
                    'task': payload.get('body', ''),
                    'voice': 'maya',
                    'webhook': _webhook_url('bland', action.id),
                },
            )
        except _ProviderTransportError as exc:
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR,
                                   outcome_unknown=exc.outcome_unknown)
        if resp.status_code >= 500:
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR,
                                   outcome_unknown=True)
        if resp.status_code != 200:
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR)
        try:
            data = _safe_json(resp)
        except _ProviderTransportError as exc:
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR,
                                   outcome_unknown=exc.outcome_unknown)
        return ExecutionResult(status='executing', provider_ref=data.get('call_id'))

    def reconcile(self, action):
        api_key = _api_key()
        if not api_key:
            return ExecutionResult(status='failed', error=errors.PROVIDER_NOT_CONFIGURED)
        try:
            resp = _safe_request(
                'GET', 'https://api.bland.ai/v1/calls/%s' % action.external_ref,
                headers={'Authorization': api_key},
            )
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


class InsuranceCallExecutor(PhoneCallExecutor):
    """Same Bland.ai transport as phone-call; the kind differs so insurance
    scripts stay a distinct, separately auditable action kind (script source
    lives with the proposer, not the rail)."""
    kind = 'insurance-call'


def register():
    # Per-executor duplicate swallow: a partially populated registry (e.g. a
    # test registered one kind manually) must not stop the other kind from
    # registering.
    for executor in (PhoneCallExecutor(), InsuranceCallExecutor()):
        try:
            register_executor(executor)
        except ValueError:
            pass


register()
