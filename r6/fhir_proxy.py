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
from urllib.parse import urlparse, urlsplit, urlunsplit

from r6.upstream_connectors import (
    AUTH_OAUTH2,
    resolve_upstream_config,
)

import httpx

from r6.version import __version__

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Medplum token cache (Redis-backed with in-process fallback)
# ---------------------------------------------------------------------------

_MEDPLUM_HOSTED_TOKEN_ENDPOINT = 'https://api.medplum.com/oauth2/token'


def medplum_token_endpoint(base_url: str = '') -> str:
    """Where to ask THIS Medplum for a token.

    Derived from the server we are actually talking to, because the constant
    it replaced was `https://api.medplum.com/oauth2/token` with no override.
    Medplum's whole proposition is that you can self-host it, so pointing
    MEDPLUM_BASE_URL at your own instance was the expected case — and it sent
    your self-hosted client credentials to Medplum's hosted service, which
    does not know them. The connector worked against exactly one deployment
    of a product designed to be deployed anywhere.

    MEDPLUM_BASE_URL is the FHIR base (`https://host/fhir/R4`); the token
    endpoint hangs off the server root, so origin + /oauth2/token. An explicit
    MEDPLUM_TOKEN_URL wins over both, for deployments that put the
    authorization server somewhere else entirely.
    """
    explicit = os.environ.get('MEDPLUM_TOKEN_URL', '').strip()
    if explicit:
        return explicit
    base = (base_url or os.environ.get('MEDPLUM_BASE_URL', '')).strip()
    if base:
        parts = urlsplit(base)
        if parts.scheme and parts.netloc:
            return urlunsplit((parts.scheme, parts.netloc, '/oauth2/token',
                               '', ''))
    return _MEDPLUM_HOSTED_TOKEN_ENDPOINT

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
        logger.debug(
            'Redis unavailable; using in-process token cache (%s)',
            type(exc).__name__,
        )
        return None


