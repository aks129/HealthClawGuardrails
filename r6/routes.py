"""
R6 FHIR REST Facade - Flask Blueprint.

Reference implementation of MCP guardrail patterns for FHIR R6 agent access.
NOT a production FHIR server — stores resources as JSON blobs with structural
validation only. Designed to demonstrate security patterns (tenant isolation,
step-up auth, audit, redaction, human-in-the-loop) that real FHIR+MCP
integrations would need.

Search supports: patient, code, status, _lastUpdated, _count, _sort, _summary.
Validation: structural checks for required fields. Falls back when external
validator unavailable. No StructureDefinition or terminology binding validation.
"""

import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode
from flask import (
    Blueprint, request, jsonify, Response, stream_with_context,
    render_template,
)
from werkzeug.http import (
    parse_list_header,
    parse_options_header,
    unquote_header_value,
)
from models import db
from r6.models import R6Resource, ContextEnvelope, ContextItem, AuditEventRecord
from r6.context_builder import ContextBuilder
from r6.validator import R6Validator
from r6.audit import add_audit_event, record_audit_event
from r6.redaction import apply_patient_controlled_redaction
from r6.redaction import apply_redaction
from r6.access import (Scope, TenantRejected, TenantSource, require_grant,
                       public_step_up_reason, tenant_from_request)
from r6.stepup import validate_step_up_token, generate_step_up_token
from r6.oauth import register_oauth_routes
from r6.read_auth import (
    authorize_tenant_read,
    read_auth_enabled as _read_auth_enabled,
    read_auth_required as _read_auth_required,
)
from r6.runtime_config import resolve_app_env
from r6.version import __version__
from r6.body_guard import (INGEST_MAX_JSON_DEPTH as _INGEST_MAX_JSON_DEPTH,
                           json_body_within_depth,
                           json_depth_within as _json_depth_within)
from r6.rate_limit import rate_limit_middleware
from r6.health_compliance import (
    add_disclaimer, enforce_human_in_loop, deidentify_resource,
    export_audit_trail, MEDICAL_DISCLAIMER
)
from r6.fhir_proxy import (
    get_proxy_for_request,
    is_proxy_enabled,
    upstream_status,
    is_sharp_context_active,
    close_request_proxy,
    sanitize_operation_outcome_resource,
    SHARP_SERVER_URL_HEADER,
)
from r6.curatr import (
    CuratrEngine,
    apply_fix as _curatr_apply_fix,
    persist_curation_state as _persist_curation_state,
)
from r6.health_context import get as _hc_get
from r6.caregaps.report import caller_reasons as _caregaps_caller_reasons

_curatr_engine = CuratrEngine()

logger = logging.getLogger(__name__)

r6_blueprint = Blueprint('r6', __name__, url_prefix='/r6/fhir')

# Register OAuth 2.1 endpoints
register_oauth_routes(r6_blueprint)

# Register rate limiting
rate_limit_middleware(r6_blueprint)

# SHARP-on-MCP: close any per-request upstream proxy created from
# X-FHIR-Server-URL / X-FHIR-Access-Token headers.
r6_blueprint.teardown_request(close_request_proxy)

# R6 version identifier aligned with ballot build
R6_FHIR_VERSION = '6.0.0-ballot3'

# Initialize services
context_builder = ContextBuilder()
validator = R6Validator()

# Valid FHIR id pattern
_FHIR_ID_PATTERN = re.compile(r'^[A-Za-z0-9\-.]{1,64}$')

# Valid tenant_id pattern: alphanumeric, hyphens, underscores, 1-64 chars
_TENANT_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')

# AuditEvent is system-managed — block external CRUD
_SYSTEM_MANAGED_TYPES = {'AuditEvent'}

# Phase 2: R6-specific valid Permission combining codes
_PERMISSION_COMBINING_CODES = {
    'deny-overrides', 'permit-overrides', 'ordered-deny-overrides',
    'ordered-permit-overrides', 'deny-unless-permit', 'permit-unless-deny',
}

# Valid Bundle types per FHIR spec
_VALID_BUNDLE_TYPES = {
    'document', 'message', 'transaction', 'transaction-response',
    'batch', 'batch-response', 'history', 'searchset', 'collection',
    'subscription-notification',
}

# Valid FHIR search patient reference pattern
_PATIENT_REF_PATTERN = re.compile(r'^Patient/[A-Za-z0-9\-.]{1,64}$')

# Local-search contract: discovery, validation, corrective messages, and self
# links all derive from this ordered registry.
_SEARCH_PARAMETER_SPECS = (
    {'name': 'patient', 'type': 'reference',
     'documentation': 'Filter by subject.reference (Patient/{id})'},
    {'name': 'code', 'type': 'token',
     'documentation': 'Filter by code.coding[].code (JSON string match)'},
    {'name': 'status', 'type': 'token',
     'documentation': 'Filter by status field'},
    {'name': '_lastUpdated', 'type': 'date',
     'documentation': 'Filter by last updated (ge/le/gt/lt prefix)'},
    {'name': '_count', 'type': 'number',
     'documentation': 'Max results (0-200)'},
    {'name': '_sort', 'type': 'string',
     'documentation': '_lastUpdated or -_lastUpdated'},
    {'name': '_summary', 'type': 'token',
     'documentation': 'count'},
    {'name': 'context-id', 'type': 'token',
     'documentation': 'Filter by local context envelope'},
)
_SUPPORTED_SEARCH_PARAMS = frozenset(
    spec['name'] for spec in _SEARCH_PARAMETER_SPECS)
_SUPPORTED_PARAMS_TEXT = ', '.join(
    spec['name'] for spec in _SEARCH_PARAMETER_SPECS)

_AUDIT_SEARCH_PARAMETER_SPECS = (
    {'name': 'context-id', 'type': 'token',
     'documentation': 'Filter by local context envelope'},
    {'name': 'entity-type', 'type': 'token',
     'documentation': 'Filter by audited resource type'},
    {'name': '_count', 'type': 'number',
     'documentation': 'Max results (0-200)'},
)
_AUDIT_SEARCH_PARAMS = frozenset(
    spec['name'] for spec in _AUDIT_SEARCH_PARAMETER_SPECS)
_AUDIT_PARAMS_TEXT = ', '.join(
    spec['name'] for spec in _AUDIT_SEARCH_PARAMETER_SPECS)

# The blueprint's url_prefix. Exemptions are matched against the full request
# path, so they must be anchored to this prefix — NOT matched by suffix.
# FHIR resource ids match [A-Za-z0-9.\-]{1,64}, so a suffix test like
# path.endswith('/metadata') also matches GET /r6/fhir/Patient/metadata (a
# resource read of id "metadata"), which would silently exempt a real read
# from tenant/read-auth enforcement. Anchor every exemption instead.
_R6_PREFIX = '/r6/fhir'

# Discovery/public endpoints exempt from tenant + read-auth enforcement.
# EXACT full paths — never suffix-matched.
_EXEMPT_EXACT_PATHS = frozenset({
    f'{_R6_PREFIX}/metadata',       # CapabilityStatement
    f'{_R6_PREFIX}/health',         # health check
    f'{_R6_PREFIX}/$conformance',   # guardrail self-test (self-tenanted internally)
})

# Genuinely-namespaced sub-trees exempt from tenant + read-auth enforcement.
# These are prefix-matched because every path under them is non-clinical and
# no FHIR resource read can introduce one of these segments: a resource read is
# /{prefix}/{ResourceType}/{id}, where ResourceType is a single bare segment —
# it can never be "internal", ".well-known", "oauth", "mcp-apps", or "demo"
# *followed by another '/segment'*. (e.g. /r6/fhir/demo/agent-loop is the demo
# endpoint; /r6/fhir/Demo is a — nonexistent — resource type but still a single
# segment, so it would NOT match these prefixes.)
_EXEMPT_PATH_PREFIXES = (
    f'{_R6_PREFIX}/internal/',
    f'{_R6_PREFIX}/.well-known/',
    f'{_R6_PREFIX}/oauth/',
    f'{_R6_PREFIX}/mcp-apps/',
    f'{_R6_PREFIX}/demo/',
)


def _is_exempt_discovery_path(path):
    """True if `path` is a public discovery/namespaced endpoint.

    Exact-match the literal discovery routes (/metadata, /health) and
    prefix-match the namespaced sub-trees. Crucially this does NOT use a
    suffix test, so a FHIR read like /r6/fhir/Patient/metadata (resource id
    "metadata") is NOT treated as discovery and stays fully gated.
    """
    if path in _EXEMPT_EXACT_PATHS:
        return True
    return any(path.startswith(p) for p in _EXEMPT_PATH_PREFIXES)


# --- Tenant Enforcement ---

@r6_blueprint.before_request
def enforce_tenant_id():
    """Require X-Tenant-Id header on all endpoints except public discovery.

    Discovery exemptions are matched by EXACT path / namespaced prefix via
    _is_exempt_discovery_path — never by suffix. A suffix test would let a
    FHIR read of resource id "metadata"/"health" (e.g. GET
    /r6/fhir/Patient/metadata) slip past tenant enforcement.
    """
    # Public discovery + namespaced endpoints (no tenant required).
    # /mcp-apps/ HTML renders without a tenant header; the tenant arrives via
    # query string or the MCP client's outer session.
    if _is_exempt_discovery_path(request.path):
        return None
    tenant_id = request.headers.get('X-Tenant-Id')
    # SHARP-on-MCP: requests bearing X-FHIR-Server-URL carry their own
    # FHIR-level identity (SMART access token). Synthesize a stable tenant
    # from the upstream URL when X-Tenant-Id is omitted so audit + guardrails
    # still scope correctly per SHARP context.
    if not tenant_id and is_sharp_context_active():
        import hashlib
        sharp_url = (request.headers.get(SHARP_SERVER_URL_HEADER) or '').strip()
        digest = hashlib.sha256(sharp_url.encode('utf-8')).hexdigest()[:16]
        tenant_id = f'sharp-{digest}'
        request.environ['HTTP_X_TENANT_ID'] = tenant_id
    if not tenant_id:
        return jsonify({
            'resourceType': 'OperationOutcome',
            'issue': [{
                'severity': 'error',
                'code': 'security',
                'diagnostics': 'X-Tenant-Id header is required'
            }]
        }), 400
    # Validate tenant_id format
    if not _TENANT_ID_PATTERN.fullmatch(tenant_id):
        return jsonify({
            'resourceType': 'OperationOutcome',
            'issue': [{
                'severity': 'error',
                'code': 'invalid',
                'diagnostics': 'X-Tenant-Id must match [a-zA-Z0-9_-]{1,64}'
            }]
        }), 400


def authenticate_tenant_read(tenant_id):
    """Validate read credentials for `tenant_id`.

    Shared by the GET before_request hook and POST read-shaped operations
    (e.g. Questionnaire/$populate). Returns None when access is allowed,
    or an (OperationOutcome, status) tuple to abort with.

    Mirrors the gate semantics: public tenants and the disabled flag pass;
    otherwise a tenant-bound step-up token OR a SMART bearer is required.
    """
    if authorize_tenant_read(tenant_id) is not None:
        return None
    # Do NOT leak whether the tenant exists or why the token failed.
    return _operation_outcome(
        'error', 'security',
        f"Read access to tenant '{tenant_id}' requires authentication",
    ), 401


@r6_blueprint.before_request
def authenticate_read():
    """Authenticate the tenant claim on FHIR reads (flag-gated).

    The base header-only tenant enforcement does NOT authenticate the
    X-Tenant-Id claim — any client can read a tenant's redacted data with
    just the header. When READ_AUTH_ENABLED is on, GET reads of non-public
    tenants must present a step-up token bound to that tenant.

    Default behavior (flag off) is unchanged — this returns None and the
    request proceeds exactly as before. Writes are not touched here; they
    already require step-up in their own handlers.
    """
    # Only reads. Writes (POST/PUT/PATCH/DELETE) are gated elsewhere.
    if request.method != 'GET':
        return None

    # No-op unless the flag is explicitly enabled.
    if not _read_auth_enabled():
        return None

    # Exempt the same public/discovery endpoints as the tenant hook —
    # these have no tenant and need no auth. Matched by exact path / namespaced
    # prefix (NOT suffix), so /r6/fhir/Patient/metadata is a gated read, not a
    # discovery exemption.
    if _is_exempt_discovery_path(request.path):
        return None

    tenant_id = request.headers.get('X-Tenant-Id')
    # SHARP-on-MCP requests carry their own FHIR-level identity (SMART
    # access token) and a synthesized tenant; the upstream proxy enforces
    # auth. Don't double-gate them.
    if not tenant_id and is_sharp_context_active():
        return None
    if not tenant_id:
        # The tenant hook already rejected this; defensive no-op.
        return None

    if not _read_auth_required(tenant_id):
        return None

    # Tenant-bound token required. Two accepted credentials:
    #   1. HMAC step-up token, via X-Step-Up-Token or Authorization: Bearer.
    #   2. A SMART-on-FHIR OAuth access token (the mechanism the
    #      CapabilityStatement advertises), via Authorization: Bearer.
    # Delegated to the reusable helper so POST read-shaped operations can
    # apply the exact same gate.
    return authenticate_tenant_read(tenant_id)


# --- Human-in-the-Loop Enforcement ---

@r6_blueprint.before_request
def check_human_confirmation():
    """Enforce human-in-the-loop for clinical writes."""
    result = enforce_human_in_loop()
    if result:
        return result


def _oauth_base():
    """Base URL for SMART OAuth endpoints, matching r6.oauth's discovery docs.

    Built from the request host the same way oauth.py builds its
    .well-known/smart-configuration endpoints — never hardcode the domain.
    """
    return request.host_url.rstrip('/') + '/r6/fhir/oauth'


@r6_blueprint.route('/metadata', methods=['GET'])
def r6_metadata():
    """
    Return a CapabilityStatement declaring R4 US Core v9 + R6 ballot3 support.
    """
    capability_statement = {
        'resourceType': 'CapabilityStatement',
        'id': 'r6-showcase',
        'status': 'active',
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'kind': 'instance',
        'fhirVersion': R6_FHIR_VERSION,
        'format': ['json'],
        'software': {
            'name': 'HealthClaw Guardrails',
            'version': _hc_get('version', '1.2.0'),
        },
        'implementation': {
            'description': (
                'MCP guardrail proxy supporting FHIR R4 (US Core v9) stable resources '
                'and FHIR R6 ballot3 experimental resources. '
                + ('Proxying to upstream FHIR server with full guardrail layer (redaction, audit, step-up auth).'
                   if is_proxy_enabled()
                   else 'Local JSON blob storage with structural validation. Not a production server.')
            ),
            'url': request.host_url.rstrip('/') + '/r6/fhir'
        },
        'instantiates': [
            'http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient',
        ],
        'implementationGuide': [
            'http://hl7.org/fhir/us/core/ImplementationGuide/hl7.fhir.us.core',
        ],
        'rest': [
            {
                'mode': 'server',
                'security': {
                    'cors': True,
                    'service': [{
                        'coding': [{
                            'system': 'http://terminology.hl7.org/CodeSystem/restful-security-service',
                            'code': 'SMART-on-FHIR',
                            'display': 'SMART-on-FHIR'
                        }]
                    }],
                    'extension': [{
                        'url': 'http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris',
                        'extension': [
                            {'url': 'authorize', 'valueUri': _oauth_base() + '/authorize'},
                            {'url': 'token', 'valueUri': _oauth_base() + '/token'},
                            {'url': 'register', 'valueUri': _oauth_base() + '/register'},
                        ]
                    }]
                },
                'resource': [
                    _resource_capability(rt) for rt in R6Resource.SUPPORTED_TYPES
                ],
                'operation': [
                    {
                        'name': 'validate',
                        'definition': 'http://hl7.org/fhir/OperationDefinition/Resource-validate'
                    },
                    {
                        'name': 'ingest-context',
                        'definition': request.host_url.rstrip('/') + '/r6/fhir/Bundle/$ingest-context'
                    },
                    {
                        'name': 'stats',
                        'definition': 'http://hl7.org/fhir/OperationDefinition/Observation-stats'
                    },
                    {
                        'name': 'lastn',
                        'definition': 'http://hl7.org/fhir/OperationDefinition/Observation-lastn'
                    },
                ]
            }
        ]
    }
    return jsonify(capability_statement)


def _resource_capability(resource_type):
    """Build a resource entry for the CapabilityStatement."""
    interactions = [
        {'code': 'read'},
        {'code': 'create'},
        {'code': 'update'},
        {'code': 'search-type'},
    ]
    search_specs = (_AUDIT_SEARCH_PARAMETER_SPECS
                    if resource_type == 'AuditEvent'
                    else _SEARCH_PARAMETER_SPECS)
    return {
        'type': resource_type,
        'interaction': interactions,
        'versioning': 'versioned',
        'readHistory': False,
        'updateCreate': False,
        'searchParam': [dict(spec) for spec in search_specs],
    }


# --- CRUD Operations ---

