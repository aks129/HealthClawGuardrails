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

The proxy rewrites upstream URLs in responses to point back to this server,
so clients never see or interact with the upstream directly.
"""

import logging
import os
from urllib.parse import urljoin, urlparse, urlencode

import httpx

logger = logging.getLogger(__name__)

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
        logger.info(f'FHIR upstream proxy initialized: {self.upstream_url}')

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


# --- Module-level singleton ---

_proxy_instance: FHIRUpstreamProxy | None = None


def get_proxy() -> FHIRUpstreamProxy | None:
    """Return the proxy singleton, or None if upstream is not configured."""
    global _proxy_instance
    if _proxy_instance is not None:
        return _proxy_instance

    upstream_url = os.environ.get('FHIR_UPSTREAM_URL', '').strip()
    if not upstream_url:
        return None

    local_base = os.environ.get('FHIR_LOCAL_BASE_URL', '').strip()
    _proxy_instance = FHIRUpstreamProxy(upstream_url, local_base)
    return _proxy_instance


def reset_proxy():
    """Reset the proxy singleton (for testing)."""
    global _proxy_instance
    if _proxy_instance:
        _proxy_instance.close()
    _proxy_instance = None


def is_proxy_enabled() -> bool:
    """Check if upstream proxy mode is configured."""
    return bool(os.environ.get('FHIR_UPSTREAM_URL', '').strip())
