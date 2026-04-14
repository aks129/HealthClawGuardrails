"""
Upstream FHIR Server Proxy.

When FHIR_UPSTREAM_URL is configured, this module proxies requests to a real
FHIR server (HAPI, SMART Health IT, Epic sandbox, etc.) while applying the
full MCP guardrail stack on top:

  Client → MCP Server → Flask (guardrails) → Upstream FHIR Server
                              ↓
                    redaction, audit, step-up,
                    tenant isolation, disclaimers

Supported upstream servers (tested):
  - HAPI FHIR R4: https://hapi.fhir.org/baseR4
  - SMART Health IT: https://r4.smarthealthit.org
  - Local HAPI: http://localhost:8080/fhir
  - Medplum: https://api.medplum.com/fhir/R4 (OAuth2 client-credentials)

The proxy rewrites upstream URLs in responses to point back to this server,
so clients never see or interact with the upstream directly.

Medplum mode (MEDPLUM_BASE_URL set, FHIR_UPSTREAM_URL not set):
  Access token acquired via OAuth2 client-credentials grant and cached in
  Redis (key: medplum:access_token, TTL = expires_in - 60s).  Falls back to
  an in-process dict cache when Redis is unavailable.
"""

import logging
import os
import time
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Medplum token cache (Redis-backed with in-process fallback)
# ---------------------------------------------------------------------------

_MEDPLUM_TOKEN_ENDPOINT = 'https://api.medplum.com/oauth2/token'

# In-process fallback cache: {'token': str|None, 'expires_at': float}
_medplum_cache: dict = {'token': None, 'expires_at': 0.0}


def _get_redis():
    """Return a Redis client or None if Redis is unavailable / not configured."""
    redis_url = os.environ.get('REDIS_URL', '').strip()
    if not redis_url:
        return None
    try:
        import redis  # optional dependency
        client = redis.Redis.from_url(redis_url, socket_connect_timeout=1)
        client.ping()
        return client
    except Exception as exc:
        logger.debug('Redis unavailable, using in-process token cache: %s', exc)
        return None


