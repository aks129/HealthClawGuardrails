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
from flask import request, jsonify

logger = logging.getLogger(__name__)

# Configuration
OAUTH_ISSUER = os.environ.get('OAUTH_ISSUER', '')
OAUTH_SECRET = os.environ.get('OAUTH_SECRET', os.environ.get('STEP_UP_SECRET', ''))
TOKEN_TTL_SECONDS = int(os.environ.get('OAUTH_TOKEN_TTL', '3600'))

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
            if (os.environ.get('APP_ENV') or os.environ.get('FLASK_ENV')) == 'production':
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
            if (os.environ.get('APP_ENV') or os.environ.get('FLASK_ENV')) == 'production':
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
            if (os.environ.get('APP_ENV') or os.environ.get('FLASK_ENV')) == 'production':
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
            if (os.environ.get('APP_ENV') or os.environ.get('FLASK_ENV')) == 'production':
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
            if (os.environ.get('APP_ENV') or os.environ.get('FLASK_ENV')) == 'production':
                return True
    return token_hash in _revoked_tokens


def register_oauth_routes(blueprint):
    """Register OAuth 2.1 endpoints on the given Flask blueprint."""

    # --- Well-Known Discovery ---

    @blueprint.route('/.well-known/oauth-authorization-server', methods=['GET'])
    def oauth_discovery():
        """RFC 8414 OAuth Authorization Server Metadata."""
        base = request.host_url.rstrip('/')
        return jsonify({
            'issuer': OAUTH_ISSUER or base,
            'authorization_endpoint': f'{base}/r6/fhir/oauth/authorize',
            'token_endpoint': f'{base}/r6/fhir/oauth/token',
            'registration_endpoint': f'{base}/r6/fhir/oauth/register',
            'revocation_endpoint': f'{base}/r6/fhir/oauth/revoke',
            'scopes_supported': list(SMART_SCOPES.keys()),
            'response_types_supported': ['code'],
            'grant_types_supported': ['authorization_code'],
            'token_endpoint_auth_methods_supported': ['client_secret_post', 'none'],
            'code_challenge_methods_supported': ['S256'],
            'service_documentation': f'{base}/r6/fhir/docs/privacy-policy',
        })

    # --- SMART-on-FHIR Well-Known ---

    @blueprint.route('/.well-known/smart-configuration', methods=['GET'])
    def smart_configuration():
        """SMART App Launch v2 configuration."""
        base = request.host_url.rstrip('/')
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

        client_id = str(uuid.uuid4())
        client_secret = secrets.token_urlsafe(32)
        redirect_uris = body.get('redirect_uris', [])
        client_name = body.get('client_name', 'Unknown Client')
        scope = body.get('scope', 'fhir.read context.read')

        _oauth_store_set('client', client_id, {
            'client_secret': client_secret,
            'redirect_uris': redirect_uris,
            'client_name': client_name,
            'scope': scope,
            'created_at': time.time(),
        }, ttl=30 * 24 * 3600)

        return jsonify({
            'client_id': client_id,
            'client_secret': client_secret,
            'client_name': client_name,
            'redirect_uris': redirect_uris,
            'scope': scope,
            'token_endpoint_auth_method': 'client_secret_post',
        }), 201

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
        if redirect_uri not in registered_client.get('redirect_uris', []):
            return jsonify({'error': 'invalid_request',
                          'error_description': 'redirect_uri not registered for this client'}), 400

        if not code_challenge:
            return jsonify({'error': 'invalid_request',
                          'error_description': 'PKCE code_challenge required (RFC 7636)'}), 400

        if code_challenge_method != 'S256':
            return jsonify({'error': 'invalid_request',
                          'error_description': 'Only S256 code_challenge_method supported'}), 400

        # H3: this endpoint AUTO-APPROVES with no consent screen and binds the
        # token's tenant from the request header. When read-auth is on, that
        # would let anyone mint a read bearer for any tenant, bypassing the gate.
        # Restrict auto-approve to public/demo tenants; a protected tenant needs
        # real per-user consent (out of scope for this reference OAuth server).
        requested_tenant = request.headers.get('X-Tenant-Id', 'default')
        from r6.command_center.access import is_public
        from r6.routes import _read_auth_enabled
        if _read_auth_enabled() and not is_public(requested_tenant):
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
            'exp': time.time() + 600,  # 10 minutes
        }, ttl=600)

        # In production, this would render a consent screen.
        # For the MCP server, we auto-approve and redirect.
        separator = '&' if '?' in redirect_uri else '?'
        location = f'{redirect_uri}{separator}code={code}&state={state}'
        return jsonify({
            'redirect': location,
            'code': code,
            'state': state,
        })

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
        client_id = body.get('client_id')

        if not code or not code_verifier:
            return jsonify({'error': 'invalid_request',
                          'error_description': 'code and code_verifier required'}), 400

        # Validate authorization code
        auth_code = _oauth_store_pop('auth-code', code)
        if not auth_code:
            return jsonify({'error': 'invalid_grant',
                          'error_description': 'Authorization code expired or invalid'}), 400

        if auth_code['exp'] < time.time():
            return jsonify({'error': 'invalid_grant',
                          'error_description': 'Authorization code expired'}), 400

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

        # Issue access token
        access_token = secrets.token_urlsafe(48)
        _oauth_store_set('access-token', access_token, {
            'client_id': auth_code['client_id'],
            'scopes': auth_code['scopes'],
            'tenant_id': auth_code['tenant_id'],
            'exp': time.time() + TOKEN_TTL_SECONDS,
        }, ttl=TOKEN_TTL_SECONDS)

        return jsonify({
            'access_token': access_token,
            'token_type': 'Bearer',
            'expires_in': TOKEN_TTL_SECONDS,
            'scope': ' '.join(auth_code['scopes']),
        })

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
