"""
Action executors — Bland.ai (phone calls) and Twilio (SMS).

SIMULATION MODE: when the relevant API keys are absent, executors return a
successful simulated result instead of making network calls. This keeps
local dev, CI, and the demo stack fully functional with zero credentials,
matching the contract careagents.cloud used before this module existed.

No retries by design: a double-placed phone call is worse than a failed one.
"""

import base64
import logging
import os
import secrets
import urllib.parse
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds


@dataclass
class ExecutionResult:
    ok: bool
    simulated: bool
    external_ref: str | None = None
    error: str | None = None
    outcome_unknown: bool = False  # True when the provider MAY have acted (timeout/garbled response after send) — caller must map to status 'unknown', never 'failed', to prevent a re-proposed duplicate call.


def _webhook_url(provider, action_id):
    base = os.environ.get('PUBLIC_BASE_URL', 'https://app.healthclaw.io')
    secret = os.environ.get('ACTIONS_WEBHOOK_SECRET', '')
    params = {'action_id': action_id or ''}
    if secret:
        params['secret'] = secret
    return '%s/r6/actions/callback/%s?%s' % (
        base, provider, urllib.parse.urlencode(params))


def _execute_call(payload, action_id):
    phone = payload.get('phone')
    if not phone:
        return ExecutionResult(ok=False, simulated=False,
                               error='phone number is required')
    api_key = os.environ.get('BLAND_AI_API_KEY')
    if not api_key:
        return ExecutionResult(ok=True, simulated=True,
                               external_ref='sim-' + secrets.token_hex(6))
    resp = requests.post(
        'https://api.bland.ai/v1/calls',
        headers={'Authorization': api_key,
                 'Content-Type': 'application/json'},
        json={
            'phone_number': phone,
            'task': payload.get('body', ''),
            'voice': 'maya',
            'webhook': _webhook_url('bland', action_id),
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code >= 500:
        logger.error('Bland.ai error %s', resp.status_code)
        return ExecutionResult(ok=False, simulated=False, outcome_unknown=True,
                               error='Call provider error (HTTP %s) — outcome unknown' % resp.status_code)
    if resp.status_code != 200:
        logger.error('Bland.ai error %s', resp.status_code)
        return ExecutionResult(ok=False, simulated=False,
                               error='Call provider error (HTTP %s)' % resp.status_code)
    return ExecutionResult(ok=True, simulated=False,
                           external_ref=resp.json().get('call_id'))


def _execute_sms(payload, action_id):
    phone = payload.get('phone')
    if not phone:
        return ExecutionResult(ok=False, simulated=False,
                               error='phone number is required')
    sid = os.environ.get('TWILIO_ACCOUNT_SID')
    token = os.environ.get('TWILIO_AUTH_TOKEN')
    from_num = os.environ.get('TWILIO_FROM_NUMBER')
    configured = [v for v in (sid, token, from_num) if v]
    if not configured:
        return ExecutionResult(ok=True, simulated=True,
                               external_ref='sim-' + secrets.token_hex(6))
    if len(configured) != 3:
        logger.error('Twilio partially configured — refusing to simulate')
        return ExecutionResult(ok=False, simulated=False,
                               error='SMS provider misconfigured')
    auth = base64.b64encode(('%s:%s' % (sid, token)).encode()).decode()
    resp = requests.post(
        'https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json' % sid,
        headers={'Authorization': 'Basic ' + auth},
        data={'To': phone, 'From': from_num, 'Body': payload.get('body', ''),
              'StatusCallback': _webhook_url('twilio', action_id)},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code >= 500:
        logger.error('Twilio error %s', resp.status_code)
        return ExecutionResult(ok=False, simulated=False, outcome_unknown=True,
                               error='SMS provider error (HTTP %s) — outcome unknown' % resp.status_code)
    if resp.status_code not in (200, 201):
        logger.error('Twilio error %s', resp.status_code)
        return ExecutionResult(ok=False, simulated=False,
                               error='SMS provider error (HTTP %s)' % resp.status_code)
    return ExecutionResult(ok=True, simulated=False,
                           external_ref=resp.json().get('sid'))


_EXECUTORS = {
    'phone-call': _execute_call,
    'insurance-call': _execute_call,   # same transport, different script source
    'sms': _execute_sms,
}


def execute_action(kind, payload, action_id=None):
    """Dispatch to the executor for this action kind."""
    executor = _EXECUTORS.get(kind)
    if executor is None:
        return ExecutionResult(ok=False, simulated=False,
                               error='No executor for kind: %s' % kind)
    try:
        return executor(payload, action_id)
    except requests.Timeout as exc:
        logger.error('Executor timeout: %s', type(exc).__name__)
        return ExecutionResult(ok=False, simulated=False, outcome_unknown=True,
                               error='Provider timed out — outcome unknown')
    except requests.ConnectionError as exc:
        # A TCP reset can occur after the request was received by the provider;
        # conservative mapping prevents a duplicate call on re-propose.
        logger.error('Executor connection error: %s', type(exc).__name__)
        return ExecutionResult(ok=False, simulated=False, outcome_unknown=True,
                               error='Connection error — outcome unknown')
    except requests.exceptions.JSONDecodeError as exc:
        logger.error('Executor bad response: %s', type(exc).__name__)
        return ExecutionResult(ok=False, simulated=False, outcome_unknown=True,
                               error='Provider response unreadable — outcome unknown')
    except requests.RequestException as exc:
        logger.error('Executor network failure: %s', type(exc).__name__)
        return ExecutionResult(ok=False, simulated=False,
                               error='Provider unreachable')