def _fetch_medplum_token(client_id: str, client_secret: str) -> str:
    """
    Obtain a Medplum access token via OAuth2 client-credentials.

    Checks Redis first (key: medplum:access_token), then in-process cache,
    then fetches a fresh token and stores it in both caches.
    """
    # 1. Try Redis
    r = _get_redis()
    if r is not None:
        try:
            cached = r.get('medplum:access_token')
            if cached:
                return cached.decode()
        except Exception as exc:
            logger.debug('Redis get failed: %s', exc)

    # 2. Try in-process cache
    if _medplum_cache['token'] and time.time() < _medplum_cache['expires_at']:
        return _medplum_cache['token']

    # 3. Fetch fresh token
    resp = httpx.post(
        _MEDPLUM_TOKEN_ENDPOINT,
        data={
            'grant_type': 'client_credentials',
            'client_id': client_id,
            'client_secret': client_secret,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload['access_token']
    expires_in = int(payload.get('expires_in', 3600))
    ttl = max(expires_in - 60, 30)  # 60 s safety buffer

    # 4. Store in Redis
    if r is not None:
        try:
            r.setex('medplum:access_token', ttl, token)
        except Exception as exc:
            logger.debug('Redis setex failed: %s', exc)

    # 5. Store in-process fallback
    _medplum_cache['token'] = token
    _medplum_cache['expires_at'] = time.time() + ttl

    logger.info('Medplum access token refreshed (ttl=%ds)', ttl)
    return token

# Timeout for upstream requests (seconds)
_UPSTREAM_TIMEOUT = float(os.environ.get('FHIR_UPSTREAM_TIMEOUT', '15'))


class FHIRUpstreamProxy:
    """
    HTTP client that proxies FHIR requests to an upstream server.

    All responses are returned as Python dicts (parsed JSON).
    URL rewriting ensures no upstream URLs leak to the client.
    """

    def __init__(self, upstream_url: str, local_base_url: str = ''):
        self.upstream_url = upstream_url.rstrip('/')
        self.local_base_url = local_base_url.rstrip('/')
        self._client = httpx.Client(
            base_url=self.upstream_url,
            timeout=_UPSTREAM_TIMEOUT,
            follow_redirects=True,
            headers={
                'Accept': 'application/fhir+json, application/json',
                'User-Agent': 'HealthClaw-Guardrails/1.0.0',
            },
        )
        self._upstream_host = urlparse(upstream_url).netloc
        logger.info('FHIR upstream proxy initialized: %s', self.upstream_url)

    def healthy(self) -> dict:
        """Check upstream server reachability via /metadata."""
        try:
            resp = self._client.get('/metadata', params={'_summary': 'true'})
            if resp.status_code == 200:
                data = resp.json()
                return {
                    'status': 'connected',
                    'upstream_url': self.upstream_url,
                    'fhir_version': data.get('fhirVersion', 'unknown'),
                    'software': data.get('software', {}).get('name', 'unknown'),
                }
            return {
                'status': 'error',
                'upstream_url': self.upstream_url,
                'http_status': resp.status_code,
            }
        except Exception as e:
            return {
                'status': 'unreachable',
                'upstream_url': self.upstream_url,
                'error': str(e),
            }

    def read(self, resource_type: str, resource_id: str) -> dict | None:
        """Read a single resource from the upstream server."""
        path = f'/{resource_type}/{resource_id}'
        try:
            resp = self._client.get(path)
            if resp.status_code == 200:
                data = resp.json()
                return self._rewrite_urls(data)
            if resp.status_code == 404:
                return None
            logger.warning(f'Upstream read {path} returned {resp.status_code}')
            return None
        except Exception as e:
            logger.error(f'Upstream read {path} failed: {e}')
            return None

    def search(self, resource_type: str, params: dict) -> dict:
        """Search resources on the upstream server. Returns a Bundle."""
        path = f'/{resource_type}'
        try:
            resp = self._client.get(path, params=params)
            if resp.status_code == 200:
                data = resp.json()
                return self._rewrite_urls(data)
            logger.warning(f'Upstream search {path} returned {resp.status_code}')
            return self._empty_bundle()
        except Exception as e:
            logger.error(f'Upstream search {path} failed: {e}')
            return self._empty_bundle()

    def create(self, resource_type: str, resource: dict) -> tuple[dict | None, int]:
        """Create a resource on the upstream server. Returns (resource, status_code)."""
        path = f'/{resource_type}'
        try:
            resp = self._client.post(
                path,
                json=resource,
                headers={'Content-Type': 'application/fhir+json'},
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return self._rewrite_urls(data), resp.status_code
            logger.warning(f'Upstream create {path} returned {resp.status_code}: {resp.text[:200]}')
            return resp.json() if resp.headers.get('content-type', '').startswith('application/') else None, resp.status_code
        except Exception as e:
            logger.error(f'Upstream create {path} failed: {e}')
            return None, 502

    def update(self, resource_type: str, resource_id: str, resource: dict,
               if_match: str | None = None) -> tuple[dict | None, int]:
        """Update a resource on the upstream server."""
        path = f'/{resource_type}/{resource_id}'
        headers = {'Content-Type': 'application/fhir+json'}
        if if_match:
            headers['If-Match'] = if_match
        try:
            resp = self._client.put(path, json=resource, headers=headers)
            if resp.status_code in (200, 201):
                data = resp.json()
                return self._rewrite_urls(data), resp.status_code
            logger.warning(f'Upstream update {path} returned {resp.status_code}')
            return resp.json() if resp.headers.get('content-type', '').startswith('application/') else None, resp.status_code
        except Exception as e:
            logger.error(f'Upstream update {path} failed: {e}')
            return None, 502

    def operation(self, path: str, method: str = 'GET',
                  params: dict | None = None,
                  body: dict | None = None) -> tuple[dict | None, int]:
        """Execute a FHIR operation ($stats, $lastn, $validate, etc.)."""
        try:
            if method.upper() == 'GET':
                resp = self._client.get(path, params=params)
            else:
                resp = self._client.post(
                    path,
                    json=body,
                    headers={'Content-Type': 'application/fhir+json'},
                    params=params,
                )
            data = resp.json() if resp.headers.get('content-type', '').startswith('application/') else None
            if data:
                data = self._rewrite_urls(data)
            return data, resp.status_code
        except Exception as e:
            logger.error(f'Upstream operation {path} failed: {e}')
            return None, 502

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    # --- Internal helpers ---

    def _rewrite_urls(self, data):
        """Rewrite upstream URLs in response to point to this proxy."""
        if not self.local_base_url:
            return data
        if isinstance(data, dict):
            return {k: self._rewrite_urls(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._rewrite_urls(item) for item in data]
        if isinstance(data, str) and self.upstream_url in data:
            return data.replace(self.upstream_url, self.local_base_url)
        return data

    @staticmethod
    def _empty_bundle():
        return {
            'resourceType': 'Bundle',
            'type': 'searchset',
            'total': 0,
            'entry': [],
        }


class MedplumProxy(FHIRUpstreamProxy):
    """
    FHIRUpstreamProxy variant for Medplum.

    Injects a Bearer token (obtained via OAuth2 client-credentials) into every
    outgoing request.  Tokens are cached in Redis when available; an in-process
    dict provides fallback caching so the server stays functional without Redis.
    """

    def __init__(
        self,
        medplum_base_url: str,
        client_id: str,
        client_secret: str,
        local_base_url: str = '',
    ):
        super().__init__(medplum_base_url, local_base_url)
        self._client_id = client_id
        self._client_secret = client_secret
        # Inject auth into every request via httpx event hook
        self._client.event_hooks['request'] = [self._inject_bearer]
        logger.info('Medplum proxy initialized: %s', self.upstream_url)

    def _inject_bearer(self, request: httpx.Request) -> None:
        """httpx event hook — adds Authorization header before each send."""
        try:
            token = _fetch_medplum_token(self._client_id, self._client_secret)
            request.headers['Authorization'] = f'Bearer {token}'
        except Exception as exc:
            logger.error('Failed to obtain Medplum token: %s', exc)


# --- Module-level singleton ---

_proxy_instance: FHIRUpstreamProxy | None = None


def get_proxy() -> FHIRUpstreamProxy | None:
    """
    Return the proxy singleton, or None if no upstream is configured.

    Priority:
      1. FHIR_UPSTREAM_URL — generic upstream proxy (no auth)
      2. MEDPLUM_BASE_URL  — Medplum proxy (OAuth2 client-credentials)
    """
    global _proxy_instance
    if _proxy_instance is not None:
        return _proxy_instance

    local_base = os.environ.get('FHIR_LOCAL_BASE_URL', '').strip()

    upstream_url = os.environ.get('FHIR_UPSTREAM_URL', '').strip()
    if upstream_url:
        _proxy_instance = FHIRUpstreamProxy(upstream_url, local_base)
        return _proxy_instance

    medplum_url = os.environ.get('MEDPLUM_BASE_URL', '').strip()
    if medplum_url:
        client_id = os.environ.get('MEDPLUM_CLIENT_ID', '').strip()
        client_secret = os.environ.get('MEDPLUM_CLIENT_SECRET', '').strip()
        if not client_id or not client_secret:
            logger.warning(
                'MEDPLUM_BASE_URL is set but MEDPLUM_CLIENT_ID / '
                'MEDPLUM_CLIENT_SECRET are missing — Medplum proxy disabled'
            )
            return None
        _proxy_instance = MedplumProxy(medplum_url, client_id, client_secret, local_base)
        return _proxy_instance

    return None


def reset_proxy():
    """Reset the proxy singleton (for testing)."""
    global _proxy_instance
    if _proxy_instance:
        _proxy_instance.close()
    _proxy_instance = None
    # Also clear in-process token cache so tests get a clean slate
    _medplum_cache['token'] = None
    _medplum_cache['expires_at'] = 0.0


def is_proxy_enabled() -> bool:
    """Check if any upstream proxy mode is configured."""
    return bool(
        os.environ.get('FHIR_UPSTREAM_URL', '').strip()
        or os.environ.get('MEDPLUM_BASE_URL', '').strip()
    )
