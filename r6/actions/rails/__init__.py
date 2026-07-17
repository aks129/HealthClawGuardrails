"""Rail executors — the new fail-loud ActionExecutor implementations that
register themselves with r6.actions.registry.

Each rail module (phone.py, sms.py, ...) defines module-level state plus a
`register()` function that calls register_executor() for its kind, and
calls that function once at import time so importing this package alone
populates the registry at boot.

Registration idempotency: register_executor() raises ValueError on a
duplicate kind, and Python caches module imports, so a naive "register only
at import" pattern breaks tests that _clear() the registry and then
re-import the rail module — it's already in sys.modules, so nothing
re-runs and the registry stays empty. register_all() below is the fix: it
calls each rail's register() function directly (not via import), swallowing
the duplicate-kind ValueError. Tests should do `_clear(); register_all()`
to reach a known-good registry state rather than relying on import order.

Scope note: 'insurance-call' registers here too (Task 10) — a tiny subclass
of PhoneCallExecutor in phone.py that only overrides kind (same Bland.ai
transport; the insurance script source lives with the proposer). 'form-fill'
(Task 3) is also registered now, but its execute() is a skeleton: it fails
loud without PUBLIC_BASE_URL and otherwise returns an honest needs_review
placeholder — the full populate/review/render/DocumentReference orchestration
lands in Task 8.
"""

import logging
import os
import urllib.parse

import requests

from r6.actions.registry import ExecutionResult

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds


class _ProviderTransportError(Exception):
    """Raised by _safe_request/_safe_json on a transport-level failure.
    Carries whether the provider MAY have already acted (timeouts,
    connection resets, and unreadable responses all count — a re-propose
    after one of these could double-dial/double-text) so callers can set
    ExecutionResult.outcome_unknown accordingly.
    """

    def __init__(self, outcome_unknown):
        super().__init__('provider transport error')
        self.outcome_unknown = outcome_unknown


def _safe_request(method, url, **kwargs):
    """requests.request(), classifying transport failures the way the old
    r6/actions/executors.py did: timeouts and connection errors are
    outcome_unknown (the provider may have received the request before the
    network gave up), other request exceptions are not. Raises
    _ProviderTransportError; never raises a bare requests exception."""
    try:
        return requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    except requests.Timeout as exc:
        logger.error('Provider timeout: %s %s (%s)', method, url, type(exc).__name__)
        raise _ProviderTransportError(outcome_unknown=True) from exc
    except requests.ConnectionError as exc:
        logger.error('Provider connection error: %s %s (%s)', method, url, type(exc).__name__)
        raise _ProviderTransportError(outcome_unknown=True) from exc
    except requests.RequestException as exc:
        logger.error('Provider unreachable: %s %s (%s)', method, url, type(exc).__name__)
        raise _ProviderTransportError(outcome_unknown=False) from exc


def _safe_json(resp):
    """resp.json(), mapping an unparseable body to _ProviderTransportError
    (outcome_unknown=True — the provider responded, so it likely acted, we
    just can't read what it said)."""
    try:
        return resp.json()
    except requests.exceptions.JSONDecodeError as exc:
        logger.error('Provider response unreadable: %s', type(exc).__name__)
        raise _ProviderTransportError(outcome_unknown=True) from exc


def _webhook_url(provider, action_id):
    """Same shape as the old executors.py helper — provider callbacks POST
    back here to resolve an 'executing' action asynchronously."""
    base = os.environ.get('PUBLIC_BASE_URL', 'https://app.healthclaw.io')
    secret = os.environ.get('ACTIONS_WEBHOOK_SECRET', '')
    params = {'action_id': action_id or ''}
    if secret:
        params['secret'] = secret
    return '%s/r6/actions/callback/%s?%s' % (
        base, provider, urllib.parse.urlencode(params))


# Shared provider-status vocabulary for reconcile(): both phone (Bland.ai)
# and SMS (Twilio) map onto this same three-way split per the rail spec.
RECONCILE_COMPLETED_STATUSES = ('completed', 'delivered')
RECONCILE_FAILED_STATUSES = ('failed', 'no-answer', 'busy', 'canceled', 'undelivered')


def reconcile_needs_review():
    """reconcile() must never invent a verdict when the provider can't be
    reached — this is that non-verdict."""
    return ExecutionResult(status='needs_review', outcome={'reason': 'reconcile_unreachable'})


from r6.actions.rails import form_fill, phone, sms, webhook_poster  # noqa: E402  (registration side effect)


def register_all():
    """(Re-)register every rail executor. Idempotent: safe to call after
    registry._clear(), and safe to call repeatedly — duplicate-kind
    registrations are swallowed."""
    for module in (phone, sms, form_fill, webhook_poster):
        try:
            module.register()
        except ValueError:
            pass
