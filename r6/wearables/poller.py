"""
Background poller that syncs Open Wearables -> HealthClaw.

One daemon thread runs while the Flask app is alive. Every
WEARABLES_POLL_INTERVAL seconds it:

  1. Loads all WearableConnection rows.
  2. Fetches deltas per connection via WearablesClient.
  3. Maps samples to FHIR Observations via mapper.samples_to_bundle.
  4. POSTs each bundle to /Bundle/$ingest-context with a step-up token
     scoped to the tenant.
  5. Updates last_sync_at / last_sync_status / observation_count.

Errors on one connection do not stop the loop. The thread is opt-in —
only starts when OPEN_WEARABLES_URL is set at process startup. Not
suitable for serverless runtimes (Vercel). Run via docker-compose or
Railway for continuous operation.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from r6.stepup import generate_step_up_token
from r6.wearables.client import WearablesClient
from r6.wearables.mapper import samples_to_bundle
from r6.wearables.models import WearableConnection

logger = logging.getLogger(__name__)

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _poll_interval() -> int:
    try:
        return max(60, int(os.environ.get('WEARABLES_POLL_INTERVAL', '900')))
    except ValueError:
        return 900


def _ingest_base_url() -> str:
    # When running in-process we need a reachable self URL. Defaults match
    # docker-compose + local dev. Override with WEARABLES_INGEST_BASE_URL.
    return os.environ.get(
        'WEARABLES_INGEST_BASE_URL',
        'http://localhost:5000/r6/fhir',
    ).rstrip('/')


def run_once(app, client: WearablesClient | None = None) -> dict:
    """
    One sync pass across all WearableConnection rows.

    Returns a summary dict {connections_checked, observations_ingested,
    errors}. Safe to call manually (e.g. from /wearables/sync-now).
    """
    wc = client or WearablesClient()
    if not wc.enabled():
        return {
            'connections_checked': 0,
            'observations_ingested': 0,
            'errors': 0,
            'skipped_reason': 'OPEN_WEARABLES_URL not set',
        }

    checked = 0
    ingested = 0
    errors = 0

    with app.app_context():
        from models import db
        connections = WearableConnection.query.all()
        for conn in connections:
            checked += 1
            try:
                ingested += _sync_one(conn, wc, app)
                conn.last_sync_at = _now()
                conn.last_sync_status = 'ok'
                conn.last_sync_detail = None
            except Exception as exc:  # noqa: BLE001
                errors += 1
                conn.last_sync_at = _now()
                conn.last_sync_status = 'error'
                conn.last_sync_detail = str(exc)[:500]
                logger.warning(
                    'wearable sync failed tenant=%s provider=%s: %s',
                    conn.tenant_id, conn.provider, exc,
                )
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

    return {
        'connections_checked': checked,
        'observations_ingested': ingested,
        'errors': errors,
    }


def _sync_one(
    conn: WearableConnection,
    wc: WearablesClient,
    app,
) -> int:
    """Sync a single connection. Returns the number of observations posted."""
    samples = wc.fetch_deltas(
        ow_user_id=conn.ow_user_id,
        provider=conn.provider,
        since=conn.last_sync_at,
        limit=int(os.environ.get('WEARABLES_BATCH_LIMIT', '200')),
    )
    if not samples:
        return 0

    patient_ref = conn.patient_ref or f'Patient/{conn.tenant_id}-self'
    bundle = samples_to_bundle(
        samples,
        patient_ref=patient_ref,
        provider=conn.provider,
        source_base_url=os.environ.get(
            'OPEN_WEARABLES_URL', 'https://open-wearables.local',
        ),
    )
    entries = bundle.get('entry') or []
    if not entries:
        return 0

    token = generate_step_up_token(
        conn.tenant_id, agent_id='wearable-sync', ttl_seconds=300,
    )
    url = f'{_ingest_base_url()}/Bundle/$ingest-context'
    with httpx.Client(timeout=30.0) as c:
        resp = c.post(
            url,
            json=bundle,
            headers={
                'Content-Type': 'application/fhir+json',
                'X-Tenant-Id': conn.tenant_id,
                'X-Step-Up-Token': token,
                'X-Agent-Id': 'wearable-sync',
            },
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f'ingest-context returned {resp.status_code}: '
            f'{resp.text[:200]}'
        )

    conn.observation_count = (conn.observation_count or 0) + len(entries)
    return len(entries)


def _loop(app) -> None:
    interval = _poll_interval()
    logger.info(
        'wearables poller started (interval=%ss, url=%s)',
        interval, os.environ.get('OPEN_WEARABLES_URL', '(unset)'),
    )
    while not _stop_event.is_set():
        # Wait first so we don't ingest on cold start before the app is
        # fully initialized. Sleep in small chunks so stop_poller is
        # responsive.
        slept = 0
        while slept < interval and not _stop_event.is_set():
            time.sleep(min(5, interval - slept))
            slept += 5
        if _stop_event.is_set():
            break
        try:
            summary = run_once(app)
            logger.info('wearables sync pass: %s', summary)
        except Exception as exc:  # noqa: BLE001
            logger.error('wearables poller crashed: %s', exc)


def start_poller(app) -> bool:
    """
    Start the daemon poller thread if OPEN_WEARABLES_URL is set. Returns
    True when started, False when skipped. Safe to call multiple times.
    """
    global _thread
    if not os.environ.get('OPEN_WEARABLES_URL'):
        logger.info('OPEN_WEARABLES_URL unset; wearables poller disabled')
        return False
    if _thread and _thread.is_alive():
        return True
    _stop_event.clear()
    _thread = threading.Thread(
        target=_loop, args=(app,), name='wearables-poller', daemon=True,
    )
    _thread.start()
    return True


def stop_poller(timeout: float = 5.0) -> None:
    """Signal the poller to stop. Used by tests."""
    _stop_event.set()
    global _thread
    if _thread and _thread.is_alive():
        _thread.join(timeout=timeout)
    _thread = None