@r6_blueprint.route('/<resource_type>', methods=['POST'])
def create_resource(resource_type):
    """Create a new R6 FHIR resource."""
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome('error', 'not-supported',
                                  f'Resource type {resource_type} is not supported'), 400

    # Block external creation of system-managed resources
    if resource_type in _SYSTEM_MANAGED_TYPES:
        return _operation_outcome('error', 'security',
                                  f'{resource_type} is system-managed and cannot be created via API'), 403

    # This parse runs BEFORE the step-up gate below — a tenant header is all
    # it takes to reach it (#312). Bounding the depth is what stops that from
    # being an unauthenticated crash lever; the ordering itself is pinned by
    # test_the_step_up_gate_runs_before_the_body_is_parsed and is not changed
    # here.
    body, too_deep = json_body_within_depth()
    if too_deep:
        return _operation_outcome('error', 'invalid',
                                  'Request body nesting is too deep'), 400
    # isinstance, not truthiness: `[1]`, `42` and `"text"` are all truthy and
    # all lack .get(), so a bare `if not body` lets them through to an
    # AttributeError and a 500 (#330).
    if not isinstance(body, dict):
        return _operation_outcome('error', 'invalid', 'Request body must be valid JSON'), 400

    if body.get('resourceType') != resource_type:
        return _operation_outcome('error', 'invalid',
                                  f'resourceType mismatch: expected {resource_type}'), 400

    # Step-up authorization check with HMAC validation
    tenant = tenant_from_request(sources=(TenantSource.HEADER,))
    tenant_id = tenant.id
    # Access kernel, slice 6. 401 for both halves keeps this site's dialect.
    # The refusal text changes: the kernel names the nine causes a caller can
    # act on and collapses 'Token tenant mismatch', which this line used to
    # hand back verbatim (#478).
    require_grant(scope=Scope.WRITE, tenant=tenant,
                  absent_status=401, rejected_status=401)

    # Validate before storing (agent proposals must pass $validate before commit)
    validation_result = validator.validate_resource(body)
    if not validation_result['valid']:
        return jsonify(validation_result['operation_outcome']), 422

    # Validate client-supplied id if present
    client_id = body.get('id')
    if client_id and not _FHIR_ID_PATTERN.fullmatch(client_id):
        return _operation_outcome('error', 'invalid',
                                  'Resource id must match [A-Za-z0-9\\-.]{1,64}'), 400

    # --- Upstream proxy mode: create on real FHIR server ---
    proxy = get_proxy_for_request()
    if proxy:
        result, status_code = proxy.create(resource_type, body)
        if result and status_code in (200, 201):
            record_audit_event('create', resource_type, result.get('id'),
                               agent_id=request.headers.get('X-Agent-Id'),
                               tenant_id=tenant_id,
                               detail='source=upstream')
            result = add_disclaimer(result, resource_type)
            result['_source'] = 'upstream'
            response = jsonify(result)
            response.status_code = status_code
            return response
        # Upstream rejected the create — audit the failure and surface the
        # sanitized OperationOutcome with its real status.
        record_audit_event('create', resource_type, None,
                           agent_id=request.headers.get('X-Agent-Id'),
                           tenant_id=tenant_id,
                           outcome='failure',
                           detail=f'create (upstream): rejected HTTP {status_code}')
        if result:
            return jsonify(result), status_code
        return _operation_outcome('error', 'exception',
                                  'Upstream FHIR server rejected the resource'), status_code

    # --- Local mode: store in SQLite ---
    resource_json = json.dumps(body, separators=(',', ':'), sort_keys=True)
    resource = R6Resource(
        resource_type=resource_type,
        resource_json=resource_json,
        resource_id=client_id,
        tenant_id=tenant_id
    )

    try:
        db.session.add(resource)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f'Failed to create {resource_type}: {e}')
        return _operation_outcome('error', 'exception',
                                  'Failed to store resource'), 500

    record_audit_event('create', resource_type, resource.id,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id)

    fhir_json = resource.to_fhir_json()
    fhir_json = add_disclaimer(fhir_json, resource_type)
    response = jsonify(fhir_json)
    response.status_code = 201
    response.headers['Location'] = f'/r6/fhir/{resource_type}/{resource.id}'
    response.headers['ETag'] = f'W/"{resource.version_id}"'
    return response


@r6_blueprint.route('/<resource_type>/<resource_id>', methods=['GET'])
def read_resource(resource_type, resource_id):
    """Read a specific R6 FHIR resource (redacted)."""
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome('error', 'not-supported',
                                  f'Resource type {resource_type} is not supported'), 400

    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id

    # --- Upstream proxy mode: fetch from real FHIR server ---
    proxy = get_proxy_for_request()
    if proxy:
        fhir_json, upstream_status = proxy.read(resource_type, resource_id)
        if upstream_status == 404:
            # Not-found is still an access attempt — audit it (every FHIR
            # resource access emits an AuditEvent, failures included).
            record_audit_event('read', resource_type, resource_id,
                               agent_id=request.headers.get('X-Agent-Id'),
                               context_id=request.headers.get('X-Context-Id'),
                               tenant_id=tenant_id,
                               outcome='failure',
                               detail='read (upstream): HTTP 404 not found')
            return _operation_outcome('error', 'not-found',
                                      f'{resource_type}/{resource_id} not found'), 404
        if upstream_status != 200:
            # Surface the (sanitized) upstream failure with its real status —
            # a 401/500 must not masquerade as not-found — and audit the
            # failed access so denied/failed reads are visible in the trail.
            record_audit_event('read', resource_type, resource_id,
                               agent_id=request.headers.get('X-Agent-Id'),
                               context_id=request.headers.get('X-Context-Id'),
                               tenant_id=tenant_id,
                               outcome='failure',
                               detail=f'read (upstream): HTTP {upstream_status}')
            return jsonify(fhir_json), upstream_status

        record_audit_event('read', resource_type, resource_id,
                           agent_id=request.headers.get('X-Agent-Id'),
                           context_id=request.headers.get('X-Context-Id'),
                           tenant_id=tenant_id,
                           detail='source=upstream')

        # Guardrails still apply on upstream data
        redacted = apply_redaction(fhir_json)
        redacted = add_disclaimer(redacted, resource_type)
        redacted['_source'] = 'upstream'
        return jsonify(redacted)

    # --- Local mode: query SQLite ---
    resource = R6Resource.query.filter_by(
        id=resource_id, resource_type=resource_type,
        is_deleted=False, tenant_id=tenant_id
    ).first()

    if not resource:
        return _operation_outcome('error', 'not-found',
                                  f'{resource_type}/{resource_id} not found'), 404

    record_audit_event('read', resource_type, resource_id,
                       agent_id=request.headers.get('X-Agent-Id'),
                       context_id=request.headers.get('X-Context-Id'),
                       tenant_id=tenant_id)

    # Apply redaction on all reads — consistent with context envelope behavior
    fhir_json = resource.to_fhir_json()
    redacted = apply_redaction(fhir_json)
    redacted = add_disclaimer(redacted, resource_type)

    response = jsonify(redacted)
    response.headers['ETag'] = f'W/"{resource.version_id}"'
    return response


@r6_blueprint.route('/<resource_type>/<resource_id>', methods=['PUT'])
def update_resource(resource_type, resource_id):
    """Update an existing R6 FHIR resource."""
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome('error', 'not-supported',
                                  f'Resource type {resource_type} is not supported'), 400

    # Block updates to system-managed resources
    if resource_type in _SYSTEM_MANAGED_TYPES:
        return _operation_outcome('error', 'security',
                                  f'{resource_type} is system-managed and cannot be modified via API'), 403

    # Step-up authorization with HMAC validation
    tenant = tenant_from_request(sources=(TenantSource.HEADER,))
    tenant_id = tenant.id
    # Access kernel, slice 6. 401 for both halves keeps this site's dialect.
    # The refusal text changes: the kernel names the nine causes a caller can
    # act on and collapses 'Token tenant mismatch', which this line used to
    # hand back verbatim (#478).
    require_grant(scope=Scope.WRITE, tenant=tenant,
                  absent_status=401, rejected_status=401)

    # Unlike create, update gates before it parses, so only a token holder
    # reaches this line. That is a smaller blast radius, not a closed one: a
    # public tenant mints a step-up token with no credential, so the same
    # payload still reaches the same parser (#312).
    body, too_deep = json_body_within_depth()
    if too_deep:
        return _operation_outcome('error', 'invalid',
                                  'Request body nesting is too deep'), 400
    # isinstance, not truthiness: `[1]`, `42` and `"a string"` are truthy and
    # lack .get(), so a bare `if not body` let them through to an
    # AttributeError and a 500. Create got this in #331; update kept the old
    # shape, and the two guards sat 190 lines apart looking equivalent.
    if not isinstance(body, dict):
        return _operation_outcome('error', 'invalid', 'Request body must be valid JSON'), 400

    # Validate resourceType matches URL
    if body.get('resourceType') != resource_type:
        return _operation_outcome('error', 'invalid',
                                  f'resourceType mismatch: expected {resource_type}'), 400

    # Validate body id matches URL id
    if body.get('id') and body['id'] != resource_id:
        return _operation_outcome('error', 'invalid',
                                  f'Resource id in body ({body["id"]}) does not match URL ({resource_id})'), 400

    if_match = request.headers.get('If-Match')

    # Run $validate pre-commit
    validation_result = validator.validate_resource(body)
    if not validation_result['valid']:
        return jsonify(validation_result['operation_outcome']), 422

    # --- Upstream proxy mode: update on real FHIR server ---
    proxy = get_proxy_for_request()
    if proxy:
        result, status_code = proxy.update(resource_type, resource_id, body, if_match)
        if result and status_code in (200, 201):
            record_audit_event('update', resource_type, resource_id,
                               agent_id=request.headers.get('X-Agent-Id'),
                               tenant_id=tenant_id,
                               detail='source=upstream')
            result = add_disclaimer(result, resource_type)
            result['_source'] = 'upstream'
            return jsonify(result)
        # Upstream rejected the update — audit the failure and surface the
        # sanitized OperationOutcome with its real status.
        record_audit_event('update', resource_type, resource_id,
                           agent_id=request.headers.get('X-Agent-Id'),
                           tenant_id=tenant_id,
                           outcome='failure',
                           detail=f'update (upstream): rejected HTTP {status_code}')
        if result:
            return jsonify(result), status_code
        return _operation_outcome('error', 'exception',
                                  'Upstream FHIR server rejected the update'), status_code

    # --- Local mode ---
    # Local tenant isolation and optimistic concurrency are evaluated only
    # after proxy selection: an upstream-only resource has no shadow DB row.
    resource = R6Resource.query.filter_by(
        id=resource_id, resource_type=resource_type,
        is_deleted=False, tenant_id=tenant_id
    ).first()

    if not resource:
        return _operation_outcome('error', 'not-found',
                                  f'{resource_type}/{resource_id} not found'), 404

    if if_match:
        # Normalize: strip W/ prefix and quotes for comparison.
        expected = if_match.strip().lstrip('W/').strip('"')
        actual = str(resource.version_id)
        if expected != actual:
            return _operation_outcome('error', 'conflict',
                                      'Resource has been modified (ETag mismatch)'), 409

    resource_json = json.dumps(body, separators=(',', ':'), sort_keys=True)
    resource.update_resource(resource_json)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f'Failed to update {resource_type}/{resource_id}: {e}')
        return _operation_outcome('error', 'exception',
                                  'Failed to update resource'), 500

    record_audit_event('update', resource_type, resource_id,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id)

    fhir_json = resource.to_fhir_json()
    fhir_json = add_disclaimer(fhir_json, resource_type)
    response = jsonify(fhir_json)
    response.headers['ETag'] = f'W/"{resource.version_id}"'
    return response


# Query keys are untrusted input too. Only these locally defined semantic
# aliases may be named in responses or audit evidence; every other unsupported
# key gets a generic corrective message.
_SAFE_UNSUPPORTED_SEARCH_KEYS = frozenset({'date', 'datetime'})
_SAFE_MODIFIER_TOKENS = frozenset({
    'above', 'below', 'contains', 'exact', 'identifier', 'in', 'iterate',
    'missing', 'not', 'not-in', 'of-type', 'text', 'type',
    # Fixed synthetic token used by the public issue contract.
    'frobnicate',
})


def _safe_unsupported_key(key):
    if key in _SAFE_UNSUPPORTED_SEARCH_KEYS:
        return key
    if ':' in key:
        base, modifier = key.split(':', 1)
        if (base in _SUPPORTED_SEARCH_PARAMS
                and modifier in _SAFE_MODIFIER_TOKENS):
            return key
    return None


def _unsupported_input_text(kind, key):
    safe_key = _safe_unsupported_key(key)
    return f'{kind}: {safe_key}' if safe_key else kind


def _error_fidelity_outcome(severity, code, text):
    """Build an OperationOutcome shaped for the error-fidelity contract.

    Unlike _operation_outcome (which uses `diagnostics`), the failure-path
    contract requires `details.text` and an issue carrying nothing else, so a
    consuming agent gets a machine-checkable, corrective message.
    """
    return {
        'resourceType': 'OperationOutcome',
        'issue': [{'severity': severity, 'code': code,
                   'details': {'text': text}}],
    }


def _lenient_search_warning_entries(ignored_params, supported_params_text):
    """Build bounded, value-free warnings for ignored local search keys."""
    safe_ignored = sorted({key for key in ignored_params
                           if _safe_unsupported_key(key)})
    has_unnamed = any(not _safe_unsupported_key(key)
                      for key in ignored_params)
    warning_keys = [*safe_ignored]
    if has_unnamed:
        warning_keys.append(None)

    entries = []
    for ignored in warning_keys:
        ignored_text = ('Unknown parameter' if ignored is None else
                        f'Unknown parameter: {ignored}')
        entries.append({
            'search': {'mode': 'outcome'},
            'resource': _error_fidelity_outcome(
                'warning', 'not-supported',
                f'{ignored_text}. '
                f'Supported parameters: {supported_params_text}.',
            ),
        })
    return entries, safe_ignored, has_unnamed


def _reject_local_search(resource_type, agent_id, tenant_id, code, message):
    """Return and audit a static, value-free local-search rejection."""
    audit_detail = {
        'invalid': 'search rejected: invalid parameter',
        'not-supported': 'search rejected: unsupported parameter',
    }.get(code, 'search rejected')
    record_audit_event('read', resource_type, None, agent_id=agent_id,
                       tenant_id=tenant_id, outcome='failure',
                       detail=audit_detail)
    return jsonify(_error_fidelity_outcome(
        'error', code, message)), 400


def _parse_prefer_handling():
    """Return 'strict' or 'lenient' (default) from the Prefer header."""
    prefer = request.headers.get('Prefer', '') or ''
    for item in parse_list_header(prefer):
        preference, _parameters = parse_options_header(item)
        name, separator, raw_value = preference.partition('=')
        if name.strip().lower() != 'handling':
            continue
        # RFC 7240 applies only the first occurrence of a preference. An
        # absent or unsupported value therefore stays at our lenient default;
        # a later duplicate must not override it.
        if not separator:
            return 'lenient'
        value = unquote_header_value(raw_value.strip()).lower()
        return value if value in ('strict', 'lenient') else 'lenient'
    return 'lenient'