def _fetch_medplum_token(client_id: str, client_secret: str,
                         token_endpoint: str = '') -> str:
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
            logger.debug('Redis token-cache read failed (%s)', type(exc).__name__)

    # 2. Try in-process cache
    if _medplum_cache['token'] and time.time() < _medplum_cache['expires_at']:
        return _medplum_cache['token']

    # 3. Fetch fresh token
    resp = httpx.post(
        token_endpoint or medplum_token_endpoint(),
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
            logger.debug('Redis token-cache write failed (%s)', type(exc).__name__)

    # 5. Store in-process fallback
    _medplum_cache['token'] = token
    _medplum_cache['expires_at'] = time.time() + ttl

    logger.info('Medplum access token refreshed (ttl=%ds)', ttl)
    return token

# Timeout for upstream requests (seconds)
_UPSTREAM_TIMEOUT = float(os.environ.get('FHIR_UPSTREAM_TIMEOUT', '15'))

# ---------------------------------------------------------------------------
# Upstream error sanitization
#
# A failed upstream call must surface as a (sanitized) OperationOutcome with
# the real status — never as an empty bundle or a fake not-found. Raw upstream
# error bodies are NOT passed through: diagnostics can embed PHI, stack traces,
# or internal URLs that _rewrite_urls() would not catch.
# ---------------------------------------------------------------------------

# Statuses that describe the CALLER's request; these pass through unchanged.
# Upstream 401/403 are deliberately absent: the caller never authenticates to
# the upstream directly, so those mean the PROXY's credentials or upstream
# policy failed — surfaced as 502 so the caller doesn't re-auth in a loop.
_PASSTHROUGH_STATUSES = frozenset({400, 404, 405, 406, 409, 410, 412, 422, 429})

# FHIR issue-type codes allowed through from upstream OperationOutcomes.
_SAFE_ISSUE_CODES = frozenset({
    'invalid', 'structure', 'required', 'value', 'invariant',
    'security', 'login', 'unknown', 'expired', 'forbidden', 'suppressed',
    'processing', 'not-supported', 'duplicate', 'multiple-matches',
    'not-found', 'deleted', 'too-long', 'code-invalid', 'extension',
    'too-costly', 'business-rule', 'conflict', 'transient', 'lock-error',
    'no-store', 'exception', 'timeout', 'incomplete', 'throttled',
    'informational',
})
_SAFE_SEVERITIES = frozenset({'fatal', 'error', 'warning', 'information'})
_MAX_SANITIZED_ISSUES = 5
# Refuse to parse oversized upstream error bodies (memory-exhaustion guard).
_MAX_ERROR_BODY_BYTES = 1_000_000

# Upstream free text (issue[].details.text / diagnostics) is NEVER forwarded:
# it can carry patient names, identifiers, or internal hostnames that no URL
# regex can reliably strip. We keep only the issue `code` (a bounded FHIR
# value-set token) and synthesize the human-readable message ourselves from
# this map. Specific, parameter-level correction ("unknown param X, did you
# mean Y") is produced on the LOCAL search path, where we parse the query and
# can name the parameter without echoing upstream text.
_ISSUE_CODE_MESSAGE = {
    'invalid': 'The upstream FHIR server rejected the request as invalid.',
    'structure': 'The upstream FHIR server rejected the request structure.',
    'required': 'The upstream FHIR server reported a missing required element.',
    'value': 'The upstream FHIR server rejected a submitted value.',
    'not-supported': 'The upstream FHIR server does not support this request.',
    'security': 'Upstream authentication or authorization failed.',
    'forbidden': 'The upstream FHIR server forbade this request.',
    'not-found': 'The upstream FHIR server reported the resource was not found.',
    'conflict': 'The request conflicted with the current state upstream.',
    'duplicate': 'The upstream FHIR server reported a duplicate.',
    'too-costly': 'The upstream FHIR server refused the request as too costly.',
    'throttled': 'The upstream FHIR server is rate-limiting requests.',
    'processing': 'The upstream FHIR server could not process the request.',
    'transient': 'The upstream FHIR server had a transient error.',
    'timeout': 'The upstream FHIR server timed out.',
    'exception': 'The upstream FHIR server encountered an internal error.',
}


def _message_for_code(code: str) -> str:
    return _ISSUE_CODE_MESSAGE.get(code, 'The upstream FHIR server returned an error.')


def _issue_code_for_status(status: int) -> str:
    """Map an HTTP status to a FHIR issue-type code."""
    mapping = {
        400: 'invalid', 401: 'security', 403: 'security',
        404: 'not-found', 405: 'not-supported', 409: 'conflict',
        410: 'deleted', 412: 'conflict', 422: 'processing',
        429: 'throttled',
    }
    if status in mapping:
        return mapping[status]
    return 'transient' if status >= 500 else 'processing'


def _sanitize_issue_list(raw_issues, fallback_code: str) -> list:
    """Allowlist-sanitize an OperationOutcome issue array.

    Keeps only ``severity`` and ``code`` from upstream (both bounded FHIR
    value-set tokens) and synthesizes ``details.text`` from the code — the
    upstream's own free text is never forwarded. Tolerates malformed shapes
    (null, dict, string, non-dict entries, unhashable fields) by dropping or
    defaulting them; a hostile or broken upstream must not crash the error
    path or leak text through it.
    """
    issues = []
    if not isinstance(raw_issues, list):
        return issues
    for issue in raw_issues[:_MAX_SANITIZED_ISSUES]:
        if not isinstance(issue, dict):
            continue
        severity = issue.get('severity')
        code = issue.get('code')
        # Membership tests must be type-guarded: a list/dict severity or code
        # is unhashable and would raise inside `in`.
        safe_severity = severity if isinstance(severity, str) and severity in _SAFE_SEVERITIES else 'error'
        safe_code = code if isinstance(code, str) and code in _SAFE_ISSUE_CODES else fallback_code
        issues.append({
            'severity': safe_severity,
            'code': safe_code,
            'details': {'text': _message_for_code(safe_code)},
        })
    return issues


def sanitize_operation_outcome_resource(oo) -> dict:
    """Sanitize an OperationOutcome embedded in a SUCCESS response — e.g. a
    search.mode="outcome" warning entry in a searchset. apply_redaction()
    targets clinical resources and does not inspect issue[].diagnostics, so
    outcome entries go through the same allowlist as upstream errors."""
    raw = oo.get('issue') if isinstance(oo, dict) else None
    issues = _sanitize_issue_list(raw, 'processing')
    if not issues:
        issues = [{'severity': 'information', 'code': 'informational',
                   'details': {'text': 'The upstream FHIR server returned a warning.'}}]
    return {'resourceType': 'OperationOutcome', 'issue': issues}


def sanitize_upstream_error(resp, caller_auth: bool = False) -> tuple[dict, int]:
    """Convert a non-2xx upstream response into (OperationOutcome, status).

    Allowlist policy: only issue ``severity`` and ``code`` (bounded FHIR
    value-set tokens) survive; ``details.text`` is synthesized from the code.
    Upstream free text, diagnostics, expressions, extensions, and any
    non-OperationOutcome body are dropped entirely — upstream error text can
    carry patient names or internal hostnames that scrubbing can't reliably
    remove.

    Status mapping: caller-attributable 4xx pass through. 401/403 depend on
    who owns the upstream credential: with ``caller_auth=True`` (SHARP mode —
    the caller's own SMART token is forwarded) they pass through so the
    caller can re-authenticate; otherwise they map to 502 because the
    PROXY's credentials failed and a passthrough 401 would send the caller
    into a futile re-auth loop. 5xx maps to 502 with a fixed message —
    upstream server-error text is never forwarded.
    """
    upstream_status = resp.status_code
    if upstream_status in _PASSTHROUGH_STATUSES:
        status = upstream_status
    elif caller_auth and upstream_status in (401, 403):
        status = upstream_status
    else:
        status = 502

    if upstream_status in (401, 403) and not caller_auth:
        # The upstream's auth diagnostics describe the proxy's credentials,
        # which the caller can neither see nor fix — replace them wholesale.
        return {
            'resourceType': 'OperationOutcome',
            'issue': [{
                'severity': 'error',
                'code': 'security',
                'details': {'text': ('Upstream authentication/authorization '
                                     f'failed (HTTP {upstream_status} from upstream)')},
            }],
        }, status

    issues = []
    if upstream_status < 500:
        # 5xx bodies are never parsed: server-error pages/traces have no
        # corrective value for the caller and the highest leak risk.
        raw = getattr(resp, 'content', None)
        try:
            oversized = raw is not None and len(raw) > _MAX_ERROR_BODY_BYTES
        except TypeError:  # content has no length — treat as unbounded, don't parse
            oversized = True
        body = None
        if not oversized:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001 — non-JSON upstream error body
                body = None
        if isinstance(body, dict) and body.get('resourceType') == 'OperationOutcome':
            issues = _sanitize_issue_list(body.get('issue'),
                                          _issue_code_for_status(upstream_status))

    if not issues:
        fallback_code = _issue_code_for_status(upstream_status)
        issues = [{
            'severity': 'error',
            'code': fallback_code,
            'details': {'text': _message_for_code(fallback_code)},
        }]

    return {'resourceType': 'OperationOutcome', 'issue': issues}, status


def malformed_upstream_response_outcome() -> tuple[dict, int]:
    """(OperationOutcome, 502) for a 2xx upstream response whose body is
    absent, unparseable, or not a JSON object — a malformed success must not
    escape as an unhandled 500 downstream (routes assume a dict resource)."""
    return {
        'resourceType': 'OperationOutcome',
        'issue': [{
            'severity': 'error',
            'code': 'processing',
            'details': {'text': 'Upstream FHIR server returned a malformed response body'},
        }],
    }, 502


def upstream_unreachable_outcome(exc: Exception) -> tuple[dict, int]:
    """(OperationOutcome, 502) for a network-level upstream failure.

    Only the exception TYPE is disclosed — str(exc) can embed URLs or
    secret-bearing connection strings.
    """
    return {
        'resourceType': 'OperationOutcome',
        'issue': [{
            'severity': 'error',
            'code': 'transient',
            'details': {'text': f'Upstream FHIR server unreachable ({type(exc).__name__})'},
        }],
    }, 502


class FHIRUpstreamProxy:
    """
    HTTP client that proxies FHIR requests to an upstream server.

    All responses are returned as Python dicts (parsed JSON).
    URL rewriting ensures no upstream URLs leak to the client.
    """

    def __init__(self, upstream_url: str, local_base_url: str = '',
                 caller_auth: bool = False,
                 basic_auth: tuple[str, str] | None = None,
                 kind: str = ''):
        # The connector kind this proxy was BUILT as, reported by healthy()
        # beside the `software` the server names itself. When those two
        # disagree the deployment is pointed somewhere nobody meant, and that
        # is otherwise invisible until a request fails naming neither.
        self.kind = kind
        self.upstream_url = upstream_url.rstrip('/')
        self.local_base_url = local_base_url.rstrip('/')
        # True when the CALLER's own credential is forwarded upstream (SHARP
        # mode) — upstream 401/403 then belong to the caller and pass
        # through, instead of mapping to 502 (proxy-credential failure).
        self.caller_auth = caller_auth
        # HTTP Basic to the upstream, when the upstream requires a credential
        # of its own (Aidbox Client, HAPI behind basic auth, ...). This is the
        # PROXY's credential, not the caller's, which is why caller_auth stays
        # False: a 401 from upstream means OUR credential is wrong, and
        # handing that to the caller as "re-authenticate" would send them
        # round a loop they cannot exit. Never logged — see _redacted_auth.
        self.basic_auth = basic_auth
        self._client = httpx.Client(
            base_url=self.upstream_url,
            timeout=_UPSTREAM_TIMEOUT,
            # SSRF: do NOT follow redirects. validate_upstream_url() only vets the
            # initial URL; following a 3xx would let a validated public host
            # redirect the server to cloud metadata / internal IPs.
            follow_redirects=False,
            auth=basic_auth,
            headers={
                'Accept': 'application/fhir+json, application/json',
                'User-Agent': f'HealthClaw-Guardrails/{__version__}',
            },
        )
        self._upstream_host = urlparse(upstream_url).netloc
        logger.info('FHIR upstream proxy initialized')

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
                    **({'kind': self.kind} if self.kind else {}),
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

    def _success_body(self, resp):
        """Parse a 2xx upstream body and require it to be a JSON object.

        Returns (rewritten_dict, None) on success, or (None, malformed
        outcome tuple) when the body is unparseable or not an object —
        downstream route code assumes a dict resource/Bundle, so an array
        or scalar 200 would otherwise crash after the guardrail boundary.
        """
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 — unparseable body
            return None, malformed_upstream_response_outcome()
        if not isinstance(data, dict):
            return None, malformed_upstream_response_outcome()
        return self._rewrite_urls(data), None

    def read(self, resource_type: str, resource_id: str) -> tuple[dict | None, int]:
        """Read a single resource from the upstream server.

        Returns (resource, 200) on success, (None, 404) when the upstream
        says the resource does not exist, and (sanitized OperationOutcome,
        status) for every other failure — an upstream 401/500 must not
        masquerade as "not found" (#74).
        """
        path = f'/{resource_type}/{resource_id}'
        try:
            resp = self._client.get(path)
        except Exception as e:  # noqa: BLE001 — network-level failure
            # Log the resource type only — the path embeds the resource id,
            # which for person resources is PHI-adjacent.
            logger.error(f'Upstream read {resource_type} failed: {type(e).__name__}')
            return upstream_unreachable_outcome(e)
        if resp.status_code == 200:
            data, malformed = self._success_body(resp)
            if malformed is not None:
                logger.warning(f'Upstream read {resource_type} returned a malformed 200 body')
                return malformed
            return data, 200
        if resp.status_code == 404:
            return None, 404
        logger.warning(f'Upstream read {resource_type} returned {resp.status_code}')
        return sanitize_upstream_error(resp, caller_auth=self.caller_auth)

    def search(self, resource_type: str, params: dict) -> tuple[dict, int]:
        """Search resources on the upstream server.

        Returns (Bundle, 200) on success and (sanitized OperationOutcome,
        status) on failure — a rejected search must not be reported as an
        empty result set (#74).
        """
        path = f'/{resource_type}'
        try:
            resp = self._client.get(path, params=params)
        except Exception as e:  # noqa: BLE001 — network-level failure
            logger.error(f'Upstream search {resource_type} failed: {type(e).__name__}')
            return upstream_unreachable_outcome(e)
        if resp.status_code == 200:
            data, malformed = self._success_body(resp)
            if malformed is not None or data.get('resourceType') != 'Bundle':
                # A search MUST return a Bundle; a bare resource (or other
                # shape) would make the route synthesize a misleading empty
                # searchset. Treat anything else as a malformed response.
                logger.warning(f'Upstream search {resource_type} returned a non-Bundle 200 body')
                return malformed if malformed is not None else malformed_upstream_response_outcome()
            return data, 200
        logger.warning(f'Upstream search {resource_type} returned {resp.status_code}')
        return sanitize_upstream_error(resp, caller_auth=self.caller_auth)

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
                data, malformed = self._success_body(resp)
                if malformed is not None:
                    return malformed
                return data, resp.status_code
            logger.warning(f'Upstream create {resource_type} returned {resp.status_code}')
            return sanitize_upstream_error(resp, caller_auth=self.caller_auth)
        except Exception as e:
            logger.error(f'Upstream create {resource_type} failed: {type(e).__name__}')
            return upstream_unreachable_outcome(e)

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
                data, malformed = self._success_body(resp)
                if malformed is not None:
                    return malformed
                return data, resp.status_code
            logger.warning(f'Upstream update {resource_type} returned {resp.status_code}')
            return sanitize_upstream_error(resp, caller_auth=self.caller_auth)
        except Exception as e:
            logger.error(f'Upstream update {resource_type} failed: {type(e).__name__}')
            return upstream_unreachable_outcome(e)

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
        except Exception as exc:
            logger.error('Upstream operation failed (%s)', type(exc).__name__)
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


class OAuth2UpstreamProxy(FHIRUpstreamProxy):
    """
    FHIRUpstreamProxy variant for upstreams using OAuth2 client-credentials.

    Injects a Bearer token into every outgoing request. Tokens are cached in
    Redis when available; an in-process dict provides fallback caching so the
    server stays functional without Redis.

    Named for the GRANT rather than for Medplum, because the grant is what
    this implements and Medplum is one server that uses it. `MedplumProxy`
    remains as an alias below: it is imported by name in several tests and in
    scripts, and renaming a class is not worth breaking a caller over.
    """

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        local_base_url: str = '',
        token_endpoint: str = '',
        kind: str = '',
    ):
        super().__init__(base_url, local_base_url, kind=kind)
        self._client_id = client_id
        self._client_secret = client_secret
        # Resolved once, from the server this proxy actually points at — not
        # read from the environment at request time, which would let a proxy
        # built for one server start authenticating against another after an
        # unrelated env change.
        self._token_endpoint = token_endpoint or medplum_token_endpoint(base_url)
        # Inject auth into every request via httpx event hook
        self._client.event_hooks['request'] = [self._inject_bearer]
        logger.info('OAuth2 upstream proxy initialized (token endpoint %s)',
                    self._token_endpoint)

    def _inject_bearer(self, request: httpx.Request) -> None:
        """httpx event hook — adds Authorization header before each send.

        RAISES when no token can be obtained. It used to log and return, which
        let the request go out with NO Authorization header at all: a token
        failure was silently downgraded to an anonymous request against the
        record system. On a server that refuses anonymous callers that
        surfaces as a confusing 502; on one that allows anonymous reads of
        anything, it is a request we did not intend to make. Neither is a
        thing to do quietly, and the difference between them is a setting on
        somebody else's server.

        httpx propagates this out of .send(), so the caller sees a failed
        request rather than a successful unauthenticated one.
        """
        token = _fetch_medplum_token(self._client_id, self._client_secret,
                                     self._token_endpoint)
        request.headers['Authorization'] = f'Bearer {token}'


