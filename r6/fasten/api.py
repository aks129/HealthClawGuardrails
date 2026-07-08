"""Server-side Fasten Connect API client (Basic auth; keys never in browser).

One call for now: trigger the EHI export. Fasten does NOT export records
automatically after a connection — POST /v1/bridge/fhir/ehi-export must be
called with the org_connection_id (idempotent: repeat calls return the
existing task). Results arrive asynchronously via the ehi_export_success
webhook. Discovered live 2026-07-08 when a verified connection produced no
export for 24 hours.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

_FASTEN_API_BASE = os.environ.get(
    'FASTEN_API_BASE', 'https://api.connect.fastenhealth.com/v1').rstrip('/')


def trigger_ehi_export(org_connection_id: str):
    """Idempotently request a bulk records export for a connection.

    Returns {task_id, status} on success, None when keys are missing or the
    call fails (callers treat this as non-fatal: the export can be triggered
    again — idempotent — and the webhook path is unaffected).
    """
    public = os.environ.get('FASTEN_PUBLIC_KEY', '').strip()
    private = os.environ.get('FASTEN_PRIVATE_KEY', '').strip()
    if not public or not private:
        logger.warning('FASTEN_PUBLIC_KEY/FASTEN_PRIVATE_KEY not set — '
                       'cannot trigger EHI export (records will not arrive '
                       'until an export is requested)')
        return None
    try:
        resp = requests.post(
            f'{_FASTEN_API_BASE}/bridge/fhir/ehi-export',
            auth=(public, private),
            json={'org_connection_id': org_connection_id},
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.error('EHI export trigger failed: %s', type(exc).__name__)
        return None
    if resp.status_code not in (200, 201):
        logger.error('EHI export trigger: http %s', resp.status_code)
        return None
    data = (resp.json() or {}).get('data') or {}
    logger.info('EHI export triggered: task=%s status=%s',
                data.get('task_id'), data.get('status'))
    return {'task_id': data.get('task_id'), 'status': data.get('status')}