@r6_blueprint.route('/<resource_type>', methods=['GET'])
def search_resources(resource_type):
    """
    Search R6 FHIR resources.

    Supported parameters:
      - patient: Reference filter (Patient/{id}) — matches subject.reference
      - code: Code filter — matches code.coding[].code in the JSON
      - status: Status filter — matches the status field
      - _lastUpdated: Date filter (ge/le prefix) on last_updated column
      - _count: Max results (1-200, default 50)
      - _sort: Sort by _lastUpdated or -_lastUpdated (desc)
      - _summary: 'count' returns total only
      - context-id: Filter to resources in a specific context envelope
    """
    # Delegate AuditEvent searches to the dedicated handler
    if resource_type == 'AuditEvent':
        return search_audit_events()

    if not R6Resource.is_supported_type(resource_type):
        return jsonify(_error_fidelity_outcome(
            'error', 'not-supported',
            'Resource type is not supported.')), 400

    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id

    # --- Upstream proxy mode: forward search to real FHIR server ---
    proxy = get_proxy_for_request()
    if proxy:
        # Forward all query params to upstream (patient, code, status, _count, etc.)
        params = dict(request.args)
        # Remove context-id (local concept, not upstream)
        params.pop('context-id', None)
        bundle, upstream_status = proxy.search(resource_type, params)
        if upstream_status != 200:
            # Surface the (sanitized) upstream rejection with its real status —
            # a failed search must not be reported as an empty result set —
            # and audit it as a failure, not a zero-result success.
            record_audit_event('read', resource_type, None,
                               agent_id=request.headers.get('X-Agent-Id'),
                               tenant_id=tenant_id,
                               outcome='failure',
                               detail=f'search (upstream): rejected HTTP {upstream_status}')
            return jsonify(bundle), upstream_status

        # Apply guardrails to each entry from upstream. Tolerate a malformed
        # bundle (null/non-list entry, non-dict entries or resources) rather
        # than 500 — the upstream is not fully trusted.
        entries = []
        bundle_entries = bundle.get('entry')
        if not isinstance(bundle_entries, list):
            bundle_entries = []
        for entry in bundle_entries:
            if not isinstance(entry, dict):
                continue
            resource_data = entry.get('resource')
            if not isinstance(resource_data, dict):
                continue
            if resource_data.get('resourceType') == 'OperationOutcome':
                # Warning entries (search.mode="outcome") carry free-text
                # issue fields apply_redaction() does not inspect — run them
                # through the same allowlist as upstream errors.
                redacted = sanitize_operation_outcome_resource(resource_data)
            else:
                redacted = apply_redaction(resource_data)
                redacted = add_disclaimer(redacted, resource_type)
            redacted['_source'] = 'upstream'
            new_entry = {
                'fullUrl': entry.get('fullUrl', ''),
                'resource': redacted,
            }
            # Preserve entry.search so mode="outcome" warnings survive — but
            # allowlist it to scalar mode/score: `search` can carry
            # extensions, and even under mode/score keys a hostile upstream
            # could nest an object holding PHI or internal URLs.
            search_info = entry.get('search')
            if isinstance(search_info, dict):
                allowed = {}
                mode = search_info.get('mode')
                if mode in ('match', 'include', 'outcome'):
                    allowed['mode'] = mode
                score = search_info.get('score')
                if isinstance(score, (int, float)) and not isinstance(score, bool):
                    allowed['score'] = score
                if allowed:
                    new_entry['search'] = allowed
            entries.append(new_entry)

        result = {
            'resourceType': 'Bundle',
            'type': 'searchset',
            'total': bundle.get('total', len(entries)),
            'link': bundle.get('link', []),
            'entry': entries,
            '_source': 'upstream',
        }

        record_audit_event('read', resource_type, None,
                           agent_id=request.headers.get('X-Agent-Id'),
                           tenant_id=tenant_id,
                           detail=f'search (upstream): {len(entries)} results')

        return jsonify(result)

    # --- Error fidelity: tell the agent the truth about unsupported inputs ---
    handling = _parse_prefer_handling()
    agent_id = request.headers.get('X-Agent-Id')
    modifier_keys = [k for k in request.args if ':' in k]
    ignored_params = [k for k in request.args
                      if ':' not in k and k not in _SUPPORTED_SEARCH_PARAMS]

    # An unsupported search modifier is always rejected (none are implemented):
    # silently dropping it would change the query's meaning unbeknownst to the
    # caller. Audited as a failure, in every handling mode.
    if modifier_keys:
        modifier = sorted(modifier_keys)[0]
        modifier_text = _unsupported_input_text('Unsupported modifier', modifier)
        return _reject_local_search(
            resource_type, agent_id, tenant_id, 'not-supported',
            f'{modifier_text}. '
            f'Supported parameters: {_SUPPORTED_PARAMS_TEXT}.',
        )

    # An unknown parameter under strict handling is rejected; under lenient
    # handling (the default) it is ignored but reported — a warning entry in
    # the bundle, an audit note, and kept out of the self link — never
    # silently swallowed.
    if ignored_params and handling == 'strict':
        unknown = sorted(ignored_params)[0]
        unknown_text = _unsupported_input_text('Unknown parameter', unknown)
        return _reject_local_search(
            resource_type, agent_id, tenant_id, 'not-supported',
            f'{unknown_text}. '
            f'Supported parameters: {_SUPPORTED_PARAMS_TEXT}.',
        )

    repeated_control = next((
        spec['name'] for spec in _SEARCH_PARAMETER_SPECS
        if len(request.args.getlist(spec['name'])) > 1
    ), None)
    if repeated_control:
        return _reject_local_search(
            resource_type, agent_id, tenant_id, 'invalid',
            f'Repeated {repeated_control} parameters are not supported.',
        )

    # Supported controls with invalid values are failures, not permission to
    # silently substitute defaults. Messages and audit notes name only the
    # locally defined control, never the submitted value.
    invalid_control = None
    count_param = request.args.get('_count')
    if (count_param is not None
            and (len(count_param) > 10
                 or not re.fullmatch(r'[0-9]+', count_param))):
        invalid_control = ('_count', '_count must be a non-negative integer.')
    sort_control = request.args.get('_sort')
    if (invalid_control is None and sort_control is not None
            and sort_control not in ('_lastUpdated', '-_lastUpdated')):
        invalid_control = (
            '_sort', '_sort must be _lastUpdated or -_lastUpdated.')
    summary_control = request.args.get('_summary')
    if (invalid_control is None and summary_control is not None
            and summary_control != 'count'):
        invalid_control = ('_summary', '_summary only supports count.')
    if invalid_control:
        _, message = invalid_control
        return _reject_local_search(
            resource_type, agent_id, tenant_id, 'invalid', message,
        )

    # --- Local mode: query SQLite ---
    query = R6Resource.query.filter_by(
        resource_type=resource_type, is_deleted=False, tenant_id=tenant_id
    )

    # --- patient reference filter ---
    patient_ref = request.args.get('patient')
    if patient_ref:
        if not _PATIENT_REF_PATTERN.fullmatch(patient_ref):
            return _reject_local_search(
                resource_type, agent_id, tenant_id, 'invalid',
                'Patient reference must match Patient/{id}.',
            )
        query = query.filter(
            db.or_(
                R6Resource.resource_json.contains(f'"reference":"{patient_ref}"'),
                R6Resource.resource_json.contains(f'"reference": "{patient_ref}"'),
            )
        )

    # --- code filter (matches code.coding[].code in JSON) ---
    code_param = request.args.get('code')
    if code_param:
        # Match "code":"<value>" inside the JSON — works for coding arrays
        query = query.filter(
            R6Resource.resource_json.contains(
                f'"code":"{code_param}"', autoescape=True)
        )

    # --- status filter (matches "status":"<value>" in JSON) ---
    status_param = request.args.get('status')
    if status_param:
        query = query.filter(
            R6Resource.resource_json.contains(
                f'"status":"{status_param}"', autoescape=True)
        )

    # --- _lastUpdated filter (ge/le prefix on DB column) ---
    last_updated_param = request.args.get('_lastUpdated')
    if last_updated_param:
        try:
            if last_updated_param.startswith('ge'):
                dt = datetime.fromisoformat(last_updated_param[2:].replace('Z', '+00:00'))
                query = query.filter(R6Resource.last_updated >= dt)
            elif last_updated_param.startswith('le'):
                dt = datetime.fromisoformat(last_updated_param[2:].replace('Z', '+00:00'))
                query = query.filter(R6Resource.last_updated <= dt)
            elif last_updated_param.startswith('gt'):
                dt = datetime.fromisoformat(last_updated_param[2:].replace('Z', '+00:00'))
                query = query.filter(R6Resource.last_updated > dt)
            elif last_updated_param.startswith('lt'):
                dt = datetime.fromisoformat(last_updated_param[2:].replace('Z', '+00:00'))
                query = query.filter(R6Resource.last_updated < dt)
            else:
                # Exact match (to the second)
                dt = datetime.fromisoformat(last_updated_param.replace('Z', '+00:00'))
                query = query.filter(R6Resource.last_updated >= dt)
        except (ValueError, TypeError):
            return _reject_local_search(
                resource_type, agent_id, tenant_id, 'invalid',
                '_lastUpdated must be a valid ISO datetime with optional '
                'ge/le/gt/lt prefix.',
            )

    # --- context-id filter (restrict to resources in a context envelope) ---
    context_id = request.args.get('context-id')
    if context_id:
        if not _FHIR_ID_PATTERN.fullmatch(context_id):
            return _reject_local_search(
                resource_type, agent_id, tenant_id, 'invalid',
                'context-id must be a valid FHIR id.',
            )
        from r6.models import ContextItem
        context_refs = [item.resource_ref for item in
                        ContextItem.query.filter_by(context_id=context_id).all()]
        # resource_ref is like "Patient/abc-123"
        context_ids = [ref.split('/')[-1] for ref in context_refs
                       if ref.startswith(f'{resource_type}/')]
        if context_ids:
            query = query.filter(R6Resource.id.in_(context_ids))
        else:
            # No matching resources in context — return empty
            query = query.filter(db.literal(False))

    # --- _sort ---
    sort_param = request.args.get('_sort', '-_lastUpdated')
    if sort_param == '_lastUpdated':
        query = query.order_by(R6Resource.last_updated.asc())
    else:
        query = query.order_by(R6Resource.last_updated.desc())

    # Support _summary=count without bypassing response finalization. Count
    # summaries omit matching resources, but still need a truthful self link,
    # lenient warning evidence, and an AuditEvent.
    summary = request.args.get('_summary')
    summary_count = summary == 'count' or count_param == '0'
    total = query.order_by(None).count()
    if summary_count:
        resources = []
    else:
        # Clamp _count to [1, 200]
        count = int(count_param) if count_param is not None else 50
        count = max(1, min(count, 200))
        resources = query.limit(count).all()

    # Apply redaction and disclaimer on all search results
    entries = []
    for r in resources:
        fhir_json = apply_redaction(r.to_fhir_json())
        fhir_json = add_disclaimer(fhir_json, resource_type)
        entries.append({
            'fullUrl': f'{request.host_url.rstrip("/")}/r6/fhir/{resource_type}/{r.id}',
            'resource': fhir_json
        })

    # Build self link with search params for transparency
    search_params = []
    for spec in _SEARCH_PARAMETER_SPECS:
        key = spec['name']
        val = request.args.get(key)
        if val:
            search_params.append((key, val))
    self_link = f'{request.host_url.rstrip("/")}/r6/fhir/{resource_type}'
    if search_params:
        self_link += '?' + urlencode(search_params)

    # Lenient handling of an unknown parameter: append a corrective warning
    # entry (search.mode=outcome) naming the ignored parameter + the supported
    # set. The self link already omits unknown params (built from the supported
    # set above), so the caller can see exactly which query actually ran.
    audit_note = ''
    if ignored_params:
        # The allowlist has two unknown semantic aliases, plus at most one
        # generic warning for every other key. This caps attacker-controlled
        # warning growth at three entries regardless of query size.
        warning_entries, safe_ignored, has_unnamed = (
            _lenient_search_warning_entries(
                ignored_params, _SUPPORTED_PARAMS_TEXT))
        entries.extend(warning_entries)

        ignored_count = len(ignored_params)
        audit_note = ('; unsupported search parameter ignored'
                      if ignored_count == 1 else
                      f'; {ignored_count} unsupported search parameters ignored')

    bundle = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'total': total,
        'link': [{'relation': 'self', 'url': self_link}],
    }
    if entries:
        bundle['entry'] = entries

    record_audit_event('read', resource_type, None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       context_id=context_id,
                       tenant_id=tenant_id,
                       detail=f'search: {total} results{audit_note}',
                       outcome_detail_code=(
                           AuditEventRecord.ignored_parameters_outcome_code(
                               safe_ignored, has_unnamed)
                           if ignored_params else None))

    return jsonify(bundle)


# --- $validate Operation ---

@r6_blueprint.route('/<resource_type>/$validate', methods=['POST'])
def validate_resource(resource_type):
    """
    Validate a proposed FHIR R6 resource.
    Returns an OperationOutcome.
    """
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome('error', 'not-supported',
                                  f'Resource type {resource_type} is not supported'), 400

    body = request.get_json(silent=True)
    if not body:
        return _operation_outcome('error', 'invalid', 'Request body must be valid JSON'), 400

    mode = request.args.get('mode', 'no-action')
    profile = request.args.get('profile')

    result = validator.validate_resource(body, mode=mode, profile=profile)

    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
    record_audit_event('validate', resource_type, body.get('id'),
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'mode={mode}, valid={result["valid"]}')

    status_code = 200 if result['valid'] else 422
    return jsonify(result['operation_outcome']), status_code


# --- Bundle Ingestion + Context Builder ---

@r6_blueprint.route('/Bundle/$ingest-context', methods=['POST'])
def ingest_context():
    """
    Accept a small Bundle, store resources, and build a context envelope.
    """
    # Parsed before the step-up gate below, and that gate is conditional on
    # READ_AUTH_ENABLED — so no reordering could close this one. The depth
    # bound is the only guard that holds in both branches (#312).
    body, too_deep = json_body_within_depth()
    if too_deep:
        return _operation_outcome('error', 'invalid',
                                  'Request body nesting is too deep'), 400
    if not isinstance(body, dict) or body.get('resourceType') != 'Bundle':
        return _operation_outcome('error', 'invalid',
                                  'Request body must be a FHIR Bundle'), 400

    # Validate Bundle.type
    bundle_type = body.get('type')
    if bundle_type and bundle_type not in _VALID_BUNDLE_TYPES:
        return _operation_outcome('error', 'invalid',
                                  f'Bundle.type "{bundle_type}" is not a valid FHIR Bundle type'), 400

    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id

    # In hardened deployments, bundle ingestion is a write boundary—not a
    # read-shaped convenience operation. Public/demo tenants remain usable in
    # local compatibility mode, while production enables this gate at startup.
    if _read_auth_enabled():
        step_up_token = request.headers.get('X-Step-Up-Token', '').strip()
        valid, _error = validate_step_up_token(
            step_up_token, tenant_id, require_scope='write'
        )
        if not valid:
            record_audit_event(
                'create', 'Bundle', None,
                agent_id=request.headers.get('X-Agent-Id'),
                tenant_id=tenant_id,
                outcome='failure',
                detail='ingest-context authorization rejected',
            )
            return _operation_outcome(
                'error', 'security',
                'Bundle ingestion requires a tenant-bound write token',
            ), 401

    try:
        from r6.fasten.ingester import skipped_type_summary

        result = context_builder.ingest_bundle(body, tenant_id=tenant_id)
        # The types are code-owned names, never the feed's own string — see
        # `safe_skipped_type`. A discard the audit trail cannot name is the
        # #377 silence with a 201 on it.
        types_summary = skipped_type_summary(result['skipped_types'])
        record_audit_event('create', 'Bundle', None,
                           agent_id=request.headers.get('X-Agent-Id'),
                           context_id=result['context_id'],
                           tenant_id=tenant_id,
                           detail=(f'ingested {result["resource_count"]} resources'
                                   + (f'; skipped {result["skipped_count"]}: '
                                      f'{types_summary}'
                                      if types_summary else '')))
        return jsonify(result), 201
    except ValueError as e:
        return _operation_outcome('error', 'invalid', str(e)), 400
    except Exception as exc:
        db.session.rollback()
        logger.error('Failed to ingest bundle: %s', type(exc).__name__)
        return _operation_outcome('error', 'exception',
                                  'Failed to ingest bundle'), 500


@r6_blueprint.route('/context/<context_id>', methods=['GET'])
def get_context(context_id):
    """
    Retrieve a context envelope by ID.

    The context envelope includes:
    - Metadata (patient ref, encounter ref, temporal window, expiry)
    - List of resource references included in this context
    - Redaction profile applied
    - Consent decision (currently always 'permit')

    If ?_include=resources is passed, the actual resource data is included
    (redacted, filtered to context membership only).
    """
    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
    envelope = ContextEnvelope.query.filter_by(
        context_id=context_id, tenant_id=tenant_id
    ).first()
    if not envelope:
        return _operation_outcome('error', 'not-found',
                                  f'Context {context_id} not found'), 404

    # Check expiry (handle both naive and aware datetimes from DB)
    now = datetime.now(timezone.utc)
    expires = envelope.expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires and expires < now:
        return _operation_outcome('error', 'expired',
                                  f'Context {context_id} has expired'), 410

    result = envelope.to_dict()

    # If _include=resources, fetch and include actual resource data (redacted)
    include = request.args.get('_include')
    if include == 'resources':
        items = ContextItem.query.filter_by(context_id=context_id).all()
        resources = []
        for item in items:
            parts = item.resource_ref.split('/', 1)
            if len(parts) == 2:
                r_type, r_id = parts
                r = R6Resource.query.filter_by(
                    id=r_id, resource_type=r_type, tenant_id=tenant_id, is_deleted=False
                ).first()
                if r:
                    fhir_json = apply_redaction(r.to_fhir_json())
                    fhir_json = add_disclaimer(fhir_json, r_type)
                    resources.append(fhir_json)
        result['resources'] = resources
        result['_note'] = ('Resources are redacted per the context redaction profile. '
                           'Only resources belonging to this context are included.')

    record_audit_event('read', 'ContextEnvelope', context_id,
                       agent_id=request.headers.get('X-Agent-Id'),
                       context_id=context_id,
                       tenant_id=tenant_id)

    return jsonify(result)


# --- AuditEvent Endpoints ---

@r6_blueprint.route('/AuditEvent', methods=['GET'])
def search_audit_events():
    """Search AuditEvent records, optionally filtered by context-id."""
    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
    agent_id = request.headers.get('X-Agent-Id')
    modifier_keys = [key for key in request.args if ':' in key]
    ignored_params = [key for key in request.args
                      if ':' not in key and key not in _AUDIT_SEARCH_PARAMS]

    if modifier_keys:
        modifier = sorted(modifier_keys)[0]
        modifier_text = _unsupported_input_text(
            'Unsupported modifier', modifier)
        return _reject_local_search(
            'AuditEvent', agent_id, tenant_id, 'not-supported',
            f'{modifier_text}. Supported parameters: {_AUDIT_PARAMS_TEXT}.',
        )

    if ignored_params and _parse_prefer_handling() == 'strict':
        unknown = sorted(ignored_params)[0]
        unknown_text = _unsupported_input_text('Unknown parameter', unknown)
        return _reject_local_search(
            'AuditEvent', agent_id, tenant_id, 'not-supported',
            f'{unknown_text}. Supported parameters: {_AUDIT_PARAMS_TEXT}.',
        )

    repeated_control = next((
        spec['name'] for spec in _AUDIT_SEARCH_PARAMETER_SPECS
        if len(request.args.getlist(spec['name'])) > 1
    ), None)
    if repeated_control:
        return _reject_local_search(
            'AuditEvent', agent_id, tenant_id, 'invalid',
            f'Repeated {repeated_control} parameters are not supported.',
        )

    count_param = request.args.get('_count')
    if (count_param is not None
            and (len(count_param) > 10
                 or not re.fullmatch(r'[0-9]+', count_param))):
        return _reject_local_search(
            'AuditEvent', agent_id, tenant_id, 'invalid',
            '_count must be a non-negative integer.',
        )

    context_id = request.args.get('context-id')
    if context_id and not _FHIR_ID_PATTERN.fullmatch(context_id):
        return _reject_local_search(
            'AuditEvent', agent_id, tenant_id, 'invalid',
            'context-id must be a valid FHIR id.',
        )
    resource_type = request.args.get('entity-type')
    count = int(count_param) if count_param is not None else 50
    count = max(1, min(count, 200))

    # Enforce tenant isolation on audit events
    query = AuditEventRecord.query.filter_by(
        tenant_id=tenant_id
    ).order_by(AuditEventRecord.recorded.desc())

    if context_id:
        query = query.filter_by(context_id=context_id)
    if resource_type:
        query = query.filter_by(resource_type=resource_type)

    total = query.order_by(None).count()
    events = [] if count_param == '0' else query.limit(count).all()
    entries = [
        {
            'fullUrl': (
                f'{request.host_url.rstrip("/")}/r6/fhir/AuditEvent/{event.id}'
            ),
            'resource': event.to_fhir_json(),
        }
        for event in events
    ]

    safe_ignored = []
    has_unnamed = False
    audit_note = ''
    if ignored_params:
        warning_entries, safe_ignored, has_unnamed = (
            _lenient_search_warning_entries(
                ignored_params, _AUDIT_PARAMS_TEXT))
        entries.extend(warning_entries)

        ignored_count = len(ignored_params)
        audit_note = ('; unsupported search parameter ignored'
                      if ignored_count == 1 else
                      f'; {ignored_count} unsupported search parameters ignored')

    search_params = []
    for spec in _AUDIT_SEARCH_PARAMETER_SPECS:
        value = request.args.get(spec['name'])
        if value:
            search_params.append((spec['name'], value))
    self_link = f'{request.host_url.rstrip("/")}/r6/fhir/AuditEvent'
    if search_params:
        self_link += '?' + urlencode(search_params)

    bundle = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'total': total,
        'link': [{'relation': 'self', 'url': self_link}],
    }
    if entries:
        bundle['entry'] = entries

    record_audit_event(
        'read', 'AuditEvent', None, agent_id=agent_id,
        tenant_id=tenant_id, detail=f'search: {total} results{audit_note}',
        outcome_detail_code=(
            AuditEventRecord.ignored_parameters_outcome_code(
                safe_ignored, has_unnamed)
            if ignored_params else None),
    )

    return jsonify(bundle)