#: Back-compat alias. Imported by name in tests and scripts.
MedplumProxy = OAuth2UpstreamProxy


# --- Module-level singleton ---

_proxy_instance: FHIRUpstreamProxy | None = None


def _upstream_basic_auth() -> tuple[str, str] | None:
    """The proxy's own HTTP Basic credential for the upstream, or None.

    Aidbox, and most FHIR servers that are not public sandboxes, refuse an
    anonymous request. Before this existed, `FHIR_UPSTREAM_URL` sent no
    credential at all, so "a proxy in front of any FHIR server" was true only
    of servers that needed no credential — the sandboxes it was tested
    against. Every secured upstream answered 401, which the sanitizer then
    correctly reported as a 502.

    HALF-CONFIGURED IS AN ERROR, AND IT SAYS SO. A typo in one of the two
    variable names would otherwise fall back to anonymous, and anonymous
    against a secured upstream is a wall of 502s whose cause is nowhere in
    the logs. Configuration that is half-present is more likely a mistake
    than an intention, so it is named at ERROR and the credential is not
    attached — the request still fails, but the reason is on the first line
    of the log instead of being inferred from a status code.

    The secret is never logged, here or anywhere else in this module.
    """
    client_id = os.environ.get('FHIR_UPSTREAM_CLIENT_ID', '').strip()
    client_secret = os.environ.get('FHIR_UPSTREAM_CLIENT_SECRET', '').strip()
    if client_id and client_secret:
        logger.info('FHIR upstream proxy: authenticating as client %r',
                    client_id)
        return (client_id, client_secret)
    if client_id or client_secret:
        missing = ('FHIR_UPSTREAM_CLIENT_SECRET' if client_id
                   else 'FHIR_UPSTREAM_CLIENT_ID')
        logger.error(
            'FHIR upstream proxy: %s is set but %s is not — connecting '
            'ANONYMOUSLY. A secured upstream will refuse every request.',
            'FHIR_UPSTREAM_CLIENT_ID' if client_id
            else 'FHIR_UPSTREAM_CLIENT_SECRET',
            missing)
    return None


