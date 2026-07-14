"""Synthetic webhook-poster rail.

This is the cookbook-sized ActionExecutor example for contributors. It posts a
synthetic payload to a configured webhook URL after the normal propose ->
commit -> out-of-band confirm rail has approved the action. It is deliberately
small and fail-loud: missing provider config or provider errors never become a
fake success.
"""

import os
from urllib.parse import urlparse

from r6.actions import errors
from r6.actions.rails import _ProviderTransportError, _safe_request
from r6.actions.registry import ExecutionResult, register_executor

_ENV_URL = 'WEBHOOK_POSTER_URL'
_ENV_TOKEN = 'WEBHOOK_POSTER_TOKEN'


def _configured_url():
    url = os.environ.get(_ENV_URL, '').strip()
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    return url


def _configured_token():
    token = os.environ.get(_ENV_TOKEN, '').strip()
    return token or None


class WebhookPosterExecutor:
    kind = 'webhook-poster'
    required_env = (_ENV_URL, _ENV_TOKEN)

    def validate(self, payload):
        to_label = payload.get('to')
        body = payload.get('body')
        metadata = payload.get('metadata', {})
        if not isinstance(to_label, str) or not to_label.strip():
            return [errors.PAYLOAD_INVALID]
        if not isinstance(body, str) or not body.strip():
            return [errors.PAYLOAD_INVALID]
        if metadata is not None and not isinstance(metadata, dict):
            return [errors.PAYLOAD_INVALID]
        return []

    def execute(self, action):
        url = _configured_url()
        token = _configured_token()
        if not (url and token):
            return ExecutionResult(status='failed',
                                   error=errors.PROVIDER_NOT_CONFIGURED)

        validation_errors = self.validate(action.payload)
        if validation_errors:
            return ExecutionResult(status='failed', error=errors.PAYLOAD_INVALID)

        payload = action.payload
        request_body = {
            'action_id': action.id,
            'kind': self.kind,
            'to': payload['to'],
            'body': payload['body'],
            'metadata': payload.get('metadata') or {},
        }
        try:
            resp = _safe_request(
                'POST', url,
                headers={
                    'Authorization': 'Bearer %s' % token,
                    'Content-Type': 'application/json',
                },
                json=request_body,
            )
        except _ProviderTransportError as exc:
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR,
                                   outcome_unknown=exc.outcome_unknown)

        if resp.status_code >= 500:
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR,
                                   outcome_unknown=True)
        if resp.status_code not in (200, 201, 202, 204):
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR)

        headers = getattr(resp, 'headers', {}) or {}
        provider_ref = None
        if hasattr(headers, 'get'):
            provider_ref = headers.get('X-Request-Id') or headers.get('x-request-id')
        return ExecutionResult(
            status='completed',
            provider_ref=provider_ref,
            outcome={'status_code': resp.status_code},
        )

    def reconcile(self, action):
        return ExecutionResult(
            status='needs_review',
            outcome={'reason': 'webhook-poster is synchronous; nothing to poll'},
        )


def register():
    register_executor(WebhookPosterExecutor())


register()