# --- Cross-Version Import Stub ---

@r6_blueprint.route('/$import-stub', methods=['POST'])
def import_stub():
    """
    R4/R5 import stub: accept Bundle + annotate "needs transform".
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get('resourceType') != 'Bundle':
        return _operation_outcome('error', 'invalid',
                                  'Request body must be a FHIR Bundle'), 400

    source_version = request.args.get('source-version', 'R4')
    entries = body.get('entry', [])
    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id

    result = {
        'resourceType': 'OperationOutcome',
        'issue': [
            {
                'severity': 'information',
                'code': 'informational',
                'diagnostics': (
                    f'Import stub received Bundle with {len(entries)} entries '
                    f'from {source_version}. Cross-version transforms for R6 ballot '
                    f'are not consistently updated. Each resource is annotated as '
                    f'"needs-transform" for pipeline processing.'
                )
            }
        ],
        '_import_stub': {
            'status': 'accepted',
            'source_version': source_version,
            'target_version': R6_FHIR_VERSION,
            'entry_count': len(entries),
            'entries': [
                {
                    'resource_type': entry.get('resource', {}).get('resourceType', 'Unknown'),
                    'resource_id': entry.get('resource', {}).get('id'),
                    'transform_status': 'needs-transform',
                    'warning': 'R6 ballot cross-version transforms are not production-ready'
                }
                for entry in entries
            ]
        }
    }

    record_audit_event('create', 'Bundle', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'import-stub from {source_version}, {len(entries)} entries')

    return jsonify(result), 202


# --- Observation $stats Operation (standard FHIR, available since R4) ---

@r6_blueprint.route('/Observation/$stats', methods=['GET'])
def observation_stats():
    """
    Observation $stats — compute statistics over stored Observations.

    Standard FHIR operation (available since R4, not R6-specific).
    Computes count, min, max, mean over numeric valueQuantity values.
    Limitations: only supports valueQuantity (not valueCodeableConcept,
    valueString, etc.). No percentile or median. No component support.
    """
    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
    code = request.args.get('code')
    patient_ref = request.args.get('patient')

    query = R6Resource.query.filter_by(
        resource_type='Observation', is_deleted=False, tenant_id=tenant_id
    )

    if patient_ref:
        if not _PATIENT_REF_PATTERN.match(patient_ref):
            return _operation_outcome('error', 'invalid',
                                      'Patient reference must match Patient/{id}'), 400
        query = query.filter(
            db.or_(
                R6Resource.resource_json.contains(f'"reference":"{patient_ref}"'),
                R6Resource.resource_json.contains(f'"reference": "{patient_ref}"'),
            )
        )

    observations = query.all()

    # Extract numeric values matching the code filter
    values = []
    for obs in observations:
        resource = json.loads(obs.resource_json)
        # Filter by code if specified
        if code:
            obs_codings = resource.get('code', {}).get('coding', [])
            if not any(c.get('code') == code for c in obs_codings):
                continue
        # Extract valueQuantity.value
        vq = resource.get('valueQuantity', {})
        if isinstance(vq, dict) and 'value' in vq:
            try:
                values.append(float(vq['value']))
            except (ValueError, TypeError):
                pass

    stats = {
        'count': len(values),
        'min': round(min(values), 2) if values else None,
        'max': round(max(values), 2) if values else None,
        'mean': round(sum(values) / len(values), 2) if values else None,
    }

    result = {
        'resourceType': 'Parameters',
        'parameter': [
            {'name': 'count', 'valueInteger': stats['count']},
        ]
    }
    if stats['min'] is not None:
        result['parameter'].extend([
            {'name': 'min', 'valueDecimal': stats['min']},
            {'name': 'max', 'valueDecimal': stats['max']},
            {'name': 'mean', 'valueDecimal': stats['mean']},
        ])

    unit = None
    if values and observations:
        for obs in observations:
            resource = json.loads(obs.resource_json)
            vq = resource.get('valueQuantity', {})
            if vq.get('unit'):
                unit = vq['unit']
                break
    if unit:
        result['parameter'].append({'name': 'unit', 'valueString': unit})

    record_audit_event('read', 'Observation', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'$stats: code={code}, count={stats["count"]}')

    return jsonify(result)


# --- Observation $lastn Operation (standard FHIR, available since R4) ---

@r6_blueprint.route('/Observation/$lastn', methods=['GET'])
def observation_lastn():
    """
    Observation $lastn — get the last N observations per code.

    Standard FHIR operation (available since R4, not R6-specific).
    Returns the most recent observations grouped by code, optionally
    filtered by patient and code. Default N=1.
    """
    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
    code = request.args.get('code')
    patient_ref = request.args.get('patient')
    max_n = request.args.get('max', 1, type=int)
    max_n = max(1, min(max_n, 100))

    query = R6Resource.query.filter_by(
        resource_type='Observation', is_deleted=False, tenant_id=tenant_id
    ).order_by(R6Resource.last_updated.desc())

    if patient_ref:
        if not _PATIENT_REF_PATTERN.match(patient_ref):
            return _operation_outcome('error', 'invalid',
                                      'Patient reference must match Patient/{id}'), 400
        query = query.filter(
            db.or_(
                R6Resource.resource_json.contains(f'"reference":"{patient_ref}"'),
                R6Resource.resource_json.contains(f'"reference": "{patient_ref}"'),
            )
        )

    all_observations = query.all()

    # Group by code and take last N per code
    code_groups = {}
    for obs in all_observations:
        resource = json.loads(obs.resource_json)
        obs_codings = resource.get('code', {}).get('coding', [])
        obs_code = obs_codings[0].get('code') if obs_codings else 'unknown'

        if code and obs_code != code:
            continue

        if obs_code not in code_groups:
            code_groups[obs_code] = []
        if len(code_groups[obs_code]) < max_n:
            code_groups[obs_code].append(obs)

    entries = []
    for code_key, obs_list in code_groups.items():
        for obs in obs_list:
            fhir_json = apply_redaction(obs.to_fhir_json())
            fhir_json = add_disclaimer(fhir_json, 'Observation')
            entries.append({
                'fullUrl': f'{request.host_url.rstrip("/")}/r6/fhir/Observation/{obs.id}',
                'resource': fhir_json
            })

    bundle = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'total': len(entries),
        'entry': entries
    }

    record_audit_event('read', 'Observation', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'$lastn: code={code}, max={max_n}, results={len(entries)}')

    return jsonify(bundle)


# --- SubscriptionTopic Discovery (R6 ballot) ---

@r6_blueprint.route('/SubscriptionTopic/$list', methods=['GET'])
def list_subscription_topics():
    """
    List available SubscriptionTopics for discovery.

    Introduced in R5, maturing in R6. Topics define subscribable events.
    This endpoint supports discovery only — no notification dispatch.
    """
    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id

    # Query stored SubscriptionTopics for this tenant
    topics = R6Resource.query.filter_by(
        resource_type='SubscriptionTopic', is_deleted=False, tenant_id=tenant_id
    ).all()

    entries = []
    for t in topics:
        fhir_json = t.to_fhir_json()
        entries.append({
            'fullUrl': f'{request.host_url.rstrip("/")}/r6/fhir/SubscriptionTopic/{t.id}',
            'resource': fhir_json
        })

    bundle = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'total': len(entries),
        'entry': entries
    }

    record_audit_event('read', 'SubscriptionTopic', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'$list: {len(entries)} topics found')

    return jsonify(bundle)


# --- Permission $evaluate (R6 Access Control) ---

@r6_blueprint.route('/Permission/$evaluate', methods=['POST'])
def evaluate_permission():
    """
    Evaluate a Permission request against stored Permission resources.

    This is the R6 access control evaluation endpoint. Given a subject,
    action, and resource, returns whether the action is permitted or denied
    based on stored Permission resources.
    """
    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
    body = request.get_json(silent=True)
    if not body:
        return _operation_outcome('error', 'invalid', 'Request body must be valid JSON'), 400

    subject_ref = body.get('subject')
    action = body.get('action', 'read')
    body.get('resource')

    # Query active Permission resources for this tenant
    permissions = R6Resource.query.filter_by(
        resource_type='Permission', is_deleted=False, tenant_id=tenant_id
    ).all()

    # Evaluate: find matching rules
    decision = 'deny'  # Default deny
    matched_rules = []

    for perm in permissions:
        perm_data = json.loads(perm.resource_json)
        if perm_data.get('status') != 'active':
            continue

        combining = perm_data.get('combining', 'deny-overrides')
        rules = perm_data.get('rule', [])

        for rule in rules:
            rule_type = rule.get('type', 'deny')

            # Check if rule matches the requested action
            activities = rule.get('activity', [])
            action_match = not activities  # Empty means match all
            for activity in activities:
                act_actions = activity.get('action', [])
                if not act_actions:
                    action_match = True
                    break
                # Actions may be CodeableConcept with coding array, or plain code
                for a in act_actions:
                    if a.get('code') == action:
                        action_match = True
                        break
                    # Check inside coding array (CodeableConcept pattern)
                    for coding in a.get('coding', []):
                        if coding.get('code') == action:
                            action_match = True
                            break
                if action_match:
                    break

            if action_match:
                matched_rules.append({
                    'permission_id': perm.id,
                    'rule_type': rule_type,
                    'combining': combining,
                })

                if rule_type == 'permit':
                    decision = 'permit'

    # Build reasoning explanation for the decision
    if not permissions:
        reasoning = 'No active Permission resources found for this tenant. Default deny applies.'
    elif not matched_rules:
        reasoning = (f'Found {len(permissions)} Permission resource(s) but no rules matched '
                     f'action "{action}". Default deny applies.')
    else:
        rule_descs = []
        for mr in matched_rules:
            rule_descs.append(f'{mr["rule_type"]} (Permission/{mr["permission_id"]}, combining={mr["combining"]})')
        reasoning = (f'Matched {len(matched_rules)} rule(s): {"; ".join(rule_descs)}. '
                     f'Final decision: {decision}.')

    result = {
        'resourceType': 'Parameters',
        'parameter': [
            {'name': 'decision', 'valueCode': decision},
            {'name': 'matched_rules', 'valueInteger': len(matched_rules)},
            {'name': 'subject', 'valueString': subject_ref or 'unspecified'},
            {'name': 'action', 'valueCode': action},
            {'name': 'reasoning', 'valueString': reasoning},
        ]
    }

    record_audit_event('read', 'Permission', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'$evaluate: subject={subject_ref}, action={action}, decision={decision}')

    return jsonify(result)


# --- De-identification Endpoint ---

@r6_blueprint.route('/<resource_type>/<resource_id>/$deidentify', methods=['GET'])
def deidentify_endpoint(resource_type, resource_id):
    """Return a conservative de-identification preview of a resource."""
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome('error', 'not-supported',
                                  f'Resource type {resource_type} is not supported'), 400

    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
    resource = R6Resource.query.filter_by(
        id=resource_id, resource_type=resource_type,
        is_deleted=False, tenant_id=tenant_id
    ).first()

    if not resource:
        return _operation_outcome('error', 'not-found',
                                  f'{resource_type}/{resource_id} not found'), 404

    record_audit_event('read', resource_type, resource_id,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail='de-identification export')

    fhir_json = resource.to_fhir_json()
    mode = request.args.get('mode', 'deidentified-preview')

    if mode == 'patient-controlled':
        patient_id = request.args.get('patient_id', resource_id)
        deidentified = apply_patient_controlled_redaction(
            fhir_json, patient_id
        )
    else:
        deidentified = deidentify_resource(fhir_json)

    return jsonify(deidentified)


# --- Audit Trail Export ---

@r6_blueprint.route('/AuditEvent/$export', methods=['GET'])
def export_audit():
    """
    Export audit trail in NDJSON or FHIR Bundle format.
    Supports date range filtering.
    """
    fmt = request.args.get('_format', 'ndjson')
    context_id = request.args.get('context-id')
    count = request.args.get('_count', 1000, type=int)
    count = max(1, min(count, 10000))
    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id

    # Enforce tenant isolation on audit export
    query = AuditEventRecord.query.filter_by(
        tenant_id=tenant_id
    ).order_by(AuditEventRecord.recorded.desc())

    if context_id:
        query = query.filter_by(context_id=context_id)

    resource_type_filter = request.args.get('entity-type')
    if resource_type_filter:
        query = query.filter_by(resource_type=resource_type_filter)

    records = query.limit(count).all()

    record_audit_event('read', 'AuditEvent', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'audit export: {len(records)} records, format={fmt}')

    content = export_audit_trail(records, format=fmt)

    if fmt == 'fhir-bundle':
        return Response(content, mimetype='application/fhir+json')
    else:
        return Response(content, mimetype='application/x-ndjson',
                       headers={'Content-Disposition': 'attachment; filename=audit-trail.ndjson'})


# --- Privacy Policy & Disclaimer Endpoint ---

@r6_blueprint.route('/docs/privacy-policy', methods=['GET'])
def privacy_policy():
    """Return the privacy policy and medical disclaimer."""
    return jsonify({
        'title': 'FHIR R6 MCP Privacy Policy & Medical Disclaimer',
        'effective_date': '2026-02-19',
        'medical_disclaimer': MEDICAL_DISCLAIMER,
        'data_collection': {
            'what_we_collect': [
                'FHIR resource data submitted via API (stored with PHI redaction)',
                'Audit trail of all resource access (append-only)',
                'Tenant identifiers and agent identifiers',
                'OAuth client registration metadata',
            ],
            'what_we_do_not_collect': [
                'User browsing behavior or analytics',
                'Device fingerprints',
                'Location data beyond what is in FHIR resources',
            ],
        },
        'data_protection': {
            'redaction': 'PHI redaction applied on all read paths (identifiers, addresses, telecom)',
            'de_identification': (
                'Conservative de-identification preview available via '
                '$deidentify; expert review is required before disclosure'
            ),
            'encryption': 'TLS required for all production deployments',
            'audit_trail': 'Immutable, append-only AuditEvent records for all operations',
            'tenant_isolation': 'Mandatory tenant-scoped data isolation on all queries',
        },
        'data_retention': {
            'context_envelopes': 'Default TTL 30 minutes (configurable)',
            'fhir_resources': 'Retained until explicitly deleted',
            'audit_events': 'Retained indefinitely (compliance requirement)',
        },
        'data_sharing': {
            'policy': 'FHIR data is never shared with third parties',
            'ai_training': 'Data is never used for AI model training',
            'advertising': 'Data is never used for advertising',
        },
        'compliance': {
            'hipaa': 'BAA-ready architecture with zero-retention API option',
            'smart_on_fhir': 'SMART App Launch v2 compliant OAuth scopes',
            'fhir_version': 'R6 v6.0.0-ballot3',
        },
        'contact': {
            'support': 'https://github.com/aks129/fhir-mcp-guardrails/issues',
            'maintainer': 'HealthClaw',
            'website': 'https://healthclaw.io',
        },
    })


# --- Health Check ---

@r6_blueprint.route('/health', methods=['GET'])
def health_check():
    """
    Health check endpoint for container orchestration (liveness/readiness).
    Returns 200 if the service is operational, 503 if degraded.
    """
    health = {
        'status': 'healthy',
        'version': __version__,
        'fhirVersion': R6_FHIR_VERSION,
        'mode': 'upstream' if is_proxy_enabled() else 'local',
        'checks': {}
    }

    # Check database connectivity
    try:
        db.session.execute(db.text('SELECT 1'))
        health['checks']['database'] = 'ok'
    except Exception as e:
        health['status'] = 'degraded'
        health['checks']['database'] = 'error'
        logger.warning(f'Health check: database failed: {e}')

    # Check upstream FHIR server connectivity. Three states, incl. an upstream
    # that was named and could not be built — see r6.fhir_proxy.upstream_status.
    upstream = upstream_status()
    health['checks']['upstream'] = upstream['check']
    if upstream['degraded']:
        health['status'] = 'degraded'

    status_code = 200 if health['status'] == 'healthy' else 503
    return jsonify(health), status_code


# --- Internal Endpoints (dashboard support) ---

def _internal_mint_authorized(tenant_id):
    """Fail-closed authorization for internal mint/seed of `tenant_id`.

    Minting a step-up token (or seeding) for a NON-public tenant is a read-auth
    bypass — anyone could mint a tenant-bound read+write token. Rules:

    - Public/synthetic tenants are always allowed. They bypass read-auth anyway,
      and the browser demo dashboard + telemetry mint desktop-demo tokens where
      no secret can be held.
    - Non-public tenants require a matching `X-Internal-Secret` (constant-time).
    - Fail-closed: if `INTERNAL_TOKEN_MINT_SECRET` is unset, non-public mints are
      REFUSED in production and allowed only in dev (backward compatible locally).
    """
    from r6.command_center.access import is_public
    if is_public(tenant_id):
        return True
    mint_secret = os.environ.get('INTERNAL_TOKEN_MINT_SECRET')
    if mint_secret:
        provided = request.headers.get('X-Internal-Secret', '')
        return hmac.compare_digest(provided, mint_secret)
    # Secret unset → open only outside production.
    return resolve_app_env() != 'production'


def _internal_ingest_authorized(tenant_id):
    """Fail-closed authorization for internal FHIR-bundle ingestion (#267).

    Deliberately NOT `_internal_mint_authorized`. That helper exempts public
    tenants because minting a token for a public tenant grants nothing extra
    — they already bypass read-auth by design. Ingestion is different: it
    lets the caller choose what the tenant's records SAY. For `desktop-demo`
    that means an unauthenticated caller could author content that lands in
    an LLM context (stored prompt injection) and forge resource types the
    caller should never be able to write — the reason `_ingest_one` below
    also gets an explicit type allowlist. So every tenant, public or not,
    requires a matching `X-Internal-Secret`. Fail-closed: if
    `INTERNAL_TOKEN_MINT_SECRET` is unset, ingestion is refused in production
    and allowed only outside production (backward compatible locally).

    The internal-secret check lives in `r6.internal_auth` so this gate and the
    `/r6/ops/*` operator gate (#304) share one implementation; `tenant_id` is
    accepted for call-site symmetry but intentionally unused (the secret is
    tenant-independent).
    """
    from r6.internal_auth import internal_secret_authorized
    return internal_secret_authorized()


# Resource types the direct-upload path (#227) may never author, regardless
# of what the caller's bundle claims. AuditEvent is system-managed elsewhere
# in this file (_SYSTEM_MANAGED_TYPES); the rest are trust/provenance/
# envelope types whose presence in an uploaded bundle is either meaningless
# (Bundle-in-Bundle) or actively dangerous (a forged Consent or Provenance
# record, or a Permission grant) if a caller could author them directly.
_INGEST_BUNDLE_FORBIDDEN_TYPES = frozenset(
    {'AuditEvent', 'Permission', 'Consent', 'Provenance', 'Bundle'})

# Computed, not hand-maintained: everything R6Resource supports, minus the
# forbidden set above. A new SUPPORTED_TYPES entry is allowed by default
# (matching current ingest semantics for Fasten/SHC) unless it is added to
# the forbidden set explicitly — the forbidden list is the one that has to
# stay intentional, not this one.
_INGEST_BUNDLE_ALLOWED_TYPES = frozenset(
    set(R6Resource.SUPPORTED_TYPES) - _INGEST_BUNDLE_FORBIDDEN_TYPES)


# Moved to r6/body_guard.py: three modules imported these back out of
# here, lazily, to dodge the import cycle that reaching into this
# module created. Re-exported for the callers already inside it.


#: Hard cap on a diagnostic payload. This endpoint takes caller-controlled
#: JSON and writes it to operational logs, so unbounded input is a
#: log-flooding primitive (#279 is the same shape on ingest). Fasten's real
#: refusal payloads are a few hundred bytes.
_CONNECT_DIAGNOSTIC_MAX_BYTES = 4096

#: Keys that mean a FHIR resource arrived. A configuration refusal happens
#: BEFORE any record is retrieved, so a resource here means either the page is
#: sending more than it should or someone is probing the endpoint. Either way
#: this must not become the one unaudited, unredacted write path into our logs.
_CONNECT_DIAGNOSTIC_FORBIDDEN = ('resourceType', 'entry', 'identifier')


@r6_blueprint.route('/internal/connect-diagnostic', methods=['POST'])
def record_connect_diagnostic():
    """Record a records-connection refusal so support has something to quote.

    The connect page handles Fasten's `widget.config_error` and tells the
    patient the truth, but the payload only ever reached the browser console.
    When Fasten asked for "a request id so we can correlate the error in our
    logs" (FAS-864) there was nothing to send: two testers had hit
    fasten_unauthorized_client and the only record of either attempt was an
    email describing it.

    This endpoint is deliberately NOT a FHIR write. It records an operational
    event and returns a short reference the patient can read back to us and we
    can quote upstream.
    """
    body = request.get_json(silent=True) or {}
    payload = body.get('payload')
    if not isinstance(payload, dict) or not payload:
        return jsonify({'error': 'payload object required'}), 400

    raw = json.dumps(payload, separators=(',', ':'))
    if len(raw.encode('utf-8')) > _CONNECT_DIAGNOSTIC_MAX_BYTES:
        # No size echoed back and nothing from the payload: a 4xx that quotes
        # what it rejected reflects the caller's string.
        return jsonify({'error': 'diagnostic payload too large'}), 413

    if any(key in payload for key in _CONNECT_DIAGNOSTIC_FORBIDDEN):
        logger.warning(
            'connect-diagnostic refused: payload carries record-shaped keys '
            '(tenant=%s)', request.headers.get('X-Tenant-Id', 'unknown'))
        return jsonify({'error': 'diagnostic payload rejected'}), 422

    reference = f'ccd_{uuid.uuid4().hex[:12]}'
    # WARNING, not INFO: this is a user-visible failure of the product's front
    # door, and it needs to be greppable in the same pass as an outage.
    logger.warning(
        'connect-diagnostic %s tenant=%s payload=%s',
        reference, request.headers.get('X-Tenant-Id', 'unknown'), raw)
    return jsonify({'reference': reference}), 202


@r6_blueprint.route('/internal/step-up-token', methods=['POST'])
def issue_step_up_token():
    """
    Issue a step-up token for the dashboard demo. Gated by
    `_internal_mint_authorized` (fail-closed for non-public tenants).
    """
    body = request.get_json(silent=True) or {}
    tenant_id = body.get('tenant_id') or request.headers.get('X-Tenant-Id', 'default')

    if not _internal_mint_authorized(tenant_id):
        return jsonify({'error': 'forbidden'}), 403

    try:
        token = generate_step_up_token(tenant_id)
        return jsonify({'token': token, 'tenant_id': tenant_id})
    except ValueError as e:
        return jsonify({'error': str(e)}), 500


@r6_blueprint.route('/internal/bind-telegram', methods=['POST'])
def bind_telegram_chat():
    """
    Bind a Telegram chat to a tenant so the Fasten ingest webhook can push
    'your records are ready' notifications back through OpenClaw without
    polling. Called by the OpenClaw bot from its /start handler.

    Body:
        tenant_id: str   — required
        chat_id:   int   — required (Telegram chat id)
        username:  str   — optional, for audit/UI
        step_up_token: str — required (HMAC tenant-bound, 5-min TTL)

    Returns the binding id + bound_at timestamp.
    """
    body = request.get_json(silent=True) or {}
    tenant_id = (body.get('tenant_id') or '').strip()
    chat_id_raw = body.get('chat_id')
    username = (body.get('username') or '').strip() or None
    token = (body.get('step_up_token')
             or request.headers.get('X-Step-Up-Token', '')).strip()

    if not tenant_id or chat_id_raw is None:
        return jsonify({'error': 'tenant_id and chat_id are required'}), 400
    try:
        chat_id = int(chat_id_raw)
    except (TypeError, ValueError):
        return jsonify({'error': 'chat_id must be an integer'}), 400
    if not _TENANT_ID_PATTERN.fullmatch(tenant_id):
        return jsonify({'error': 'invalid tenant_id format'}), 400

    from r6.stepup import validate_step_up_token
    if not token:
        return jsonify({'error': 'valid step-up token required'}), 401
    valid, err = validate_step_up_token(token, tenant_id)
    if not valid:
        return jsonify({'error': public_step_up_reason(err)}), 401

    from r6.telegram_push import bind as bind_chat
    try:
        row = bind_chat(tenant_id=tenant_id, chat_id=chat_id, username=username)
    except Exception as exc:
        logger.exception('bind-telegram failed: %s', exc)
        return jsonify({'error': 'binding failed'}), 500

    record_audit_event(
        'create', 'TelegramBinding', row.id,
        agent_id='openclaw',
        tenant_id=tenant_id,
        detail=f'chat_id={chat_id} username={username or ""}',
    )

    return jsonify({
        'binding_id': row.id,
        'tenant_id': tenant_id,
        'chat_id': chat_id,
        'bound_at': row.bound_at.isoformat() if row.bound_at else None,
    }), 201


@r6_blueprint.route('/internal/purge-tenant', methods=['POST'])
def purge_tenant_route():
    """Delete a tenant's PHI-bearing data — the engine behind "delete my records".

    Gated exactly like seed/mint (fail-closed for non-public tenants) because
    deletion is at least as sensitive as creation. The AuditEvent trail is
    retained by design and the deletion itself is audited; see r6/purge.py.
    """
    body = request.get_json(silent=True) or {}
    tenant_id = body.get('tenant_id') or request.headers.get('X-Tenant-Id')
    if not tenant_id:
        return jsonify({'error': 'tenant_id is required'}), 400
    if not _internal_mint_authorized(tenant_id):
        return jsonify({'error': 'forbidden'}), 403

    from r6.purge import purge_summary, purge_tenant

    try:
        deleted = purge_tenant(tenant_id)
        # Audit the deletion BEFORE committing, so the record of what was
        # removed is part of the same transaction as the removal. A failure
        # here aborts the purge rather than deleting data unrecorded (#182).
        add_audit_event(
            event_type='delete',
            resource_type='Tenant',
            resource_id=tenant_id,
            tenant_id=tenant_id,
            agent_id='purge',
            detail='tenant data purged on request',
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('tenant purge failed for %s', tenant_id)
        return jsonify({'error': 'purge failed', 'deleted': False}), 500

    total = purge_summary(deleted)
    logger.info('tenant purge complete: %s rows for %s', total, tenant_id)
    return jsonify({
        'tenant_id': tenant_id,
        'deleted': True,
        'rows_deleted': total,
        'detail': deleted,
        'audit_retained': True,
        'note': ('Clinical data and connector state removed. The PHI-free '
                 'audit trail is retained as the immutable record of prior '
                 'access, and this deletion was added to it.'),
    }), 200


@r6_blueprint.route('/internal/seed', methods=['POST'])
def seed_tenant():
    """
    Seed a tenant with a realistic Patient + Observations + Condition bundle
    for live MCP testing.

    Re-seeding with the BUILT-IN set is a no-op: every built-in resource
    carries a stable id and the seed skips what is already present (#457).
    A caller-supplied `bundle` is different — resources in it without an
    `id` still get a generated one and are appended, because a caller
    posting their own data is asking for new rows.

    This docstring used to open with "Idempotent" and then, in the same
    sentence, describe re-seeding as appending new rows with newly generated
    ids. It claimed the guarantee and described its violation in one breath.
    Nobody read past the first word, and the demo tenant reached twelve
    copies of the same patient. tests/test_seed_endpoint_is_idempotent.py
    now pins the behaviour, so this text describes something enforced rather
    than something asserted.

    Body (all optional):
        tenant_id: str  — defaults to 'desktop-demo'
        bundle: dict    — custom FHIR Bundle; if omitted, uses built-in sample
    """
    from r6.seed import seed_demo_data

    body = request.get_json(silent=True) or {}
    tenant_id = body.get('tenant_id') or request.headers.get('X-Tenant-Id', 'desktop-demo')

    # C1: seed also mints a write token — gate it exactly like the mint endpoint
    # (fail-closed for non-public tenants) so it can't be a token oracle.
    if not _internal_mint_authorized(tenant_id):
        return jsonify({'error': 'forbidden'}), 403

    # A caller-supplied bundle is INGESTION — the caller chooses what the
    # records say — so it takes the ingest gate, which grants no public-tenant
    # exemption. The mint gate above reasons about TOKENS; that does not
    # transfer to content. tests/test_seed_bundle_requires_the_ingest_gate.py
    custom_bundle = body.get('bundle')
    if custom_bundle:
        if not _internal_ingest_authorized(tenant_id):
            return jsonify({'error': 'forbidden'}), 403
        entries = custom_bundle.get('entry', [])
        resources = [e.get('resource') for e in entries if e.get('resource')]
    else:
        resources = None  # use built-in defaults

    count = seed_demo_data(tenant_id, resources=resources)

    token = None
    try:
        token = generate_step_up_token(tenant_id, agent_id='seed')
    except ValueError:
        pass

    return jsonify({
        'tenant_id': tenant_id,
        'created_count': count,
        'step_up_token': token,
        'note': ('Use step_up_token for write operations. Re-seeding the built-in '
                 'set is a no-op; supply a bundle to add resources.')
    }), 201


# Caps for the file-upload / SHL import ingest path (#227). Small on purpose —
# this is the zero-integration patient-facing path (paste or upload a bundle),
# not a bulk provider push, and the endpoint runs synchronously so the caller
# can render honest per-entry results. The 5 MiB / 500-entry ceiling matches
# the strongest production precedent (Azure FHIR: 500 entries, 28 MB; Bumble
# `RESEARCH/HEALTHCLAW_207_227_SAFETY_LIMITS_2026_08_02.md`). Overridable via
# env if a real bundle turns out larger in practice.
INGEST_BUNDLE_MAX_BYTES_DEFAULT = 5 * 1024 * 1024      # 5 MiB
INGEST_BUNDLE_MAX_ENTRIES_DEFAULT = 500

# The body of this internal endpoint is `{bundle: {...}}` — an ENVELOPE
# carrying a Bundle. `application/fhir+json` is the media type for a *raw*
# FHIR resource; using it for an envelope would mis-label the payload.
# The patient-facing CareAgents route accepts `application/fhir+json` for
# the raw Bundle the browser posts; that layer wraps into the envelope and
# calls this endpoint as `application/json`.
_INGEST_BUNDLE_MIME_TYPES = frozenset({'application/json'})


def _ingest_bundle_limits() -> tuple[int, int]:
    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name, '').strip()
        if not raw:
            return default
        try:
            v = int(raw)
        except ValueError:
            return default
        return v if v > 0 else default
    return (_int_env('INGEST_BUNDLE_MAX_BYTES', INGEST_BUNDLE_MAX_BYTES_DEFAULT),
            _int_env('INGEST_BUNDLE_MAX_ENTRIES', INGEST_BUNDLE_MAX_ENTRIES_DEFAULT))


def _read_body_with_hard_cap(max_bytes: int) -> tuple[bytes | None, tuple[dict, int] | None]:
    """Read the request body streamed, refusing after `max_bytes`.

    Content-Length alone is not a memory bound — chunked or length-absent
    requests can still be arbitrarily large. Read one byte past the cap so we
    can distinguish "exactly at the limit" from "over"; on over, return a
    413 pair for the caller to jsonify.
    """
    clen = request.content_length
    if clen is not None and clen > max_bytes:
        return None, ({'error': 'payload_too_large',
                       'max_bytes': max_bytes,
                       'received_bytes': clen}, 413)
    try:
        raw = request.stream.read(max_bytes + 1)
    except Exception:
        return None, ({'error': 'invalid_body'}, 400)
    if raw is None:
        raw = b''
    if len(raw) > max_bytes:
        return None, ({'error': 'payload_too_large',
                       'max_bytes': max_bytes}, 413)
    return raw, None


@r6_blueprint.route('/internal/ingest-bundle', methods=['POST'])
def ingest_bundle():
    """Synchronous FHIR Bundle ingest for the file-upload / SHL import path (#227).

    The `direct` and `shl` connector tiles previously advertised upload/paste
    but had no server-side path — this is that path. It reuses `_ingest_one`
    (the same code path Fasten/SHC take, with parameterized provenance so
    the audit event honestly records `direct-upload` rather than borrowing
    Fasten's), is gated fail-closed via `_internal_ingest_authorized` —
    unlike seed/purge, the public-tenant exemption does NOT apply here,
    because authoring content is a different risk than minting a token (see
    that function's docstring) — and is deliberately synchronous so a
    patient watching an upload sees an honest per-entry result.

    Request contract:
      - Header `X-Tenant-Id`: required. The tenant to write into. This is
        the ONLY tenant selector; a `tenant_id` in the JSON body is rejected
        as a legacy selector rather than silently honored (an attacker who
        can influence the body could otherwise redirect the write).
      - Header `X-Internal-Secret`: required unconditionally (no
        public-tenant exemption — see `_internal_ingest_authorized`).
      - Header `Content-Type`: `application/json` (charset optional). The
        body is an envelope carrying a Bundle, NOT a raw FHIR resource,
        so `application/fhir+json` is refused here. The patient-facing
        CareAgents route accepts `fhir+json` for the raw Bundle from the
        browser and wraps it into the envelope for this internal call.
      - Body: `{ "bundle": { "resourceType": "Bundle", "entry": [...] } }`.
        `entry[].resource` is the FHIR entry shape; a bare entry without a
        `resource` object is rejected per-entry (we do not silently pick a
        different shape).

    Fail-loud on:
      - missing/invalid tenant header, missing secret (400/403)
      - wrong content-type (415), malformed JSON (400)
      - body larger than INGEST_BUNDLE_MAX_BYTES via streaming cap (413)
      - not a FHIR Bundle, or entry count over INGEST_BUNDLE_MAX_ENTRIES (400)
      - legacy `tenant_id` in body (400)

    Per-entry failures are ATOMIC per-entry via SAVEPOINT: a failure rolls
    back only that row, so a mid-Bundle DB exception can never delete
    earlier successes while still counting them as ingested. Every failure
    is surfaced in `errors[]` with a stable code; exception text is NEVER
    returned to the caller (SQL/driver messages can carry PHI). Instead a
    correlation id is logged server-side and returned as an opaque handle.
    """
    max_bytes, max_entries = _ingest_bundle_limits()

    # Auth gate FIRST — before content-type sniffing, before the body is
    # read, before anything is parsed. #267's review found the body read and
    # JSON parse both happened before this check, so a deeply-nested payload
    # could crash the worker (RecursionError, uncaught) with NO credentials
    # at all, against ANY tenant. Tenant selection needs only the header, so
    # nothing below this point requires touching the request body.
    tenant_id = request.headers.get('X-Tenant-Id', '').strip()
    if not tenant_id:
        return jsonify({'error': 'tenant_id is required'}), 400
    if not _TENANT_ID_PATTERN.fullmatch(tenant_id):
        return jsonify({'error': 'invalid tenant_id format'}), 400
    if not _internal_ingest_authorized(tenant_id):
        return jsonify({'error': 'forbidden'}), 403

    ct_raw = (request.content_type or '').split(';', 1)[0].strip().lower()
    if ct_raw not in _INGEST_BUNDLE_MIME_TYPES:
        return jsonify({'error': 'content_type_required',
                        'message': 'Content-Type must be one of: '
                                   + ', '.join(sorted(_INGEST_BUNDLE_MIME_TYPES))}), 415

    raw, err = _read_body_with_hard_cap(max_bytes)
    if err is not None:
        body_err, status = err
        return jsonify(body_err), status

    try:
        body = json.loads(raw.decode('utf-8')) if raw else {}
    except RecursionError:
        # CPython's JSON array/object scanner recurses per nesting level.
        # A 120KB payload nested ~60,000 deep reproduces this with no size
        # cap anywhere near triggering — the byte cap below is irrelevant to
        # this crash lever. Caught explicitly because RecursionError is not
        # a ValueError and was previously unhandled -> 500.
        return jsonify({'error': 'invalid_json',
                        'message': 'nesting too deep to parse'}), 400
    except (ValueError, UnicodeDecodeError):
        return jsonify({'error': 'invalid_json'}), 400
    if not isinstance(body, dict):
        return jsonify({'error': 'invalid_json',
                        'message': 'body must be a JSON object'}), 400
    if not _json_depth_within(body, _INGEST_MAX_JSON_DEPTH):
        # Defense in depth beyond the RecursionError catch above: a payload
        # that parses successfully (recursion limit not hit) but is still
        # absurdly nested can cause problems downstream — audit-event
        # construction, redaction, re-serialization. Refuse it here rather
        # than trust every later consumer to be equally careful.
        return jsonify({'error': 'bundle_too_deep',
                        'max_depth': _INGEST_MAX_JSON_DEPTH}), 400

    # Header is the ONLY tenant selector — any body `tenant_id` is a legacy
    # client-controlled selector and is refused rather than treated as
    # authoritative even when it agrees with the header. Anything less lets
    # a request-shaping bug turn into a cross-tenant write oracle.
    if 'tenant_id' in body:
        return jsonify({'error': 'legacy_body_selector',
                        'message': 'Tenant is derived from X-Tenant-Id only; '
                                   'remove "tenant_id" from the body.'}), 400

    bundle = body.get('bundle')
    if not isinstance(bundle, dict) or bundle.get('resourceType') != 'Bundle':
        return jsonify({'error': 'not_a_bundle',
                        'message': 'bundle.resourceType must be "Bundle"'}), 400

    entries_raw = bundle.get('entry') or []
    if not isinstance(entries_raw, list):
        return jsonify({'error': 'invalid_bundle',
                        'message': 'bundle.entry must be an array'}), 400
    if len(entries_raw) > max_entries:
        return jsonify({'error': 'too_many_entries',
                        'max_entries': max_entries,
                        'received_entries': len(entries_raw)}), 400

    # Reuse the same code path Fasten/SHC take — a change to ingest semantics
    # cannot silently diverge for the upload path — with honest provenance.
    from r6.fasten.ingester import _ingest_one

    correlation_id = uuid.uuid4().hex[:12]

    ingested = skipped = failed = 0
    errors: list[dict] = []
    for idx, entry in enumerate(entries_raw):
        # FHIR entries carry the resource under `entry.resource`; nothing
        # else is a real exporter shape and accepting a "flat" entry would
        # widen the surface for typo bugs.
        if not isinstance(entry, dict) or not isinstance(entry.get('resource'), dict):
            failed += 1
            errors.append({'index': idx, 'code': 'invalid_entry',
                           'message': 'entry.resource must be a JSON object'})
            continue
        resource = entry['resource']
        rtype = resource.get('resourceType') or ''
        # SAVEPOINT per entry — a driver-level failure on this row is
        # rolled back inside the savepoint, so previous flushed rows in
        # the outer transaction remain and the reported `ingested` count
        # matches the rows the caller can actually read back.
        try:
            with db.session.begin_nested():
                result, _rid = _ingest_one(
                    resource, tenant_id,
                    agent_id='direct-upload',
                    detail='Ingested via patient direct upload',
                    allowed_types=_INGEST_BUNDLE_ALLOWED_TYPES)
        except Exception as exc:  # noqa: BLE001
            # NEVER return `str(exc)` — SQL driver messages can echo
            # statements and parameters that carry PHI. Log ONLY the
            # exception class name (never the message or a traceback that
            # can echo bound values) against a correlation id, and return
            # an opaque code to the caller.
            failed += 1
            logger.warning(
                'ingest-bundle entry %d failed (tenant=%s correlation=%s '
                'exc_class=%s)',
                idx, tenant_id, correlation_id, type(exc).__name__)
            errors.append({'index': idx, 'resourceType': rtype,
                           'code': 'ingest_error',
                           'correlation_id': correlation_id,
                           'message': 'Entry could not be persisted; '
                                      'see server logs by correlation_id.'})
            continue
        if result == 'ok':
            ingested += 1
        elif result == 'forbidden':
            skipped += 1
            errors.append({'index': idx, 'resourceType': rtype,
                           'code': 'forbidden_type',
                           'message': f'{rtype!r} may not be authored via '
                                      'direct upload'})
        elif result == 'invalid_id':
            skipped += 1
            errors.append({'index': idx, 'resourceType': rtype,
                           'code': 'invalid_resource_id',
                           'message': 'entry.resource.id is not a valid '
                                      'FHIR id and was refused'})
        else:
            skipped += 1
            errors.append({'index': idx, 'resourceType': rtype,
                           'code': 'unsupported_resource_type',
                           'message': f'{rtype!r} is not a supported type'})

    try:
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        # Same PHI rule as per-entry: never log the exception message or
        # let it back through the response. Class name + correlation id
        # is enough to reproduce from operator-side logs.
        db.session.rollback()
        logger.warning('ingest-bundle commit failed (tenant=%s '
                       'correlation=%s exc_class=%s)',
                       tenant_id, correlation_id, type(exc).__name__)
        return jsonify({'error': 'commit_failed',
                        'correlation_id': correlation_id,
                        'ingested': 0, 'skipped': skipped, 'failed': failed,
                        'errors': errors}), 500

    # Audit outcome is `partial` whenever the bundle wasn't fully successful —
    # a skipped entry (unsupported resource type) is a signal too, not just
    # a hard failure, and lumping it under `success` hides the truth.
    outcome = 'success' if (failed == 0 and skipped == 0) else 'partial'
    record_audit_event(
        event_type='ingest_bundle',
        agent_id='direct-upload',
        tenant_id=tenant_id,
        outcome=outcome,
        detail=(f'entries={len(entries_raw)} ingested={ingested} '
                f'skipped={skipped} failed={failed} '
                f'correlation={correlation_id}'),
    )

    return jsonify({
        'tenant_id': tenant_id,
        'entries': len(entries_raw),
        'ingested': ingested,
        'skipped': skipped,
        'failed': failed,
        'errors': errors,
        'correlation_id': correlation_id,
    }), 200


# --- SSE Audit Stream ---

@r6_blueprint.route('/AuditEvent/$stream', methods=['GET'])
def audit_stream():
    """
    Server-Sent Events stream for real-time audit trail.
    Clients receive new AuditEvents as they are created.
    """
    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id

    def generate():
        last_id = None
        while True:
            try:
                query = AuditEventRecord.query.filter_by(
                    tenant_id=tenant_id
                ).order_by(AuditEventRecord.recorded.desc()).limit(5)

                if last_id:
                    query = query.filter(AuditEventRecord.id != last_id)

                events = query.all()
                for event in events:
                    if last_id and event.id == last_id:
                        continue
                    data = json.dumps(event.to_fhir_json())
                    yield f"data: {data}\n\n"

                if events:
                    last_id = events[0].id

            except Exception:
                pass

            time.sleep(1)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


# --- Agent Demo Loop ---

# The demo loop writes at FIXED ids, one set per tenant. It used to mint a
# uuid8 suffix per call, so every press of "Run 6-Step Guardrail Demo" — and
# every e2e run, since dashboard.spec.ts drives that button — left another
# Patient behind. The demo tenant reached 19, and $care-gaps then correctly
# refused to guess whose preventive care to evaluate, so two steps of our own
# 10-minute demo script stopped working (#415). None of the six steps needs a
# distinct patient per run: they demonstrate create, read, redaction, audit and
# permission enforcement, all of which a repeated run shows identically. A
# showcase a viewer can re-run and see the same thing is the point.
_DEMO_LOOP_PATIENT_ID = 'demo-loop-pt'
_DEMO_LOOP_OBSERVATION_ID = 'demo-loop-obs'
_DEMO_LOOP_PERMISSION_ID = 'demo-loop-perm'


def _demo_loop_upsert(resource, tenant_id):
    """Write one demo resource at its fixed id, reviving a tombstone.

    Identity is (tenant_id, resource_type, id) — the composite PK — so a
    plain insert on the second run collides. Same upsert shape as the Fasten
    ingester (r6/fasten/ingester.py), including the deliberate absence of an
    `is_deleted` filter: step 3 soft-deletes every Permission for the tenant,
    so on run two the Permission written in step 4 is revived from a tombstone
    this endpoint laid down itself moments earlier.
    """
    resource_json = json.dumps(resource, separators=(',', ':'), sort_keys=True)
    existing = R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type=resource['resourceType'],
        id=resource['id'],
    ).first()
    if existing:
        existing.update_resource(resource_json)
        existing.is_deleted = False
        row = existing
    else:
        row = R6Resource(
            resource_type=resource['resourceType'],
            resource_json=resource_json,
            resource_id=resource['id'],
            tenant_id=tenant_id,
        )
        db.session.add(row)
    db.session.commit()
    return row


@r6_blueprint.route('/demo/agent-loop', methods=['POST'])
def demo_agent_loop():
    """
    Orchestrated 6-step agent guardrail demo.

    Executes the full security pattern sequence that tells the guardrail story:
    1. Read patient (redacted) — shows PHI protection
    2. Agent proposes MedicationRequest — shows $validate gate
    3. Permission $evaluate DENIES — shows access control
    4. Create permit rule + re-evaluate — shows policy change
    5. Step-up auth + human-in-the-loop check — shows write gate
    6. Commit write with full audit trail — shows end-to-end

    Each step returns its result so the dashboard can render progressively.

    IMPORTANT — this endpoint NARRATES the guardrail pattern for the demo
    dashboard; it does not enforce it. The step descriptions below are scripted
    copy, not the outcome of a live gate. Real enforcement lives on the actual
    FHIR routes (redaction on read, step-up + human confirmation on write,
    audit on everything) and is what `$conformance` grades.

    Two step-up tokens used to be minted here and thrown away, which read like
    authorization and was not. They are gone; the write authorization for this
    endpoint is the `_internal_mint_authorized` gate below.
    """
    tenant_id = request.headers.get('X-Tenant-Id', 'demo-tenant')

    # This endpoint WRITES (Patient, Permission, Observation) and soft-deletes
    # every existing Permission for the tenant, and it lives under /demo/ —
    # a prefix exempt from tenant enforcement and human-in-the-loop. Without a
    # gate it is an anonymous cross-tenant write + access-policy-delete
    # primitive against any tenant an attacker can name, which falsifies the
    # whole "a client cannot bypass the guardrails" claim.
    #
    # Same fail-closed gate as seed/mint: public/synthetic tenants (what the
    # demo is actually for) are allowed; anything else needs the internal
    # secret. Deliberately reusing that helper rather than inventing a second
    # authorization rule for a third kind of privileged write.
    if not _internal_mint_authorized(tenant_id):
        return jsonify({'error': 'forbidden'}), 403

    steps = []

    # --- Step 1: Create + Read Patient (redacted) ---
    patient = {
        'resourceType': 'Patient',
        'id': _DEMO_LOOP_PATIENT_ID,
        'name': [{'family': 'Rivera', 'given': ['Maria', 'Elena']}],
        'gender': 'female',
        'birthDate': '1990-03-15',
        'identifier': [{'system': 'http://hospital.example/mrn', 'value': 'MRN-2026-4471'}],
        'address': [{'line': ['123 Clinical Ave'], 'city': 'Boston', 'state': 'MA', 'postalCode': '02115'}],
        'telecom': [{'system': 'phone', 'value': '617-555-0198', 'use': 'mobile'}],
    }

    _demo_loop_upsert(patient, tenant_id)
    record_audit_event('create', 'Patient', patient['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: created patient for guardrail walkthrough')

    # Read back with redaction
    read_resource = R6Resource.query.filter_by(
        id=patient['id'], resource_type='Patient',
        is_deleted=False, tenant_id=tenant_id
    ).first()
    redacted_patient = apply_redaction(read_resource.to_fhir_json())
    record_audit_event('read', 'Patient', patient['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: read patient with PHI redaction applied')

    steps.append({
        'step': 1,
        'title': 'Read Patient Record (PHI Redacted)',
        'action': 'fhir.read Patient/' + patient['id'],
        'status': 'success',
        'guardrail': 'PHI redaction',
        'detail': 'Identifiers masked, addresses stripped, telecom redacted. Agent sees only safe data.',
        'result': redacted_patient,
    })

    # --- Step 2: Agent proposes MedicationRequest ---
    med_request = {
        'resourceType': 'Observation',
        'id': _DEMO_LOOP_OBSERVATION_ID,
        'status': 'preliminary',
        'code': {
            'coding': [{'system': 'http://loinc.org', 'code': '2339-0', 'display': 'Glucose [Mass/volume] in Blood'}],
        },
        'subject': {'reference': f'Patient/{patient["id"]}'},
        'valueQuantity': {'value': 142, 'unit': 'mg/dL', 'system': 'http://unitsofmeasure.org', 'code': 'mg/dL'},
        'interpretation': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'H', 'display': 'High'}]}],
    }

    validation_result = validator.validate_resource(med_request)
    record_audit_event('validate', 'Observation', med_request['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail=f'Agent demo: validated proposed Observation, valid={validation_result["valid"]}')

    steps.append({
        'step': 2,
        'title': 'Agent Proposes Clinical Observation',
        'action': 'fhir.propose_write Observation (Glucose 142 mg/dL — HIGH)',
        'status': 'validated' if validation_result['valid'] else 'rejected',
        'guardrail': '$validate gate',
        'detail': 'Agent proposal passes structural validation. Now checking access control...',
        'result': {
            'proposed_resource': med_request,
            'validation': validation_result['operation_outcome'],
            'requires_step_up': True,
            'requires_human_confirmation': True,
        },
    })

    # --- Step 3: Permission $evaluate DENIES (no rules yet) ---
    # Clear any existing permissions for clean demo
    existing_perms = R6Resource.query.filter_by(
        resource_type='Permission', tenant_id=tenant_id, is_deleted=False
    ).all()
    for p in existing_perms:
        p.is_deleted = True
    db.session.commit()

    {
        'subject': 'Agent/demo-agent',
        'action': 'create',
        'resource': f'Observation/{med_request["id"]}',
    }

    # Evaluate with no permissions — should deny
    R6Resource.query.filter_by(
        resource_type='Permission', is_deleted=False, tenant_id=tenant_id
    ).all()

    deny_reasoning = 'No active Permission resources found for this tenant. Default deny applies.'
    record_audit_event('read', 'Permission', None,
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: $evaluate — subject=Agent/demo-agent, action=create, decision=deny')

    steps.append({
        'step': 3,
        'title': 'Permission $evaluate — ACCESS DENIED',
        'action': 'fhir.permission_evaluate',
        'status': 'denied',
        'guardrail': 'R6 Permission access control',
        'detail': 'No active Permission resources exist. Default-deny policy blocks the write.',
        'result': {
            'resourceType': 'Parameters',
            'parameter': [
                {'name': 'decision', 'valueCode': 'deny'},
                {'name': 'matched_rules', 'valueInteger': 0},
                {'name': 'subject', 'valueString': 'Agent/demo-agent'},
                {'name': 'action', 'valueCode': 'create'},
                {'name': 'reasoning', 'valueString': deny_reasoning},
            ],
        },
    })

    # --- Step 4: Create permit rule + re-evaluate → PERMIT ---
    permission = {
        'resourceType': 'Permission',
        'id': _DEMO_LOOP_PERMISSION_ID,
        'status': 'active',
        'combining': 'permit-overrides',
        'asserter': {'reference': 'Organization/hospital-1'},
        'justification': {
            'basis': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/v3-ActReason', 'code': 'TREAT', 'display': 'Treatment'}]}],
        },
        'rule': [{
            'type': 'permit',
            'activity': [{
                'action': [{'coding': [{'system': 'http://hl7.org/fhir/permission-action', 'code': 'create'}]}],
                'purpose': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/v3-ActReason', 'code': 'TREAT'}]}],
            }],
        }],
    }

    _demo_loop_upsert(permission, tenant_id)
    record_audit_event('create', 'Permission', permission['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: created permit rule for treatment-purpose writes')

    # Re-evaluate — now should permit
    permit_reasoning = (f'Matched 1 rule(s): permit (Permission/{permission["id"]}, '
                        f'combining=permit-overrides). Final decision: permit.')
    record_audit_event('read', 'Permission', None,
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: $evaluate — action=create, decision=permit')

    steps.append({
        'step': 4,
        'title': 'Create Permit Rule + Re-evaluate — ACCESS GRANTED',
        'action': 'fhir.permission_evaluate (after policy change)',
        'status': 'permitted',
        'guardrail': 'R6 Permission with reasoning',
        'detail': 'Treatment-purpose permit rule created. Re-evaluation now allows the write.',
        'result': {
            'permission_created': permission,
            'evaluation': {
                'resourceType': 'Parameters',
                'parameter': [
                    {'name': 'decision', 'valueCode': 'permit'},
                    {'name': 'matched_rules', 'valueInteger': 1},
                    {'name': 'subject', 'valueString': 'Agent/demo-agent'},
                    {'name': 'action', 'valueCode': 'create'},
                    {'name': 'reasoning', 'valueString': permit_reasoning},
                ],
            },
        },
    })

    # --- Step 5: Step-up auth + human-in-the-loop enforcement ---
    # Show what happens WITHOUT human confirmation
    hitl_detail = (
        'Clinical write (Observation) requires X-Human-Confirmed: true header. '
        'Without it, server returns HTTP 428 Precondition Required. '
        'Agent must surface the proposed write to a human reviewer.'
    )
    record_audit_event('read', 'Observation', med_request['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: step-up token issued, human confirmation required')

    steps.append({
        'step': 5,
        'title': 'Step-up Auth + Human-in-the-Loop Gate',
        'action': 'Request X-Step-Up-Token + X-Human-Confirmed',
        'status': 'awaiting_confirmation',
        'guardrail': 'HMAC step-up + human-in-the-loop',
        'detail': hitl_detail,
        'result': {
            'step_up_token_issued': True,
            'token_type': 'HMAC-SHA256 with 128-bit nonce',
            'token_ttl_seconds': 300,
            'human_confirmation_required': True,
            'blocked_without_header': {
                'status': 428,
                'body': {
                    'resourceType': 'OperationOutcome',
                    'issue': [{
                        'severity': 'error',
                        'code': 'precondition-required',
                        'diagnostics': 'Clinical writes require X-Human-Confirmed: true',
                    }],
                },
            },
        },
    })

    # --- Step 6: Commit write with full audit trail ---
    obs_resource = _demo_loop_upsert(med_request, tenant_id)
    record_audit_event('create', 'Observation', med_request['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: committed Observation after full guardrail sequence')

    committed = apply_redaction(obs_resource.to_fhir_json())
    committed = add_disclaimer(committed, 'Observation')

    # Gather all audit events for this demo
    demo_audits = AuditEventRecord.query.filter_by(
        tenant_id=tenant_id, agent_id='demo-agent'
    ).order_by(AuditEventRecord.recorded.desc()).limit(10).all()

    steps.append({
        'step': 6,
        'title': 'Commit Write — Full Audit Trail',
        'action': 'fhir.commit_write Observation (with step-up + human confirmation)',
        'status': 'committed',
        'guardrail': 'Append-only audit trail',
        'detail': 'Write committed after passing all guardrails. Every step recorded in immutable audit trail.',
        'result': {
            'committed_resource': committed,
            'audit_trail': [e.to_fhir_json() for e in demo_audits],
        },
    })

    return jsonify({
        # Constant, like the ids it names: a run is no longer distinguishable
        # from the run before it, which is the fix. Kept in the response
        # because it is part of the published shape.
        'demo_id': 'demo-loop',
        'title': 'MCP Guardrail Pattern Sequence',
        'description': 'Complete 6-step walkthrough showing how security patterns protect clinical data when an AI agent accesses FHIR resources via MCP.',
        'guardrails_demonstrated': [
            'PHI redaction on reads',
            '$validate gate on proposals',
            'R6 Permission $evaluate with reasoning',
            'Policy change + re-evaluation',
            'HMAC step-up tokens + human-in-the-loop',
            'Append-only audit trail',
        ],
        'steps': steps,
    })


# --- Curatr Data Quality Operations ---

@r6_blueprint.route(
    '/<resource_type>/<resource_id>/$curatr-evaluate',
    methods=['GET']
)
def curatr_evaluate(resource_type, resource_id):
    """
    Evaluate a FHIR resource for data quality issues.

    Checks coding elements against public terminology services
    (tx.fhir.org, NLM ICD-10, RXNAV) and returns issues in plain
    language with impact descriptions and resolution suggestions.

    Read-only — does not require step-up authorization.
    """
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome(
            'error', 'not-supported',
            f'Resource type {resource_type} is not supported'
        ), 400

    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
    resource = R6Resource.query.filter_by(
        id=resource_id, resource_type=resource_type,
        is_deleted=False, tenant_id=tenant_id
    ).first()

    if not resource:
        return _operation_outcome(
            'error', 'not-found',
            f'{resource_type}/{resource_id} not found'
        ), 404

    record_audit_event(
        'read', resource_type, resource_id,
        agent_id=request.headers.get('X-Agent-Id'),
        tenant_id=tenant_id,
        detail='curatr-evaluate',
    )

    fhir_json = resource.to_fhir_json()
    result = _curatr_engine.evaluate(fhir_json)

    # Persist curation_state + quality_score on the row. This is what makes
    # $compiled-truth reflect the latest quality signal without re-running
    # terminology lookups on every read.
    _persist_curation_state(
        resource_type, resource_id, tenant_id, result, fixed=False,
    )

    body = result.to_dict()
    # Surface persisted state alongside the result for callers that skip
    # a separate $compiled-truth fetch.
    body['curation_state'] = resource.curation_state
    body['quality_score'] = resource.quality_score
    return jsonify(body)


@r6_blueprint.route(
    '/<resource_type>/<resource_id>/$curatr-apply-fix',
    methods=['POST']
)
def curatr_apply_fix(resource_type, resource_id):
    """
    Apply patient-approved data quality fixes to a FHIR resource.

    Request body::

        {
          "fixes": [
            {"field_path": "Condition.code.coding[0].system",
             "new_value": "http://hl7.org/fhir/sid/icd-10-cm"},
            {"field_path": "Condition.code.coding[0].code",
             "new_value": "E11.9"},
            {"field_path": "Condition.code.coding[0].display",
             "new_value": "Type 2 diabetes mellitus without complications"}
          ],
          "patient_intent": "Updating from retired ICD-9 to ICD-10-CM"
        }

    Production requires an operation-bound, single-use Curatr approval token.
    ``X-Human-Confirmed`` remains a compatibility signal, but is not treated as
    proof of approval by itself.

    Creates a linked Provenance resource with full change attribution.
    """
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome(
            'error', 'not-supported',
            f'Resource type {resource_type} is not supported'
        ), 400

    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
    step_up_token = request.headers.get('X-Step-Up-Token')
    if not step_up_token:
        return _operation_outcome(
            'error', 'security',
            'Write operations require X-Step-Up-Token header'
        ), 403

    production_approval = resolve_app_env() == 'production'
    operation = f'curatr-apply-fix:{resource_type}/{resource_id}'
    valid, err = validate_step_up_token(
        step_up_token,
        tenant_id,
        require_audience='curatr' if production_approval else None,
        require_operation=operation if production_approval else None,
    )
    if not valid:
        return _operation_outcome(
            'error', 'security',
            f'Step-up token rejected: {public_step_up_reason(err)}'
        ), 403

    body = request.get_json(silent=True)
    if not body:
        return _operation_outcome(
            'error', 'invalid', 'Request body must be valid JSON'
        ), 400

    fixes = body.get('fixes', [])
    patient_intent = body.get('patient_intent', 'Patient-initiated fix')

    if not fixes:
        return _operation_outcome(
            'error', 'invalid', 'fixes array is required and must not be empty'
        ), 400

    if production_approval:
        # Consume only after the request shape is valid so malformed attempts
        # cannot burn a legitimate one-time approval credential.
        valid, err = validate_step_up_token(
            step_up_token,
            tenant_id,
            consume_nonce=True,
            require_audience='curatr',
            require_operation=operation,
        )
        if not valid:
            return _operation_outcome(
                'error', 'security',
                f'Step-up token rejected: {public_step_up_reason(err)}'), 403

    try:
        result = _curatr_apply_fix(
            resource_type=resource_type,
            resource_id=resource_id,
            approved_fixes=fixes,
            patient_intent=patient_intent,
            tenant_id=tenant_id,
            agent_id=request.headers.get('X-Agent-Id', 'curatr'),
        )
    except RuntimeError as exc:
        logger.error('curatr_apply_fix failed: %s', type(exc).__name__)
        return _operation_outcome(
            'error', 'exception', 'Curatr fix could not be applied'
        ), 500

    if 'error' in result:
        return _operation_outcome('error', 'not-found', result['error']), 404

    # After a successful fix, re-evaluate and promote curation_state -> curated.
    try:
        from r6.curatr import compute_quality_score
        fresh = result.get('updated_resource') or {}
        if fresh:
            fresh_result = _curatr_engine.evaluate(fresh)
            _persist_curation_state(
                resource_type, resource_id, tenant_id, fresh_result,
                fixed=True,
            )
            result['curation_state'] = 'curated'
            result['quality_score'] = compute_quality_score(fresh_result)
    except Exception as exc:
        logger.warning(
            'curation state promotion failed (fix still committed): %s',
            type(exc).__name__,
        )

    # Evaluate the real resource, return a REDACTED copy — in that order.
    #
    # This path had no redaction at all (#282), so every free-text field the
    # approved fix did NOT touch came back exactly as the upstream feed wrote
    # it: `code.text`, `code.coding[].display`, and `note[].text`. Free-text
    # notes are where real feeds put names, and the realistic caller here is
    # the `curatr_apply_fix` MCP tool, so that text went into a model's
    # context. apply_redaction strips those fields and re-labels from
    # r6/terminology.py keyed by code, so the answer stays readable without
    # any of it coming from the feed.
    #
    # It has to come AFTER the promotion block above: `evaluate` scores the
    # resource's completeness, so scoring a redacted copy would compute the
    # quality score on stripped fields and promote curation_state on it. That
    # regression is silent. apply_redaction returns a new dict rather than
    # rewriting its argument, so rebinding here cannot reach back into the
    # object `evaluate` already consumed.
    if result.get('updated_resource'):
        result['updated_resource'] = apply_redaction(
            result['updated_resource'])

    return jsonify(result)


# --- Compiled Truth: current state + evidence timeline ------------

@r6_blueprint.route(
    '/<resource_type>/<resource_id>/$compiled-truth',
    methods=['GET']
)
def compiled_truth(resource_type, resource_id):
    """
    Return the current best understanding of a resource plus the
    append-only evidence trail of how it got there.

    Pattern inspired by gbrain's "compiled truth + timeline" — every
    resource has a canonical current state AND an immutable history
    of agents/reasons/changes. Patients see exactly what their record
    says now and why.

    Output is a FHIR Parameters resource with:
      - current: the redacted resource
      - curation_state, quality_score, review_needed
      - timeline: Provenance entries that target this resource,
        newest first. Each carries recorded/agent/reason/summary.

    Read-only. Redaction + audit + tenant isolation apply.
    """
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome(
            'error', 'not-supported',
            f'Resource type {resource_type} is not supported',
        ), 400

    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
    if not tenant_id:
        return _operation_outcome(
            'error', 'security',
            'X-Tenant-Id header is required',
        ), 400

    row = R6Resource.query.filter_by(
        id=resource_id, resource_type=resource_type,
        is_deleted=False, tenant_id=tenant_id,
    ).first()

    if not row:
        return _operation_outcome(
            'error', 'not-found',
            f'{resource_type}/{resource_id} not found',
        ), 404

    record_audit_event(
        'read', resource_type, resource_id,
        agent_id=request.headers.get('X-Agent-Id'),
        tenant_id=tenant_id,
        detail='compiled-truth',
    )

    current_json = apply_redaction(row.to_fhir_json())

    # Build timeline from Provenance resources targeting this reference.
    # Note: Provenance.target[] is stored as JSON; we do a prefix scan on
    # the JSON blob. Acceptable for the local store's scale.
    target_ref = f'{resource_type}/{resource_id}'
    prov_rows = R6Resource.query.filter_by(
        resource_type='Provenance',
        is_deleted=False,
        tenant_id=tenant_id,
    ).all()

    timeline = []
    for p in prov_rows:
        try:
            prov = json.loads(p.resource_json)
        except Exception:
            continue
        targets = prov.get('target') or []
        if not any(
            t.get('reference') == target_ref for t in targets
            if isinstance(t, dict)
        ):
            continue
        agent_display = 'system'
        for a in prov.get('agent', []) or []:
            who = a.get('who') or {}
            if isinstance(who, dict) and who.get('display'):
                agent_display = who['display']
                break
        reason = ''
        reasons = prov.get('reason') or []
        if reasons and isinstance(reasons[0], dict):
            codings = reasons[0].get('coding') or []
            if codings:
                reason = codings[0].get('display', '') or ''
        # Extract curatr-correction extension summary if present
        summary = ''
        intent = ''
        for ext in prov.get('extension', []) or []:
            if 'curatr-correction' not in (ext.get('url') or ''):
                continue
            for inner in ext.get('extension', []) or []:
                if inner.get('url') == 'change_summary':
                    summary = inner.get('valueString', '') or ''
                elif inner.get('url') == 'patient_intent':
                    intent = inner.get('valueString', '') or ''
        timeline.append({
            'provenance_id': p.id,
            'recorded': prov.get('recorded', ''),
            'agent': agent_display,
            'reason': reason,
            'summary': summary,
            'patient_intent': intent,
        })

    timeline.sort(key=lambda e: e.get('recorded', ''), reverse=True)

    parameters = {
        'resourceType': 'Parameters',
        'parameter': [
            {'name': 'current', 'resource': current_json},
            {
                'name': 'curation_state',
                'valueString': row.curation_state or 'raw',
            },
            {
                'name': 'quality_score',
                'valueDecimal': (
                    row.quality_score
                    if row.quality_score is not None else 1.0
                ),
            },
            {
                'name': 'review_needed',
                'valueBoolean': bool(row.review_needed),
            },
            {
                'name': 'timeline_count',
                'valueInteger': len(timeline),
            },
            {
                'name': 'timeline',
                'part': [
                    {
                        'name': 'event',
                        'part': [
                            {
                                'name': k,
                                'valueString': str(v) if v is not None else '',
                            }
                            for k, v in event.items()
                        ],
                    }
                    for event in timeline
                ],
            },
        ],
    }
    return jsonify(parameters)


# --- MCP Apps (embedded HTML surfaces for MCP clients) ------------

def _mcp_app_tenant():
    """The tenant an MCP App page was opened with, or `''` if none was named.

    Access kernel, slice 11a (spec §2.6). HEADER then QUERY: an MCP client
    sends the header, a browser opening the same URI cannot. Absent is not an
    error — `/mcp-apps/` is exempt and these pages render an input to type a
    tenant into, so they must open cold. Malformed propagates to the kernel's
    400. Both pinned in tests/test_mcp_app_tenant_goes_through_the_kernel.py.
    """
    try:
        return tenant_from_request(
            sources=(TenantSource.HEADER, TenantSource.QUERY)).id
    except TenantRejected as exc:
        if exc.reason == TenantRejected.ABSENT:
            return ''
        raise


@r6_blueprint.route(
    '/mcp-apps/compiled-truth/<resource_type>/<resource_id>',
    methods=['GET']
)
def mcp_app_compiled_truth(resource_type, resource_id):
    """
    MCP App: Compiled Truth Review.

    Single-page HTML surface that renders the $compiled-truth Parameters
    response (current state + evidence timeline) with Approve / Re-evaluate
    actions. Linked from the `fhir_compiled_truth` MCP tool response via
    `_meta.ui.resourceUri`. MCP clients that understand the
    `text/html;profile=mcp-app` content type render it inline; others
    treat it as a normal web page.
    """
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome(
            'error', 'not-supported',
            f'Resource type {resource_type} is not supported',
        ), 400

    tenant_id = _mcp_app_tenant()

    html = render_template(
        'mcp_apps/compiled_truth.html',
        resource_type=resource_type,
        resource_id=resource_id,
        tenant_id=tenant_id,
    )
    resp = Response(html, mimetype='text/html')
    resp.headers['Content-Type'] = 'text/html; profile=mcp-app'
    resp.headers['X-MCP-App'] = 'compiled-truth'
    return resp


@r6_blueprint.route('/mcp-apps/care-gaps/', methods=['GET'])
@r6_blueprint.route('/mcp-apps/care-gaps', methods=['GET'])
def mcp_app_care_gaps():
    """
    MCP App: Preventive Care Gaps.

    Renders the Patient/$care-gaps Parameters response (summary buckets,
    per-rule cards, consumer lines, disclaimer). Linked from the
    `care_gaps` MCP tool via `_meta.ui.resourceUri`. Layout ported from
    SmartHealthConnect's care-gaps view (archived); data path rebuilt on
    the engine's own operation so redaction + audit apply by construction.
    """
    tenant_id = _mcp_app_tenant()
    html = render_template(
        'mcp_apps/care_gaps.html',
        tenant_id=tenant_id,
        # The engine's own "nothing was read" list; the page must not copy it (#538).
        not_evaluated_reasons=_caregaps_caller_reasons(),
    )
    resp = Response(html, mimetype='text/html')
    resp.headers['Content-Type'] = 'text/html; profile=mcp-app'
    resp.headers['X-MCP-App'] = 'care-gaps'
    return resp


@r6_blueprint.route('/mcp-apps/lab-trends/', methods=['GET'])
@r6_blueprint.route('/mcp-apps/lab-trends', methods=['GET'])
def mcp_app_lab_trends():
    """
    MCP App: Lab Trends.

    A timeline of one analyte over time — the shape "give me a timeline of my
    cholesterol results" actually asks for, and which prose answers badly.
    Linked from the `fhir_interpret_labs` MCP tool via `_meta.ui.resourceUri`.

    Data path is the engine's own Observation/$interpret with an empty body,
    so redaction, audit and tenant scoping apply by construction AND the
    normal/high flags drawn here are the engine's verdict rather than a
    threshold re-implemented in a browser. Reference ranges live in exactly
    one place (r6/labs/interpret.py) and this view is not a second one.
    """
    tenant_id = _mcp_app_tenant()
    html = render_template(
        'mcp_apps/lab_trends.html',
        tenant_id=tenant_id,
    )
    resp = Response(html, mimetype='text/html')
    resp.headers['Content-Type'] = 'text/html; profile=mcp-app'
    resp.headers['X-MCP-App'] = 'lab-trends'
    return resp


@r6_blueprint.route('/mcp-apps/wearables/', methods=['GET'])
@r6_blueprint.route('/mcp-apps/wearables', methods=['GET'])
def mcp_app_wearables():
    """
    MCP App: Wearables Connection Manager.

    Shows one card per supported provider with connection status, last
    sync, observation count, and Connect / Sync / Re-auth actions. Linked
    from the `wearables_sync_status` MCP tool via `_meta.ui.resourceUri`.
    """
    tenant_id = _mcp_app_tenant()
    html = render_template(
        'mcp_apps/wearables.html',
        tenant_id=tenant_id,
    )
    resp = Response(html, mimetype='text/html')
    resp.headers['Content-Type'] = 'text/html; profile=mcp-app'
    resp.headers['X-MCP-App'] = 'wearables'
    return resp


# --- $share-bundle Export (SMART Health Link feed) ---

def _intake_strip(res):
    """Intake profile: identified for clinic check-in (name/DOB/address/telecom
    preserved) but SSN-class identifiers and clinician free-text never ship."""
    res.pop('note', None)
    res.pop('text', None)
    _SSN_SYSTEMS = ('http://hl7.org/fhir/sid/us-ssn', 'urn:oid:2.16.840.1.113883.4.1')
    idents = res.get('identifier')
    if isinstance(idents, list):
        kept = [i for i in idents if not (isinstance(i, dict) and i.get('system') in _SSN_SYSTEMS)]
        if kept:
            res['identifier'] = kept
        else:
            res.pop('identifier', None)
    return res


@r6_blueprint.route('/$share-bundle', methods=['POST'])
def share_bundle():
    """
    Export a patient-controlled FHIR collection Bundle for SMART Health Link
    generation.

    Profiles:
        intake (default) — identified; name/DOB/address/insurance preserved;
                           SSN-class identifiers (http://hl7.org/fhir/sid/us-ssn
                           and urn:oid:2.16.840.1.113883.4.1), narrative text
                           (text), and free-text notes (note) stripped; meta.tag
                           stamped intake-identified.
        deidentified    — apply_patient_controlled_redaction; strips name/
                          telecom/address/notes, preserves birthDate and clinical
                          codes, injects healthclaw canonical identifier; stamps
                          meta.tag patient-controlled.  NOTE: this is
                          patient-controlled redaction, not HIPAA Safe Harbor
                          (birthDate is preserved, which Safe Harbor strips).

    Body (all optional JSON):
        patient_id      — if given, restrict to resources whose subject/patient/
                          beneficiary reference resolves to this patient id, plus
                          the Patient resource itself.
        resource_types  — list of FHIR resource types to include; defaults to
                          the SHL intake set.

    Returns: application/fhir+json  Bundle{type:collection}
    """
    DEFAULT_TYPES = [
        'Patient', 'Condition', 'AllergyIntolerance',
        'MedicationRequest', 'Immunization', 'Observation', 'Coverage',
    ]

    tenant = tenant_from_request(sources=(TenantSource.HEADER,))
    tenant_id = tenant.id

    # Step-up required — this bundle carries identified patient data
    # Access kernel, slice 6. 401 for both halves keeps this site's dialect.
    # The refusal text changes: the kernel names the nine causes a caller can
    # act on and collapses 'Token tenant mismatch', which this line used to
    # hand back verbatim (#478).
    require_grant(scope=Scope.WRITE, tenant=tenant,
                  absent_status=401, rejected_status=401)

    body = request.get_json(silent=True) or {}
    patient_id = body.get('patient_id') or None
    requested_types = body.get('resource_types')
    profile = body.get('profile', 'intake')

    VALID_PROFILES = ('intake', 'deidentified')
    if profile not in VALID_PROFILES:
        return _operation_outcome(
            'error', 'invalid',
            f'Invalid profile "{profile}". Valid values: {", ".join(VALID_PROFILES)}'
        ), 400

    if requested_types is None:
        resource_types = list(DEFAULT_TYPES)
    else:
        if not isinstance(requested_types, list):
            return _operation_outcome(
                'error', 'invalid',
                'resource_types must be a JSON array'
            ), 400
        unknown = [t for t in requested_types if t not in R6Resource.SUPPORTED_TYPES]
        if unknown:
            return _operation_outcome(
                'error', 'not-supported',
                f'Unknown resource type(s): {", ".join(unknown)}'
            ), 400
        resource_types = list(requested_types)

    # Query resources for this tenant
    query = R6Resource.query.filter(
        R6Resource.tenant_id == tenant_id,
        R6Resource.is_deleted == False,  # noqa: E712
        R6Resource.resource_type.in_(resource_types),
    )

    all_rows = query.all()

    # Apply patient filter when patient_id is supplied
    if patient_id:
        filtered = []
        for row in all_rows:
            if row.resource_type == 'Patient':
                # Include the Patient resource whose stored id matches
                data = json.loads(row.resource_json)
                if data.get('id') == patient_id or row.id == patient_id:
                    filtered.append(row)
            else:
                data = json.loads(row.resource_json)
                subject = data.get('subject', {}) or {}
                patient_ref = data.get('patient', {}) or {}
                beneficiary_ref = data.get('beneficiary', {}) or {}
                ref = (
                    subject.get('reference')
                    or patient_ref.get('reference')
                    or beneficiary_ref.get('reference')
                    or ''
                )
                if ref == f'Patient/{patient_id}':
                    filtered.append(row)
        all_rows = filtered

    # Apply profile-appropriate handling to every resource
    entries = []
    type_set = set()
    for row in all_rows:
        fhir_json = row.to_fhir_json()
        if profile == 'deidentified':
            # Determine the patient_id to pass to redaction; for Patient resources
            # the resource itself is the patient.
            redact_pid = (
                (patient_id or fhir_json.get('id'))
                if row.resource_type == 'Patient'
                else (patient_id or '')
            )
            resource = apply_patient_controlled_redaction(fhir_json, redact_pid)
        else:
            # intake profile: strip SSN-class identifiers and free-text, then
            # stamp meta.tag so receivers know this is an identified share.
            resource = _intake_strip(fhir_json)
            meta = resource.setdefault('meta', {})
            tags = meta.setdefault('tag', [])
            intake_tag = {
                'system': 'https://healthclaw.io/share-profile',
                'code': 'intake-identified',
            }
            if intake_tag not in tags:
                tags.append(intake_tag)
        entries.append({'resource': resource})
        type_set.add(row.resource_type)

    # Detect multi-patient tenant when no patient_id filter was applied
    patient_rows = [r for r in all_rows if r.resource_type == 'Patient']
    multi_patient_note = ''
    if not patient_id and len(patient_rows) > 1:
        multi_patient_note = ' [multi-patient tenant, no patient filter]'

    bundle = {
        'resourceType': 'Bundle',
        'type': 'collection',
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        'entry': entries,
    }

    record_audit_event(
        'read',
        resource_type='Bundle',
        resource_id='share-bundle',
        agent_id=request.headers.get('X-Agent-Id'),
        tenant_id=tenant_id,
        detail=(
            f'share-bundle export (profile={profile}): {len(entries)} resources '
            f'across {len(type_set)} type(s){multi_patient_note}'
        ),
    )

    return Response(
        json.dumps(bundle),
        status=200,
        mimetype='application/fhir+json',
    )


# --- FHIR Control Panel Aggregate Operations (read-only) ---

# Cap resources sampled per type in $profile-adherence to bound validation cost.
_PROFILE_ADHERENCE_SAMPLE_CAP = 50


@r6_blueprint.route('/$inventory', methods=['GET'])
def fhir_inventory():
    """
    $inventory — tenant-scoped resource census.

    Returns counts of non-deleted resources grouped by resource_type for the
    calling tenant, plus an overall total and the tenant's most-recent
    last_updated timestamp. Powers the FHIR control panel UI.

    Read-only: tenant isolation + audit apply, no step-up required.
    """
    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id

    # Efficient grouped count: one query, GROUP BY resource_type.
    rows = (
        db.session.query(
            R6Resource.resource_type,
            db.func.count(R6Resource.id),
        )
        .filter(
            R6Resource.tenant_id == tenant_id,
            R6Resource.is_deleted == False,  # noqa: E712
        )
        .group_by(R6Resource.resource_type)
        .all()
    )

    # Only types with count > 0 (GROUP BY already excludes zero), sorted desc.
    by_type = sorted(
        ((rt, count) for rt, count in rows if count > 0),
        key=lambda x: (-x[1], x[0]),
    )
    total = sum(count for _, count in by_type)

    last_updated_dt = (
        db.session.query(db.func.max(R6Resource.last_updated))
        .filter(
            R6Resource.tenant_id == tenant_id,
            R6Resource.is_deleted == False,  # noqa: E712
        )
        .scalar()
    )

    parameters = [
        {'name': 'tenant', 'valueString': tenant_id},
        {'name': 'total', 'valueInteger': total},
    ]
    if last_updated_dt is not None:
        parameters.append({
            'name': 'lastUpdated',
            'valueDateTime': last_updated_dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        })
    parameters.append({
        'name': 'byType',
        'part': [
            {'name': rt, 'valueInteger': count} for rt, count in by_type
        ],
    })

    record_audit_event(
        'read', 'Parameters', 'inventory',
        agent_id=request.headers.get('X-Agent-Id'),
        tenant_id=tenant_id,
        detail=f'$inventory: types={len(by_type)}, total={total}',
    )

    return jsonify({
        'resourceType': 'Parameters',
        'parameter': parameters,
    })


@r6_blueprint.route('/$profile-adherence', methods=['GET'])
def fhir_profile_adherence():
    """
    $profile-adherence — tenant-scoped conformance summary.

    For each resource type present, sample up to _PROFILE_ADHERENCE_SAMPLE_CAP
    resources and run each through the structural validator (US Core required
    fields). Aggregates per-type adherence and the most common failing
    diagnostics, plus an overall adherence ratio across all sampled resources.

    Uses the validator's network-free structural path (_validate_structural)
    so the operation is fast and deterministic for the demo — it never calls
    the external HL7 validator even when one is configured.

    Read-only: tenant isolation + audit apply, no step-up required.
    """
    tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id

    # Distinct types present for this tenant, with totals.
    type_rows = (
        db.session.query(
            R6Resource.resource_type,
            db.func.count(R6Resource.id),
        )
        .filter(
            R6Resource.tenant_id == tenant_id,
            R6Resource.is_deleted == False,  # noqa: E712
        )
        .group_by(R6Resource.resource_type)
        .all()
    )

    by_type_parts = []
    total_sampled = 0
    total_conformant = 0

    # Sort by total desc for a stable, useful ordering in the UI.
    for resource_type, total in sorted(type_rows, key=lambda x: (-x[1], x[0])):
        if total <= 0:
            continue
        sampled_rows = (
            R6Resource.query.filter_by(
                resource_type=resource_type,
                is_deleted=False,
                tenant_id=tenant_id,
            )
            .order_by(R6Resource.last_updated.desc())
            .limit(_PROFILE_ADHERENCE_SAMPLE_CAP)
            .all()
        )

        sampled = len(sampled_rows)
        conformant = 0
        issue_counts = {}
        for row in sampled_rows:
            try:
                resource = json.loads(row.resource_json)
            except (ValueError, TypeError):
                # Unparseable stored JSON counts as non-conformant.
                issue_counts['Stored resource is not valid JSON'] = (
                    issue_counts.get('Stored resource is not valid JSON', 0) + 1
                )
                continue
            resource.setdefault('resourceType', resource_type)
            # Network-free structural validation only.
            result = validator._validate_structural(resource)
            if result.get('valid'):
                conformant += 1
            else:
                for issue in result.get('operation_outcome', {}).get('issue', []):
                    if issue.get('severity') not in ('error', 'fatal'):
                        continue
                    diag = issue.get('diagnostics') or 'Unknown issue'
                    issue_counts[diag] = issue_counts.get(diag, 0) + 1

        total_sampled += sampled
        total_conformant += conformant

        adherence = round(conformant / sampled, 2) if sampled else 0.0
        top_issues = sorted(
            issue_counts.items(), key=lambda x: (-x[1], x[0])
        )[:3]
        top_issues_str = '; '.join(
            f'{diag} ({count})' for diag, count in top_issues
        )

        part = [
            {'name': 'total', 'valueInteger': total},
            {'name': 'sampled', 'valueInteger': sampled},
            {'name': 'conformant', 'valueInteger': conformant},
            {'name': 'adherence', 'valueDecimal': adherence},
        ]
        if top_issues_str:
            part.append({'name': 'topIssues', 'valueString': top_issues_str})

        by_type_parts.append({'name': resource_type, 'part': part})

    overall = round(total_conformant / total_sampled, 2) if total_sampled else 0.0

    record_audit_event(
        'read', 'Parameters', 'profile-adherence',
        agent_id=request.headers.get('X-Agent-Id'),
        tenant_id=tenant_id,
        detail=(
            f'$profile-adherence: types={len(by_type_parts)}, '
            f'sampled={total_sampled}, conformant={total_conformant}'
        ),
    )

    return jsonify({
        'resourceType': 'Parameters',
        'parameter': [
            {'name': 'tenant', 'valueString': tenant_id},
            {'name': 'overallAdherence', 'valueDecimal': overall},
            {'name': 'byType', 'part': by_type_parts},
        ],
    })


# --- Helper Functions ---

def _operation_outcome(severity, code, diagnostics):
    """Build a FHIR OperationOutcome response."""
    return jsonify({
        'resourceType': 'OperationOutcome',
        'issue': [
            {
                'severity': severity,
                'code': code,
                'diagnostics': diagnostics
            }
        ]
    })


# --- SDC ($populate / $extract) ---
from r6.sdc.routes import register_sdc_routes  # noqa: E402

register_sdc_routes(r6_blueprint, {
    "operation_outcome": _operation_outcome,
    "authenticate_tenant_read": authenticate_tenant_read,
    "validate_step_up_token": validate_step_up_token,
    "validator": validator,
})

# --- Quality measures ($evaluate-measure — NQF 0018) ---
from r6.quality.routes import register_quality_routes  # noqa: E402

register_quality_routes(r6_blueprint, {
    "operation_outcome": _operation_outcome,
    "authenticate_tenant_read": authenticate_tenant_read,
})

# --- Lab reference-range interpreter ($interpret) ---
from r6.labs.routes import register_labs_routes  # noqa: E402

register_labs_routes(r6_blueprint, {
    "operation_outcome": _operation_outcome,
    "authenticate_tenant_read": authenticate_tenant_read,
})

# --- Preventive-care gaps ($care-gaps) ---
from r6.caregaps.routes import register_caregaps_routes  # noqa: E402

register_caregaps_routes(r6_blueprint, {
    "operation_outcome": _operation_outcome,
    "authenticate_tenant_read": authenticate_tenant_read,
})

# --- Appointment Brief ($appointment-brief) ---
from r6.brief.routes import register_brief_routes  # noqa: E402

register_brief_routes(r6_blueprint, {
    "operation_outcome": _operation_outcome,
    "authenticate_tenant_read": authenticate_tenant_read,
})

# --- Guardrail conformance self-test ($conformance) ---
from r6.conformance.routes import register_conformance_routes  # noqa: E402

register_conformance_routes(r6_blueprint, {})