def get_proxy() -> FHIRUpstreamProxy | None:
    """
    Return the proxy singleton, or None if no upstream is configured.

    WHICH upstream, and how it authenticates, is answered by
    r6.upstream_connectors rather than by branches here — see that module for
    the registry and for why the SHARP per-request path is deliberately not
    part of it. This function's remaining job is to turn a resolved config
    into a client and hold the singleton.

    Precedence is unchanged: FHIR_UPSTREAM_URL wins over MEDPLUM_BASE_URL.
    """
    global _proxy_instance
    if _proxy_instance is not None:
        return _proxy_instance

    config = resolve_upstream_config()
    if config is None:
        return None

    local_base = os.environ.get('FHIR_LOCAL_BASE_URL', '').strip()

    if config.auth == AUTH_OAUTH2:
        # Refusing beats proceeding: a client-credentials upstream with no
        # credentials can only make anonymous requests, and an anonymous
        # request at a record system is not a degraded mode worth having.
        if not config.client_id or not config.client_secret:
            logger.warning(
                'Upstream kind %r needs client credentials and none are set '
                '(FHIR_UPSTREAM_CLIENT_ID/_SECRET) — upstream proxy disabled',
                config.kind)
            return None
        _proxy_instance = OAuth2UpstreamProxy(
            config.base_url, config.client_id, config.client_secret,
            local_base, token_endpoint=config.token_endpoint,
            kind=config.kind)
        return _proxy_instance

    _proxy_instance = FHIRUpstreamProxy(
        config.base_url, local_base, basic_auth=config.basic_auth,
        kind=config.kind)
    return _proxy_instance


