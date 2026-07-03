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

import ipaddress
import logging
import os
import socket
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


# ---------------------------------------------------------------------------
# SHARP-on-MCP support (Standardised Healthcare Agent Remote Protocol)
# ---------------------------------------------------------------------------
#
# Per the SHARP spec the MCP server never runs an OAuth dance itself: the
# agent host obtains a SMART-on-FHIR access token and forwards it on every
# call via HTTP headers, alongside the target FHIR base URL.  This lets a
# single HealthClaw deployment guard any SMART-launched FHIR endpoint
# (Epic, Cerner, MEDITECH, HAPI, SMART Health IT, ...) without re-config.
#
# Headers (consumed before the request reaches Flask route handlers):
#   X-FHIR-Server-URL  — upstream FHIR base, e.g. https://hapi.fhir.org/baseR4
#   X-FHIR-Access-Token — bearer token (raw or with "Bearer " prefix)
#   X-Patient-ID       — optional patient banner / launch context
#
# When these headers are present we build a transient proxy for that one
# request, applying the full guardrail stack (redaction, audit, disclaimers,
# URL rewriting) on top of the SHARP-supplied upstream.  When absent, the
# server falls back to the singleton proxy configured via env vars, or to
# pure local mode.

SHARP_SERVER_URL_HEADER = 'X-FHIR-Server-URL'
SHARP_ACCESS_TOKEN_HEADER = 'X-FHIR-Access-Token'
SHARP_PATIENT_ID_HEADER = 'X-Patient-ID'


def _is_blocked_ip(ip_str: str) -> bool:
    """True if `ip_str` is an internal/reserved address an upstream must not use."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # not a parseable IP → block (fail closed)
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def validate_upstream_url(url: str) -> bool:
    """SSRF guard for a client-supplied upstream FHIR base URL.

    Requires https; blocks private / loopback / link-local / reserved hosts
    (including cloud metadata 169.254.169.254); honours an optional
    FHIR_UPSTREAM_ALLOWED_HOSTS allowlist. Hostnames are resolved and EVERY
    resolved IP is checked (anti-DNS-rebinding). Returns False on any doubt.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:  # noqa: BLE001 — malformed URL → reject
        return False
    if parsed.scheme != 'https' or not parsed.hostname:
        return False
    host = parsed.hostname

    allow = os.environ.get('FHIR_UPSTREAM_ALLOWED_HOSTS', '').strip()
    if allow:
        allowed = {h.strip().lower() for h in allow.split(',') if h.strip()}
        if host.lower() not in allowed:
            return False

    # Literal IP → check directly (no DNS).
    try:
        ipaddress.ip_address(host)
        return not _is_blocked_ip(host)
    except ValueError:
        pass  # it's a hostname

    # Hostname → resolve and reject if ANY resolved address is internal.
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443,
                                   proto=socket.IPPROTO_TCP)
    except (socket.gaierror, socket.error, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        if _is_blocked_ip(info[4][0]):
            return False
    return True


def _sharp_url_from_request():
    """Return (raw_url, is_valid) for the SHARP upstream header, cached on g.

    Caching avoids re-resolving DNS multiple times per request (is_sharp_context_active
    and get_proxy_for_request both consult it).
    """
    try:
        from flask import g, request, has_request_context
    except ImportError:  # pragma: no cover
        return '', False
    if not has_request_context():
        return '', False
    cached = getattr(g, '_sharp_url_check', None)
    if cached is not None:
        return cached
    raw = (request.headers.get(SHARP_SERVER_URL_HEADER) or '').strip()
    result = (raw, bool(raw) and validate_upstream_url(raw))
    try:
        g._sharp_url_check = result
    except Exception:  # pragma: no cover — g not available
        pass
    return result


def make_sharp_proxy(server_url: str,
                     access_token: str | None,
                     local_base_url: str = '') -> FHIRUpstreamProxy:
    """Create a per-request FHIR proxy from SHARP context headers."""
    proxy = FHIRUpstreamProxy(server_url, local_base_url)
    if access_token:
        token = access_token.strip()
        if token.lower().startswith('bearer '):
            token = token[7:].strip()
        proxy._client.headers['Authorization'] = f'Bearer {token}'
    return proxy


def get_proxy_for_request() -> FHIRUpstreamProxy | None:
    """
    Return the proxy for the current request.

    Priority:
      1. SHARP headers (X-FHIR-Server-URL) — build a transient per-request proxy
      2. Singleton env-var proxy (FHIR_UPSTREAM_URL / MEDPLUM_BASE_URL)
      3. None (local mode)

    The transient proxy is cached on flask.g and closed in the teardown handler.
    """
    try:
        from flask import g, request, has_request_context
    except ImportError:  # pragma: no cover - Flask is a hard dep, defensive only
        return get_proxy()

    if not has_request_context():
        return get_proxy()

    cached = getattr(g, '_sharp_proxy', None)
    if cached is not None:
        return cached

    server_url, valid = _sharp_url_from_request()
    if server_url and not valid:
        # SSRF guard: a client-supplied upstream that fails validation
        # (non-https, private/loopback/link-local/reserved, or off-allowlist)
        # is ignored — no per-request proxy is built.
        logger.warning('Rejected SHARP upstream URL (failed SSRF validation)')
        return get_proxy()
    if server_url:
        access_token = (request.headers.get(SHARP_ACCESS_TOKEN_HEADER) or '').strip() or None
        local_base = os.environ.get('FHIR_LOCAL_BASE_URL', '').strip()
        proxy = make_sharp_proxy(server_url, access_token, local_base)
        g._sharp_proxy = proxy
        return proxy

    return get_proxy()


def is_sharp_context_active() -> bool:
    """True when the request carries a SHARP upstream header that PASSES the
    SSRF guard. An invalid/malicious upstream must not activate SHARP context
    (which would otherwise skip read-auth and synthesize a tenant)."""
    _raw, valid = _sharp_url_from_request()
    return valid


def close_request_proxy(_exc=None):
    """Flask teardown handler — close any SHARP per-request proxy on flask.g."""
    try:
        from flask import g
    except ImportError:  # pragma: no cover
        return
    proxy = getattr(g, '_sharp_proxy', None)
    if proxy is not None:
        try:
            proxy.close()
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            delattr(g, '_sharp_proxy')
        except AttributeError:
            pass
