"""
HTTP client for the Open Wearables sidecar.

Reconciled against the-momentum/open-wearables @ 0.6.3 source. The prior client
targeted endpoints that do not exist in Open Wearables (`/api/v1/samples`,
`/api/v1/providers`) and authenticated with `Authorization: Bearer`; the real
API uses an `X-Open-Wearables-API-Key` header and these surfaces:

  GET /api/v1/oauth/providers                      provider settings list
  GET /api/v1/users/{user_id}/timeseries           granular biometrics/activity
  GET /api/v1/users/{user_id}/events/sleep         sleep sessions (incl. naps)
  GET /api/v1/oauth/{provider}/authorize           OAuth kickoff  [see caveat]

NOT verified against a live Open Wearables instance — paths, params, auth
header, and response shapes are transcribed from the 0.6.3 source. Treat as
best-effort until exercised end to end.

CAVEAT — OAuth kickoff auth model: `GET /oauth/{provider}/authorize` is guarded
by *developer-session* auth (`DeveloperDep`) in Open Wearables, not the API key
this client holds, and it requires a pre-created Open Wearables `user_id`. A
downstream API-key consumer (HealthClaw) therefore cannot, as things stand,
initiate the connect handshake itself — that flow appears to be intended for the
Open Wearables developer/dashboard context. `oauth_authorize_url` is implemented
to the documented shape but will 401 without developer auth; resolving this is
an integration decision, tracked separately, not a client bug.

Timeouts default to 15s to match FHIR_UPSTREAM_TIMEOUT; callers can override for
bulk backfills.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Open Wearables SeriesType -> our METRIC_MAP key (see r6/wearables/mapper.py).
# Only aliases where the names differ; unlisted types pass through unchanged and
# fall back to a code.text Observation so no data is lost.
_SERIES_TO_METRIC = {
    'heart_rate_variability_sdnn': 'heart_rate_variability',
    'oxygen_saturation': 'spo2',
    'weight': 'body_weight',
}

# The biometric/activity SeriesType values we map with clinical codes today.
# Passed as explicit `types` so a provider dump doesn't stream every series.
_WANTED_SERIES = [
    'heart_rate', 'resting_heart_rate', 'heart_rate_variability_sdnn',
    'oxygen_saturation', 'respiratory_rate', 'steps', 'body_temperature',
    'weight', 'blood_pressure_systolic', 'blood_pressure_diastolic',
    'blood_glucose',
]

# Providers Open Wearables supports (static fallback when the API is unreachable).
_STATIC_PROVIDERS = (
    'garmin', 'oura', 'polar', 'suunto',
    'whoop', 'fitbit', 'strava', 'ultrahuman',
)


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
            # Open Wearables reads the key from this header (utils/auth.py),
            # NOT an Authorization: Bearer.
            h['X-Open-Wearables-API-Key'] = self.api_key
        return h

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers=self._headers(),
        )

    # -- provider discovery -------------------------------------------------

    def list_providers(self) -> list[dict[str, Any]]:
        """Providers Open Wearables exposes, from GET /api/v1/oauth/providers.

        Returns the raw provider-settings list; falls back to the static
        supported set if the endpoint is unreachable. Callers layer per-app
        credential availability on top (from env inspection).
        """
        if not self.enabled():
            return []
        try:
            with self._client() as c:
                resp = c.get('/api/v1/oauth/providers')
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
                'Open Wearables /api/v1/oauth/providers unavailable (%s); '
                'returning static list', exc,
            )
        return [{'name': n} for n in _STATIC_PROVIDERS]

    # -- OAuth kickoff ------------------------------------------------------

    def oauth_authorize_url(
        self,
        provider: str,
        *,
        ow_user_id: str,
        redirect_uri: str | None = None,
    ) -> str | None:
        """Ask Open Wearables for the URL to redirect the patient to.

        Hits GET /api/v1/oauth/{provider}/authorize?user_id=...&redirect_uri=...
        which returns JSON `{auth_url, state}`. NOTE: that endpoint is guarded
        by developer-session auth upstream, so this call will 401 with only an
        API key — see the module docstring. Returns the auth_url on success,
        None otherwise (caller surfaces a "not configured" state).
        """
        if not self.enabled():
            return None
        params: dict[str, Any] = {'user_id': ow_user_id}
        if redirect_uri:
            params['redirect_uri'] = redirect_uri
        try:
            with self._client() as c:
                resp = c.get(
                    f'/api/v1/oauth/{provider}/authorize', params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning(
                'Open Wearables authorize for %s failed (%s) — likely the '
                'developer-auth requirement; see client docstring', provider, exc,
            )
            return None
        if isinstance(data, dict):
            return data.get('auth_url') or data.get('url')
        return None

    # -- delta fetch --------------------------------------------------------

    def fetch_deltas(
        self,
        *,
        ow_user_id: str,
        provider: str,
        since: datetime | None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Fetch new timeseries samples for an Open Wearables user since `since`.

        Hits GET /api/v1/users/{user_id}/timeseries (required start_time/
        end_time, repeated `types`, capped `limit`) and normalizes each
        `TimeSeriesSample` (`type`/`timestamp`) into the shape the mapper
        expects (`kind`/`recorded_at`), applying the SeriesType alias table.

        Sleep sessions are a *separate* Open Wearables surface
        (`/events/sleep`) and are not returned here — see fetch_sleep_sessions.
        Empty list on no new data. Raises on non-recoverable HTTP errors so the
        caller can mark the sync status.
        """
        if not self.enabled():
            return []

        end = datetime.now(timezone.utc)
        start = since if since is not None else end.replace(
            year=end.year - 1)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)

        # Open Wearables caps limit at 100 per call.
        params = [
            ('start_time', start.isoformat()),
            ('end_time', end.isoformat()),
            ('limit', str(min(limit, 100))),
        ]
        params += [('types', s) for s in _WANTED_SERIES]

        with self._client() as c:
            resp = c.get(
                f'/api/v1/users/{ow_user_id}/timeseries', params=params)
            resp.raise_for_status()
            data = resp.json()

        raw = data if isinstance(data, list) else (
            data.get('data') or data.get('items') or []
            if isinstance(data, dict) else []
        )
        return [self._normalize_sample(s, provider) for s in raw
                if isinstance(s, dict)]

    def fetch_sleep_sessions(
        self,
        *,
        ow_user_id: str,
        since: datetime | None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch sleep sessions (including naps) for an Open Wearables user.

        Hits GET /api/v1/users/{user_id}/events/sleep and returns the raw
        `SleepSession` dicts (`start_time`, `end_time`, `is_nap`, ...) for
        r6.wearables.mapper.sleep_session_to_observation. Sleep is an event
        record, never a timeseries sample.
        """
        if not self.enabled():
            return []
        end = datetime.now(timezone.utc)
        start = since if since is not None else end.replace(year=end.year - 1)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        params = {
            'start_time': start.isoformat(),
            'end_time': end.isoformat(),
            'limit': min(limit, 100),
        }
        with self._client() as c:
            resp = c.get(
                f'/api/v1/users/{ow_user_id}/events/sleep', params=params)
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get('data') or data.get('items') or []
        return []

    @staticmethod
    def _normalize_sample(sample: dict[str, Any], provider: str) -> dict[str, Any]:
        """Map an Open Wearables TimeSeriesSample to the mapper's input shape."""
        series = sample.get('type')
        return {
            'kind': _SERIES_TO_METRIC.get(series, series),
            'value': sample.get('value'),
            'unit': sample.get('unit'),
            'recorded_at': sample.get('timestamp'),
            'provider': provider,
            'sample_id': sample.get('id'),
        }