def reset_proxy():
    """Reset the proxy singleton (for testing)."""
    global _proxy_instance
    if _proxy_instance:
        _proxy_instance.close()
    _proxy_instance = None
    # Also clear in-process token cache so tests get a clean slate
    _medplum_cache['token'] = None
    _medplum_cache['expires_at'] = 0.0


def upstream_intended() -> bool:
    """True when the environment NAMES an upstream, working or not.

    This is the old `is_proxy_enabled` — an env-var presence check — under a
    name that says what it actually measures. It answers "did the operator
    mean to proxy", which is only useful next to the answer to "are we
    proxying". Never use it alone to describe the running system.
    """
    return bool(
        os.environ.get('FHIR_UPSTREAM_URL', '').strip()
        or os.environ.get('MEDPLUM_BASE_URL', '').strip()
    )


def is_proxy_enabled() -> bool:
    """True when a proxy EXISTS, not when one was asked for.

    These were the same question until an upstream could refuse to be built.
    Since #503 an OAuth2 upstream with no credentials returns None from
    get_proxy() rather than making anonymous requests at a record system —
    correct, and it split the question in two while this function kept
    answering the old one.

    The consequence, measured: with FHIR_UPSTREAM_URL set and the secret
    missing, /r6/fhir/health reported mode 'upstream', status 'healthy',
    HTTP 200 — and every write landed in the container's own SQLite. An
    operator reading that page has no way to see it, and the data is in the
    wrong store by the time anyone looks.

    `upstream_intended()` is the other half. Health reports both, because
    "local because nobody configured an upstream" and "local because the
    upstream we configured could not be built" are different situations and
    only one of them is fine.
    """
    return get_proxy() is not None


