"""
HTTP client for the Open Wearables sidecar.

All calls use a single shared API key (OPEN_WEARABLES_API_KEY) as an
Authorization: Bearer header. No per-user tokens — Open Wearables owns
those.

Timeouts default to 15s to match FHIR_UPSTREAM_TIMEOUT; callers can
override for bulk backfills.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class WearablesClient:
    """Thin typed wrapper over the Open Wearables REST API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ):
        self.base_url = (
            base_url or os.environ.get('OPEN_WEARABLES_URL', '')
        ).rstrip('/')
        self.api_key = api_key or os.environ.get('OPEN_WEARABLES_API_KEY', '')
        self.timeout = float(
            timeout if timeout is not None
            else os.environ.get('OPEN_WEARABLES_TIMEOUT', '15')
        )

    def enabled(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict[str, str]:
        h = {'Accept': 'application/json'}
        if self.api_key:
            h['Authorization'] = f'Bearer {self.api_key}'
        return h

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers=self._headers(),
        )

    # -- provider discovery -------------------------------------------------

    def list_providers(self) -> list[dict[str, Any]]:
        """
        Return the providers supported by the sidecar, each annotated with
        whether its OAuth app credentials are configured. The sidecar does
        not expose this exactly; we return the static Open Wearables set and
        let the caller layer availability from env inspection.
        """
        if not self.enabled():
            return []
        try:
            with self._client() as c:
                resp = c.get('/api/v1/providers')
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and isinstance(
                    data.get('providers'), list,
                ):
                    return data['providers']
        except Exception as exc:
            logger.info(
                'Open Wearables /api/v1/providers unavailable (%s); '
                'returning static list', exc,
            )
        # Static fallback — what Open Wearables supports today.
        return [
            {'name': n}
            for n in (
                'garmin', 'oura', 'polar', 'suunto',
                'whoop', 'fitbit', 'strava', 'ultrahuman',
            )
        ]

    # -- OAuth kickoff ------------------------------------------------------

    def oauth_kickoff_url(
        self,
        provider: str,
        *,
        ow_user_id: str,
        callback_url: str,
        state: str,
    ) -> str:
        """
        Build the URL the patient should be redirected to in order to start
        an OAuth handshake with the given provider. Open Wearables hosts the
        redirect targets itself.
        """
        from urllib.parse import urlencode

        qs = urlencode({
            'user_id': ow_user_id,
            'redirect_uri': callback_url,
            'state': state,
        })
        return f'{self.base_url}/api/v1/providers/{provider}/connect?{qs}'

    # -- delta fetch --------------------------------------------------------

    def fetch_deltas(
        self,
        *,
        ow_user_id: str,
        provider: str,
        since: datetime | None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """
        Fetch new samples for an Open Wearables user since `since`.

        Returns a list of raw sample dicts. Empty list on no new data.
        Raises on non-recoverable errors so the caller can mark the sync
        status appropriately.
        """
        if not self.enabled():
            return []

        params: dict[str, Any] = {
            'user_id': ow_user_id,
            'provider': provider,
            'limit': limit,
        }
        if since is not None:
            # Open Wearables expects ISO 8601 UTC
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            params['since'] = since.isoformat()

        with self._client() as c:
            resp = c.get('/api/v1/samples', params=params)
            resp.raise_for_status()
            data = resp.json()

        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get('samples'), list):
            return data['samples']
        return []
