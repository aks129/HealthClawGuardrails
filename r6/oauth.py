"""
OAuth 2.1 Authorization Server for FHIR R6 MCP.

Implements:
- Authorization code flow with PKCE (RFC 7636)
- Dynamic client registration (RFC 7591)
- Bearer token validation
- SMART-on-FHIR v2 scopes (patient/*.read, patient/*.write)
- Token revocation (RFC 7009)

This module is designed to work standalone or alongside an external
OAuth provider (Auth0, Keycloak) via OAUTH_ISSUER configuration.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from functools import wraps
from urllib.parse import urlencode, urlsplit
from flask import request, jsonify, redirect
from r6.runtime_config import resolve_app_env
from r6.runtime_config import read_auth_enabled
from r6.constant_time import equal as constant_time_equal

logger = logging.getLogger(__name__)

# Configuration
OAUTH_SECRET = os.environ.get('OAUTH_SECRET', os.environ.get('STEP_UP_SECRET', ''))
TOKEN_TTL_SECONDS = int(os.environ.get('OAUTH_TOKEN_TTL', '3600'))
#: A registered client lives this long; `client_secret_expires_at` says so.
CLIENT_TTL_SECONDS = 30 * 24 * 3600
#: RFC 7591 methods a client may register with. `none` is a public client:
#: no secret is issued and `client_id` is required at the token endpoint.
CLIENT_AUTH_METHODS = ('none', 'client_secret_post', 'client_secret_basic')
#: Loopback hosts whose redirect URIs match on any port (RFC 8252 §7.3).
#: Native clients such as Claude Code listen on an ephemeral port.
_LOOPBACK_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})


def issuer():
    """The issuer identifier (RFC 8414). `OAUTH_ISSUER` when set, else the
    request's own root. Read per call: the tests set the variable after import,
    and a deployment sets it without a code change."""
    configured = os.environ.get('OAUTH_ISSUER', '').strip().rstrip('/')
    return configured or request.host_url.rstrip('/')


def fhir_resource():
    """RFC 8707 identifier of the FHIR surface this issuer fronts. A token
    minted for it carries it as `aud`; `r6.read_auth` accepts nothing else."""
    return f'{issuer()}/r6/fhir'


def resource_policies():
    """Every audience this server will mint for, and how a browser-initiated
    authorize binds its tenant for that audience (spec §3.5.1, P2-b).

    `header`: today's behaviour, the `X-Tenant-Id` header behind the
    public-tenant guard. `demo`: `MCP_OAUTH_DEMO_TENANT`, and the header is
    ignored, because a browser flow has no trusted place to put a tenant.
    The map is explicit; an audience not in it is `invalid_target`, never
    recorded as sent.
    """
    policies = {fhir_resource(): 'header'}
    mcp = os.environ.get('MCP_CANONICAL_RESOURCE', '').strip()
    if mcp:
        policies[mcp] = 'demo'
    return policies


def _loopback_form(uri):
    """The port-stripped form of a plain-http loopback URI, else None."""
    parts = urlsplit(uri)
    if parts.scheme == 'http' and (parts.hostname or '') in _LOOPBACK_HOSTS:
        return parts._replace(netloc=parts.hostname)
    return None


def redirect_uri_allowed(uri):
    """RFC 7591 registration: `https://`, or a plain-http loopback URI.
    Anything else is a redirect we would send a code to over the open
    network, and the registration is refused rather than stored."""
    if not isinstance(uri, str):
        return False
    parts = urlsplit(uri)
    if not parts.netloc or parts.fragment:
        return False
    if parts.scheme == 'https':
        return True
    return _loopback_form(uri) is not None


def redirect_uri_matches(candidate, registered):
    """Exact match, except that loopback URIs match on any port."""
    if candidate == registered:
        return True
    a, b = _loopback_form(candidate), _loopback_form(registered)
    return a is not None and b is not None and a == b


def _redirect_to_client(redirect_uri, state, **params):
    """302 to the client's registered redirect URI (OAuth 2.1 §4.1.2), with
    each parameter URL-encoded and RFC 9207 `iss` appended so the client can
    tell which issuer answered. `state` is echoed only when it was sent."""
    if state:
        params['state'] = state
    params['iss'] = issuer()
    separator = '&' if '?' in redirect_uri else '?'
    response = redirect(f'{redirect_uri}{separator}{urlencode(params)}',
                        code=302)
    response.headers['Cache-Control'] = 'no-store'
    return response


def discovery_document():
    """RFC 8414 metadata. Served under the issuer's root and under the FHIR
    prefix; both copies are this one document."""
    base = issuer()
    return {
        'issuer': base,
        'authorization_endpoint': f'{base}/r6/fhir/oauth/authorize',
        'token_endpoint': f'{base}/r6/fhir/oauth/token',
        'registration_endpoint': f'{base}/r6/fhir/oauth/register',
        'revocation_endpoint': f'{base}/r6/fhir/oauth/revoke',
        'scopes_supported': list(SMART_SCOPES.keys()),
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code'],
        'token_endpoint_auth_methods_supported': list(CLIENT_AUTH_METHODS),
        'code_challenge_methods_supported': ['S256'],
        'authorization_response_iss_parameter_supported': True,
        'introspection_endpoint': f'{base}/r6/fhir/oauth/introspect',
        'introspection_endpoint_auth_methods_supported': [
            'client_secret_basic', 'client_secret_post'],
        'service_documentation': f'{base}/r6/fhir/docs/privacy-policy',
    }


def discovery_root_view():
    """The issuer-root copy of the metadata, registered by `main` at
    `/.well-known/oauth-authorization-server` — the first location a client
    tries for a path-less issuer (RFC 8414 §3)."""
    return jsonify(discovery_document())


def introspection_client_authorized(body):
    """RFC 7662 §2.1: the introspection endpoint is protected (P2-e).

    The one caller is the MCP server, holding the pre-registered confidential
    client `MCP_INTROSPECTION_CLIENT_ID` / `_SECRET`. Unconfigured means
    nobody is authorized — an open introspection endpoint turns any captured
    token into a lookup of the tenant behind it.
    """
    expected_id = os.environ.get('MCP_INTROSPECTION_CLIENT_ID', '').strip()
    expected_secret = os.environ.get('MCP_INTROSPECTION_CLIENT_SECRET', '').strip()
    if not expected_id or not expected_secret:
        return False
    client_id, client_secret = _presented_client_credentials(body)
    if not client_id or not client_secret:
        return False
    return (constant_time_equal(client_id, expected_id)
            and constant_time_equal(client_secret, expected_secret))


def _presented_client_credentials(body):
    """(client_id, client_secret) from the POST body, else HTTP Basic."""
    client_id = body.get('client_id')
    client_secret = body.get('client_secret')
    basic = request.authorization
    if basic is not None and basic.type == 'basic':
        client_id = client_id or basic.username
        client_secret = client_secret or basic.password
    return client_id, client_secret

# SMART-on-FHIR v2 scope definitions
SMART_SCOPES = {
    'fhir.read': 'Read FHIR resources (redacted)',
    'fhir.write': 'Create and update FHIR resources (requires step-up)',
    'context.read': 'Read pre-built context envelopes',
    'context.write': 'Ingest bundles and create context envelopes',
    'audit.read': 'Read audit event records',
    'smart/patient/*.read': 'SMART-on-FHIR patient-level read access',
    'smart/patient/*.write': 'SMART-on-FHIR patient-level write access',
}

# In-memory stores (production: use Redis or database)
_registered_clients = {}  # client_id -> {client_secret, redirect_uris, scopes, name}
_auth_codes = {}  # code -> {client_id, code_challenge, scopes, tenant_id, exp}
_access_tokens = {}  # token -> {client_id, scopes, tenant_id, exp}
_revoked_tokens = set()  # revoked token hashes
_redis_client = None

_OAUTH_STORES = {
    'client': _registered_clients,
    'auth-code': _auth_codes,
    'access-token': _access_tokens,
}


def _is_production():
    return resolve_app_env() == 'production'


def _get_redis_client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    redis_url = os.environ.get('REDIS_URL', '').strip()
    if not redis_url:
        return None
    import redis
    _redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
    return _redis_client


def _oauth_key(kind, key):
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()
    return f'healthclaw:oauth:{kind}:{digest}'


def _oauth_store_set(kind, key, value, ttl=None):
    client = _get_redis_client()
    if client is not None:
        try:
            client.set(
                _oauth_key(kind, key),
                json.dumps(value, separators=(',', ':')),
                ex=ttl,
            )
            return
        except Exception as exc:  # noqa: BLE001 - Redis errors vary
            logger.error('OAuth Redis write failed: %s', type(exc).__name__)
            if _is_production():
                raise RuntimeError('OAuth state store unavailable') from None
    _OAUTH_STORES[kind][key] = value


def _decode_oauth_value(raw):
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    return json.loads(raw)


def _oauth_store_get(kind, key):
    client = _get_redis_client()
    if client is not None:
        try:
            return _decode_oauth_value(client.get(_oauth_key(kind, key)))
        except Exception as exc:  # noqa: BLE001
            logger.error('OAuth Redis read failed: %s', type(exc).__name__)
            if _is_production():
                return None
    return _OAUTH_STORES[kind].get(key)


def _oauth_store_pop(kind, key):
    """Atomically consume a one-time value when backed by Redis."""
    client = _get_redis_client()
    if client is not None:
        try:
            return _decode_oauth_value(client.getdel(_oauth_key(kind, key)))
        except Exception as exc:  # noqa: BLE001
            logger.error('OAuth Redis consume failed: %s', type(exc).__name__)
            if _is_production():
                return None
    return _OAUTH_STORES[kind].pop(key, None)


def _oauth_store_delete(kind, key):
    client = _get_redis_client()
    if client is not None:
        try:
            client.delete(_oauth_key(kind, key))
        except Exception as exc:  # noqa: BLE001
            logger.error('OAuth Redis delete failed: %s', type(exc).__name__)
    _OAUTH_STORES[kind].pop(key, None)


def _oauth_revoke(token, ttl):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    client = _get_redis_client()
    if client is not None:
        try:
            client.set(_oauth_key('revoked', token_hash), '1', ex=max(1, ttl))
            return
        except Exception as exc:  # noqa: BLE001
            logger.error('OAuth Redis revoke failed: %s', type(exc).__name__)
            if _is_production():
                raise RuntimeError('OAuth state store unavailable') from None
    _revoked_tokens.add(token_hash)


def _oauth_is_revoked(token_hash):
    client = _get_redis_client()
    if client is not None:
        try:
            return bool(client.exists(_oauth_key('revoked', token_hash)))
        except Exception as exc:  # noqa: BLE001
            logger.error('OAuth Redis revocation read failed: %s',
                         type(exc).__name__)
            if _is_production():
                return True
    return token_hash in _revoked_tokens


def register_oauth_routes(blueprint):
    """Register OAuth 2.1 endpoints on the given Flask blueprint."""

    # --- Well-Known Discovery ---

    @blueprint.route('/.well-known/oauth-authorization-server', methods=['GET'])
    def oauth_discovery():
        """RFC 8414 OAuth Authorization Server Metadata (the prefixed copy;
        the issuer-root copy is registered in `main`)."""
        return jsonify(discovery_document())

    # --- SMART-on-FHIR Well-Known ---

    @blueprint.route('/.well-known/smart-configuration', methods=['GET'])
    def smart_configuration():
        """SMART App Launch v2 configuration."""
        base = issuer()
        return jsonify({
            'authorization_endpoint': f'{base}/r6/fhir/oauth/authorize',
            'token_endpoint': f'{base}/r6/fhir/oauth/token',
            'registration_endpoint': f'{base}/r6/fhir/oauth/register',
            'revocation_endpoint': f'{base}/r6/fhir/oauth/revoke',
            'scopes_supported': list(SMART_SCOPES.keys()),
            'capabilities': [
                'launch-standalone',
                'client-public',
                'client-confidential-symmetric',
                'context-standalone-patient',
                'permission-patient',
                'sso-openid-connect',
            ],
            'code_challenge_methods_supported': ['S256'],
        })

    # --- Dynamic Client Registration (RFC 7591) ---

    @blueprint.route('/oauth/register', methods=['POST'])
    def register_client():
        """Register an OAuth client dynamically."""
        body = request.get_json(silent=True)
        if not body:
            return jsonify({'error': 'invalid_request'}), 400

        redirect_uris = body.get('redirect_uris')
        if (not isinstance(redirect_uris, list) or not redirect_uris
                or not all(redirect_uri_allowed(u) for u in redirect_uris)):
            return jsonify({
                'error': 'invalid_redirect_uri',
                'error_description': 'redirect_uris must be https URLs or '
                'plain-http loopback URLs (localhost, 127.0.0.1, [::1])',
            }), 400
        auth_method = body.get('token_endpoint_auth_method') or 'client_secret_post'
        if auth_method not in CLIENT_AUTH_METHODS:
            return jsonify({
                'error': 'invalid_client_metadata',
                'error_description': 'token_endpoint_auth_method must be one of '
                + ', '.join(CLIENT_AUTH_METHODS),
            }), 400

        client_id = str(uuid.uuid4())
        client_secret = None if auth_method == 'none' else secrets.token_urlsafe(32)
        client_name = body.get('client_name', 'Unknown Client')
        scope = body.get('scope', 'fhir.read context.read')
        issued_at = int(time.time())

        _oauth_store_set('client', client_id, {
            'client_secret': client_secret,
            'redirect_uris': redirect_uris,
            'client_name': client_name,
            'scope': scope,
            'token_endpoint_auth_method': auth_method,
            'created_at': issued_at,
        }, ttl=CLIENT_TTL_SECONDS)

        response = {
            'client_id': client_id,
            'client_id_issued_at': issued_at,
            'client_name': client_name,
            'redirect_uris': redirect_uris,
            'scope': scope,
            'token_endpoint_auth_method': auth_method,
        }
        if client_secret is not None:
            response['client_secret'] = client_secret
            response['client_secret_expires_at'] = issued_at + CLIENT_TTL_SECONDS
        return jsonify(response), 201

    # --- Authorization Endpoint ---

    @blueprint.route('/oauth/authorize', methods=['GET'])
    def authorize():
        """OAuth 2.1 authorization endpoint (PKCE required)."""
        client_id = request.args.get('client_id')
        redirect_uri = request.args.get('redirect_uri')
        scope = request.args.get('scope', 'fhir.read')
        state = request.args.get('state', '')
        code_challenge = request.args.get('code_challenge')
        code_challenge_method = request.args.get('code_challenge_method', 'S256')

        if not client_id or not redirect_uri:
            return jsonify({'error': 'invalid_request',
                          'error_description': 'client_id and redirect_uri required'}), 400

        # Validate client exists and redirect_uri is registered
        registered_client = _oauth_store_get('client', client_id)
        if not registered_client:
            return jsonify({'error': 'invalid_client',
                          'error_description': 'Client not registered'}), 401
        if not any(redirect_uri_matches(redirect_uri, registered)
                   for registered in registered_client.get('redirect_uris', [])):
            return jsonify({'error': 'invalid_request',
                          'error_description': 'redirect_uri not registered for this client'}), 400

        if not code_challenge:
            return jsonify({'error': 'invalid_request',
                          'error_description': 'PKCE code_challenge required (RFC 7636)'}), 400

        if code_challenge_method != 'S256':
            return jsonify({'error': 'invalid_request',
                          'error_description': 'Only S256 code_challenge_method supported'}), 400

        # From here the redirect URI is the client's own registered one, so
        # protocol errors go back to it (OAuth 2.1 §4.1.2.1) instead of to a
        # JSON body a browser popup cannot act on.
        resource = (request.args.get('resource') or '').strip()
        policies = resource_policies()
        if resource and resource not in policies:
            # RFC 8707 §2: an audience we do not know is refused, and is never
            # recorded as sent — that would make `aud` a caller-chosen string.
            return _redirect_to_client(redirect_uri, state, error='invalid_target',
                                       error_description='unknown resource')
        audience = resource or fhir_resource()
        policy = policies[audience]

        from r6.command_center.access import is_public
        if policy == 'demo':
            # P2-b: for the MCP audience the tenant is config, not a header.
            # The demo tenant must be public; anything else fails closed here
            # rather than minting the strongest token a browser flow can ask for.
            requested_tenant = os.environ.get('MCP_OAUTH_DEMO_TENANT', '').strip()
            if not requested_tenant or (
                    read_auth_enabled() and not is_public(requested_tenant)):
                return jsonify({
                    'error': 'access_denied',
                    'error_description': 'The MCP resource binds a configured '
                    'demo tenant, and none is configured or it is not public.',
                }), 403
        else:
            # H3: this endpoint AUTO-APPROVES with no consent screen and binds
            # the token's tenant from the request header. When read-auth is on,
            # that would let anyone mint a read bearer for any tenant, bypassing
            # the gate. Restrict auto-approve to public/demo tenants; a protected
            # tenant needs real per-user consent (spec §13, not built here).
            requested_tenant = request.headers.get('X-Tenant-Id', 'default')
            if read_auth_enabled() and not is_public(requested_tenant):
                return jsonify({
                    'error': 'access_denied',
                    'error_description': 'Auto-approve authorization is limited to '
                    'public/demo tenants; protected tenants require per-user consent.',
                }), 403

        # Generate authorization code
        code = secrets.token_urlsafe(32)
        _oauth_store_set('auth-code', code, {
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'code_challenge': code_challenge,
            'code_challenge_method': code_challenge_method,
            'scopes': scope.split(),
            'tenant_id': requested_tenant,
            'aud': audience,
            'exp': time.time() + 600,  # 10 minutes
        }, ttl=600)

        # OAuth 2.1 §4.1.2: the code travels in a 302 to the registered
        # redirect URI, with RFC 9207 `iss`. A JSON body here is a flow no
        # browser can finish (#568).
        return _redirect_to_client(redirect_uri, state, code=code)

    # --- Token Endpoint ---

    @blueprint.route('/oauth/token', methods=['POST'])
    def token():
        """OAuth 2.1 token endpoint."""
        grant_type = request.form.get('grant_type') or (request.get_json(silent=True) or {}).get('grant_type')
        if grant_type != 'authorization_code':
            return jsonify({'error': 'unsupported_grant_type'}), 400

        body = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
        code = body.get('code')
        code_verifier = body.get('code_verifier')
        client_id, client_secret = _presented_client_credentials(body)

        if not code or not code_verifier:
            return jsonify({'error': 'invalid_request',
                          'error_description': 'code and code_verifier required'}), 400

        # Validate authorization code. Popped before the client is checked so
        # a failed exchange burns the code: a code is single-use either way.
        auth_code = _oauth_store_pop('auth-code', code)
        if not auth_code:
            return jsonify({'error': 'invalid_grant',
                          'error_description': 'Authorization code expired or invalid'}), 400

        if auth_code['exp'] < time.time():
            return jsonify({'error': 'invalid_grant',
                          'error_description': 'Authorization code expired'}), 400

        # Client authentication (RFC 6749 §3.2.1). A public client has no
        # secret, so its `client_id` is the only thing binding the code to the
        # client it was issued to (§4.1.3) and is required. A confidential
        # client authenticates with the secret it was issued, POST body or
        # HTTP Basic; a secret that is absent or wrong is `invalid_client`.
        registered_client = _oauth_store_get('client', auth_code['client_id']) or {}
        auth_method = registered_client.get('token_endpoint_auth_method',
                                            'client_secret_post')
        if auth_method == 'none':
            if not client_id:
                return jsonify({'error': 'invalid_request',
                              'error_description': 'client_id required for a public client'}), 400
        else:
            expected_secret = registered_client.get('client_secret') or ''
            if not client_secret or not expected_secret or not constant_time_equal(
                    client_secret, expected_secret):
                return jsonify({'error': 'invalid_client'}), 401

        if client_id and auth_code['client_id'] != client_id:
            return jsonify({'error': 'invalid_grant',
                          'error_description': 'Client ID mismatch'}), 400

        # Verify redirect_uri matches what was used in authorize
        redirect_uri = body.get('redirect_uri')
        if redirect_uri and redirect_uri != auth_code.get('redirect_uri'):
            return jsonify({'error': 'invalid_grant',
                          'error_description': 'redirect_uri mismatch'}), 400

        # Verify PKCE (S256)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b'=').decode()

        if challenge != auth_code['code_challenge']:
            return jsonify({'error': 'invalid_grant',
                          'error_description': 'PKCE verification failed'}), 400

        # RFC 8707 (P2-c): a `resource` here must be the one the code was
        # issued for; absent, it inherits. A code for one audience is never
        # redeemed for another.
        resource = (body.get('resource') or '').strip()
        if resource and resource != auth_code.get('aud'):
            return jsonify({'error': 'invalid_target',
                          'error_description': 'resource does not match the authorization'}), 400

        # Issue access token
        access_token = secrets.token_urlsafe(48)
        _oauth_store_set('access-token', access_token, {
            'client_id': auth_code['client_id'],
            'scopes': auth_code['scopes'],
            'tenant_id': auth_code['tenant_id'],
            'aud': auth_code.get('aud'),
            'exp': time.time() + TOKEN_TTL_SECONDS,
        }, ttl=TOKEN_TTL_SECONDS)

        return jsonify({
            'access_token': access_token,
            'token_type': 'Bearer',
            'expires_in': TOKEN_TTL_SECONDS,
            'scope': ' '.join(auth_code['scopes']),
        })

    # --- Token Introspection (RFC 7662) ---

    @blueprint.route('/oauth/introspect', methods=['POST'])
    def introspect():
        """Answer whether a token is live, and for whom (spec §3.5 item 3).

        On any doubt the answer is `{"active": false}` and nothing else: an
        unknown, expired or revoked token gets the same shape, so the
        response never says which. The token value is not echoed.
        """
        body = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
        if not introspection_client_authorized(body):
            response = jsonify({'error': 'invalid_client'})
            response.headers['WWW-Authenticate'] = 'Basic realm="introspection"'
            return response, 401
        inactive = jsonify({'active': False})
        inactive.headers['Cache-Control'] = 'no-store'
        token_value = (body.get('token') or '').strip()
        if not token_value:
            return inactive
        ok, info = validate_bearer_token(token_value)
        if not ok or not isinstance(info, dict):
            return inactive
        response = jsonify({
            'active': True,
            'token_type': 'Bearer',
            'aud': info.get('aud'),
            'scope': ' '.join(info.get('scopes') or []),
            'tenant_id': info.get('tenant_id'),
            'client_id': info.get('client_id'),
            'exp': int(info['exp']),
        })
        response.headers['Cache-Control'] = 'no-store'
        return response

    # --- Token Revocation (RFC 7009) ---

    @blueprint.route('/oauth/revoke', methods=['POST'])
    def revoke():
        """Revoke an access token."""
        body = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
        token_value = body.get('token')
        if token_value:
            token_info = _oauth_store_get('access-token', token_value) or {}
            ttl = max(1, int(token_info.get('exp', time.time()) - time.time()))
            _oauth_store_delete('access-token', token_value)
            _oauth_revoke(token_value, ttl)
        return '', 200


def validate_bearer_token(token):
    """
    Validate a bearer token and return (is_valid, token_info_or_error).

    Returns:
        tuple: (True, {client_id, scopes, tenant_id}) or (False, error_string)
    """
    if not token:
        return False, 'No token provided'

    # Check revocation
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if _oauth_is_revoked(token_hash):
        return False, 'Token has been revoked'

    token_info = _oauth_store_get('access-token', token)
    if not token_info:
        return False, 'Token not found or expired'

    if token_info['exp'] < time.time():
        _oauth_store_delete('access-token', token)
        return False, 'Token expired'

    return True, token_info


def require_scope(*required_scopes):
    """
    Flask route decorator that checks for required OAuth scopes.
    Falls through gracefully if no Authorization header is present
    (allowing HMAC step-up to handle auth instead).
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            auth_header = request.headers.get('Authorization', '')
            if not auth_header.startswith('Bearer '):
                # No OAuth token — fall through to step-up auth
                return f(*args, **kwargs)

            token = auth_header[7:]
            valid, result = validate_bearer_token(token)
            if not valid:
                return jsonify({
                    'resourceType': 'OperationOutcome',
                    'issue': [{
                        'severity': 'error',
                        'code': 'security',
                        'diagnostics': f'Bearer token rejected: {result}'
                    }]
                }), 401

            # Check scopes
            token_scopes = set(result['scopes'])
            if not any(s in token_scopes for s in required_scopes):
                return jsonify({
                    'resourceType': 'OperationOutcome',
                    'issue': [{
                        'severity': 'error',
                        'code': 'security',
                        'diagnostics': f'Insufficient scope. Required: {", ".join(required_scopes)}'
                    }]
                }), 403

            return f(*args, **kwargs)
        return decorated
    return decorator