def upstream_status() -> dict:
    """What /health should say about the upstream, and whether it is degraded.

    Three states, and the middle one is the reason this function exists:

      not_configured   nobody asked for an upstream. Local mode by design.
      misconfigured    one was NAMED and no client could be built for it.
                       Writes are landing in local storage while the operator
                       believes they are going to their FHIR server.
      <health payload> a proxy exists; the proxy reports on itself.

    'misconfigured' is degraded rather than a quiet 200, which is consistent
    with an upstream that is configured and unreachable — that already
    answers 503. The failure it makes loud is the one where a deploy succeeds
    and the data goes somewhere nobody looks.

    Returns a dict rather than a pair: a 2-tuple is always truthy, and this
    codebase has a documented bypass built on exactly that shape.
    """
    try:
        proxy = get_proxy()
    except ValueError as exc:
        # An unknown connector kind. Startup validation refuses to boot on
        # this, so reaching here means something bypassed it — and /health
        # answering a 500 HTML page is the worst way to say so. Report it.
        logger.error('upstream misconfigured: %s', exc)
        return {'check': 'misconfigured', 'degraded': True}
    if proxy:
        payload = proxy.healthy()
        return {'check': payload,
                'degraded': payload.get('status') != 'connected'}
    if upstream_intended():
        return {'check': 'misconfigured', 'degraded': True}
    return {'check': 'not_configured', 'degraded': False}


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
    resolved IP is checked. Returns False on any doubt.

    Residual: the connection is not pinned to the validated IP, so a low-TTL
    DNS-rebind between validation and connect is still theoretically possible —
    use FHIR_UPSTREAM_ALLOWED_HOSTS in production to eliminate it. Redirect-based
    SSRF is closed separately (the upstream client uses follow_redirects=False).
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
    """Create a per-request FHIR proxy from SHARP context headers.

    caller_auth is set only when a SMART token is actually forwarded: then
    upstream 401/403 belong to the caller and pass through for re-auth. With
    no token forwarded, a 401/403 is not the caller's to fix, so it maps to
    502 like any other proxy-side failure.
    """
    proxy = FHIRUpstreamProxy(server_url, local_base_url,
                              caller_auth=bool(access_token))
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
